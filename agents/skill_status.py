"""运行时 skill 状态：连接离线评测与线上检索/注入的轻量桥梁。

状态来源分两级：
1. primary：离线评测每次运行物化的 status_map.json（含规则通过率等完整信号）。
   只有这一级的状态参与检索权重——未认证的估算不奖励也不惩罚，防止把
   "还没评测"误判成"该被降权"。
2. fallback：尚未跑过评测时从 usage stats 粗估，仅供试用期标注与 fork 禁令
   使用（pruned 直接采信；行为达标的视为已过试用期；否则按 incubating 对待）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .skill_evolution import (SKILL_USAGE_STATS, _append_jsonl, _utc_now,
                              get_evolution_dir, skill_state_read_only)

# 检索权重只对评测认证过的状态生效；其余状态一律 1.0（不惩罚新 skill）。
STATUS_WEIGHTS = {"healthy": 1.25, "watch": 0.75}
# 试用期状态：注入时打 provisional 标注，并禁用 fork 执行。
PROVISIONAL_STATUSES = {"incubating", "unobserved"}


SCOPED_STATUS_FILE = "skill_status_scoped.json"
SCOPED_STATUS_AUDIT = "skill_status_scoped_audit.jsonl"


def status_map_path() -> Path:
    return get_evolution_dir() / "online-eval" / "status_map.json"


def status_scoped_path() -> Path:
    return get_evolution_dir() / SCOPED_STATUS_FILE


def _scope_key(scope: dict | None) -> str:
    s = scope or {}
    return "|".join(str(s.get(key) or "").strip() for key in
                    ("tenant", "space", "owner"))


def get_status_scoped_map(scope: dict | None) -> dict[str, str]:
    """作用域状态：base 有效状态 + 该 tenant/space/owner 的覆盖。"""
    base = dict(get_effective_status_map())
    data = _read_json(status_scoped_path(), {})
    buckets = data.get("statuses") if isinstance(data, dict) else {}
    bucket = buckets.get(_scope_key(scope)) if isinstance(buckets, dict) else {}
    if isinstance(bucket, dict):
        base.update({str(name): str(status) for name, status in bucket.items()})
    return base


def set_status_scoped(skill_name: str, status: str, *, scope: dict,
                      actor: str = "system", reason: str = "") -> dict[str, Any]:
    """作用域状态覆盖入口：只影响该 tenant/space/owner，base 状态表不动。"""
    if skill_state_read_only():
        return {"ok": False, "error": "holdout read-only mode: status writes disabled"}
    skill = str(skill_name or "").strip()
    target = str(status or "").strip()
    if not skill or not target:
        return {"ok": False, "error": "skill_and_status_required"}
    resolved_scope = {key: str((scope or {}).get(key) or "").strip()
                      for key in ("tenant", "space", "owner")}
    if not any(resolved_scope.values()):
        return {"ok": False, "error": "scope_required"}
    key = _scope_key(resolved_scope)
    data = _read_json(status_scoped_path(), {})
    buckets = data.get("statuses") if isinstance(data, dict) else {}
    if not isinstance(buckets, dict):
        buckets = {}
    bucket = dict(buckets.get(key) or {})
    bucket[skill] = target
    buckets[key] = bucket
    path = status_scoped_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "statuses": buckets},
                               ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    _append_jsonl(get_evolution_dir() / SCOPED_STATUS_AUDIT, {
        "event": "status_change", "time": _utc_now(), "skill": skill,
        "status": target, "scope": resolved_scope, "actor": actor,
        "reason": str(reason or "")[:300],
    })
    return {"ok": True, "skill": skill, "status": target,
            "scope": resolved_scope}


_cached_status_map: dict[str, str] | None = None
_cached_certified_map: dict[str, str] | None = None
# Each view tracks its own immutable file snapshot. Timestamps alone can collide
# on fast rewrites and do not identify the scope when the source path changes.
_cached_status_identity: tuple[str, bytes] | None = None
_cached_certified_identity: tuple[str, bytes] | None = None


def _status_snapshot(path: Path) -> tuple[tuple[str, bytes] | None, bytes]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None, b""
    return (str(path.resolve()), hashlib.sha256(raw).digest()), raw


def _decode_snapshot(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def reset_status_cache() -> None:
    """撤权/作用域变化时清空状态缓存（下一次读取重新物化）。"""
    global _cached_status_map, _cached_certified_map
    global _cached_status_identity, _cached_certified_identity
    _cached_status_map = None
    _cached_certified_map = None
    _cached_status_identity = None
    _cached_certified_identity = None


def get_trusted_status_map() -> dict[str, str]:
    """读评测物化的状态表；文件更新时自动刷新。没跑过评测时返回空 dict。"""
    global _cached_status_map, _cached_status_identity
    path = status_map_path()
    identity, raw = _status_snapshot(path)
    if identity is None:
        _cached_status_map = {}
        _cached_status_identity = None
        return {}
    if _cached_status_map is None or identity != _cached_status_identity:
        data = _decode_snapshot(raw)
        statuses = data.get("statuses") if isinstance(data, dict) else None
        _cached_status_map = (
            {str(k): str(v) for k, v in statuses.items()} if isinstance(statuses, dict) else {}
        )
        _cached_status_identity = identity
    return _cached_status_map


def get_certified_status_map() -> dict[str, str]:
    """有 replay 答卷、经状态机认证的状态——只有这一份参与检索加权。

    无答卷 skill 的估算状态（典型是手动导入）只用于试用期待遇，
    不加权不降权：行为达标是"不惩罚"，够不上"认证奖励"。
    """
    global _cached_certified_map, _cached_certified_identity
    path = status_map_path()
    identity, raw = _status_snapshot(path)
    if identity is None:
        _cached_certified_map = {}
        _cached_certified_identity = None
        return {}
    if _cached_certified_map is None or identity != _cached_certified_identity:
        data = _decode_snapshot(raw)
        certified = data.get("certified_statuses") if isinstance(data, dict) else None
        _cached_certified_map = (
            {str(k): str(v) for k, v in certified.items()} if isinstance(certified, dict) else {}
        )
        _cached_certified_identity = identity
    return _cached_certified_map


def estimate_status_from_usage(item: dict[str, Any]) -> str:
    """按 usage stats 粗估运行时状态（公开给评测侧复用：无答卷 skill 的状态表写入）。"""
    if item.get("pruned"):
        return "pruned"
    retrieved = int(item.get("retrieved", 0) or 0)
    relevant = int(item.get("relevant", 0) or 0)
    used = int(item.get("used", 0) or 0)
    if retrieved >= 5 and used / max(1, retrieved) >= 0.2 and relevant / max(1, retrieved) >= 0.35:
        # 行为达标但未经离线评测认证：不再按试用期对待（也不参与权重奖惩）。
        return "watch"
    if retrieved > 0:
        return "incubating"
    return "unobserved"


def get_effective_status_map() -> dict[str, str]:
    """试用期判定用的状态：优先评测表，缺省时按 usage stats 粗估。"""
    trusted = get_trusted_status_map()
    if trusted:
        return trusted
    stats = _read_json(get_evolution_dir() / SKILL_USAGE_STATS, {})
    if not isinstance(stats, dict):
        return {}
    return {
        str(name): estimate_status_from_usage(item)
        for name, item in stats.items()
        if isinstance(item, dict)
    }
