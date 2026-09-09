"""运行时 skill 状态：连接离线评测与线上检索/注入的轻量桥梁。

状态来源分两级：
1. primary：离线评测每次运行物化的 status_map.json（含规则通过率等完整信号）。
   只有这一级的状态参与检索权重——未认证的估算不奖励也不惩罚，防止把
   "还没评测"误判成"该被降权"。
2. fallback：尚未跑过评测时从 usage stats 粗估，仅供试用期标注与 fork 禁令
   使用（pruned 直接采信；行为达标的视为已过试用期；否则按 incubating 对待）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .skill_evolution import SKILL_USAGE_STATS, get_evolution_dir

# 检索权重只对评测认证过的状态生效；其余状态一律 1.0（不惩罚新 skill）。
STATUS_WEIGHTS = {"healthy": 1.25, "watch": 0.75}
# 试用期状态：注入时打 provisional 标注，并禁用 fork 执行。
PROVISIONAL_STATUSES = {"incubating", "unobserved"}


def status_map_path() -> Path:
    return get_evolution_dir() / "online-eval" / "status_map.json"


_cached_status_map: dict[str, str] | None = None
_cached_mtime: float = -1.0


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def get_trusted_status_map() -> dict[str, str]:
    """读评测物化的状态表；文件更新时自动刷新。没跑过评测时返回空 dict。"""
    global _cached_status_map, _cached_mtime
    path = status_map_path()
    if not path.is_file():
        _cached_status_map = {}
        _cached_mtime = -1.0
        return {}
    mtime = path.stat().st_mtime
    if _cached_status_map is None or mtime != _cached_mtime:
        data = _read_json(path, {})
        statuses = data.get("statuses") if isinstance(data, dict) else None
        _cached_status_map = (
            {str(k): str(v) for k, v in statuses.items()} if isinstance(statuses, dict) else {}
        )
        _cached_mtime = mtime
    return _cached_status_map


def _estimate_status_from_usage(item: dict[str, Any]) -> str:
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
        str(name): _estimate_status_from_usage(item)
        for name, item in stats.items()
        if isinstance(item, dict)
    }
