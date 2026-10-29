"""部署分区（deployment tiers）：skill 的第三张正交状态表。

五状态机（healthy/watch/incubating/unobserved/pruned，status_map.json）回答
"质量证据状态"；本模块回答"部署信任级别/来源"：
- experimental（实验区）：自进化（create/evolve）写出的 skill，BM25 可召回但
  注入带 "[experimental: ...]" 来源标注，禁 fork；
- staging（预发区）：证据双达标（状态机 healthy + champion 门）自动晋升而来，
  默认注入、可一键回退；
- core（核心区）：等同手写内置。缺省 core——存量与手写 skill 行为零变化。

契约（设计评审钉死）：
- 缓存时序：get_tier_map() 按 mtime 动态读盘（含路径比对，照 skill_status.py
  模式加固），绝不烤进 discover_skills 的 _cached_skills——晋升发生在评测 CLI
  的另一进程，运行中会话必须在下一次检索时生效。
- 导入方向：本模块模块级 import skill_evolution 的 IO 帮手；所有反向引用
  （skill_evolution / online_skill_eval / skills / agent / skill_repair）一律
  函数内延迟导入（先例 online_skill_eval 僵尸归档、online_skill_evolution._ir_tools）。
- 失效模式：deployment_tiers.json 损坏时按缺省 {} 读=全员 core=实验区隔离
  静默消失（与 status_map 同款弱点）。写入走同目录 tmp+os.replace 原子替换
  降低撕裂概率；恢复路径为重放追加式 tier_audit.jsonl（每 skill 取最后一条
  tier_change 行，restore_from_audit）。
- 与 experience_standard 纯分工：出生静态内容门（IR 校验）与上线后动态行为门
  （tier 晋升）互不 import。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .skill_evolution import _append_jsonl, _read_json, _utc_now, get_evolution_dir, skill_state_read_only

TIER_EXPERIMENTAL = "experimental"
TIER_STAGING = "staging"
TIER_CORE = "core"
VALID_TIERS = (TIER_EXPERIMENTAL, TIER_STAGING, TIER_CORE)
DEFAULT_TIER = TIER_CORE

TIERS_FILE = "deployment_tiers.json"
TIER_AUDIT_LOG = "tier_audit.jsonl"
TIER_FAULT_LOG = "tier_fault.jsonl"


def deployment_tiers_path() -> Path:
    return get_evolution_dir() / TIERS_FILE


def tier_audit_path() -> Path:
    return get_evolution_dir() / TIER_AUDIT_LOG


def tier_fault_path() -> Path:
    """分区元数据损坏/恢复的故障记录（可诊断：受影响 Skill + 恢复动作）。"""
    return get_evolution_dir() / TIER_FAULT_LOG


def experimental_injection_enabled() -> bool:
    """保守注入开关，每次调用时读环境变量（monkeypatch.setenv 可测，不 import 期固化）。

    EVOHARNESS_TIER_EXPERIMENTAL_INJECT=0 时实验区 skill 不进检索命中（默认开=
    召回带标注）。保守模式的代价：使用窗口样本断供、只剩证据组，自动晋升不可达
    ——与"写入即上线攒证据"的冷启动逻辑冲突，故默认注入带标注。
    """
    raw = os.environ.get("EVOHARNESS_TIER_EXPERIMENTAL_INJECT", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _auto_rollback_enabled() -> bool:
    # 自动回退门：默认关（staging skill 认证状态跌 watch 且硬失败>0 时才回实验区）。
    raw = os.environ.get("EVOHARNESS_TIER_AUTO_ROLLBACK", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


_cached_tiers: dict[str, str] | None = None
_cached_tiers_mtime: float = -1.0
_cached_tiers_path: str = ""
# 隔离态：分区元数据不可信（损坏且无法从审计恢复）时置位。此时未知 skill 不得被当作
# 正式能力（get_tier 缺省返回 experimental），且自动晋升一律禁止。
_degraded: bool = False
_degraded_reason: str = ""


def reset_tier_cache() -> None:
    global _cached_tiers, _cached_tiers_mtime, _cached_tiers_path, _degraded, _degraded_reason
    _cached_tiers = None
    _cached_tiers_mtime = -1.0
    _cached_tiers_path = ""
    _degraded = False
    _degraded_reason = ""


def tier_state_degraded() -> bool:
    """分区元数据当前是否处于「不可信 → 强制隔离」态。"""
    get_tier_map()
    return _degraded


def _runtime_initialized() -> bool:
    """区分「首次安装」与「已初始化运行时」。

    首次安装（无审计、无状态机产物）时缺省 {} = 全员 core 是正确的兼容行为；一旦运行时
    已有分区/状态痕迹，tiers 文件缺失或损坏就不能再静默回落为全员 core，否则实验区隔离
    会消失、被隔离的 Skill 会被重新当作正式能力。
    """
    if tier_audit_path().is_file():
        return True
    if (get_evolution_dir() / "online-eval" / "status_map.json").is_file():
        return True
    return False


def _record_tier_fault(event: str, reason: str, detail: str, affected: list[str]) -> None:
    try:
        _append_jsonl(
            tier_fault_path(),
            {
                "event": event,
                "time": _utc_now(),
                "reason": reason,
                "detail": detail,
                "affected": affected,
                "recovery": (
                    "auto-recovered from tier_audit.jsonl (append-only log)"
                    if event == "tier_metadata_recovered"
                    else "manual review required; isolation forced (unknown skills -> experimental)"
                ),
            },
        )
    except Exception as error:  # noqa: BLE001
        # 诊断通道自己失败时不能再调 _record_tier_fault（会递归），
        # 但也不能完全静默——否则故障记录丢失且无任何痕迹。
        import sys as _sys
        try:
            _sys.stderr.write(
                f"[tier-fault] cannot record {event}: {type(error).__name__}: {error}\n")
        except Exception:  # noqa: BLE001
            pass  # stderr 不可用（GUI 无控制台）时放弃，避免递归故障


def _parse_tiers_file(path: Path) -> dict[str, str] | None:
    """严格解析分区表；任何非法分区值都视为损坏（不得静默降级为 core）。"""
    data = _read_json(path, None)
    tiers = data.get("tiers") if isinstance(data, dict) else None
    if not isinstance(tiers, dict):
        return None
    out: dict[str, str] = {}
    for k, v in tiers.items():
        sv = str(v)
        if sv not in VALID_TIERS:
            return None
        out[str(k)] = sv
    return out


# 信任度排序：experimental 最低（隔离最严）。合并时取「更受限」的一侧。
_TRUST_RANK = {TIER_EXPERIMENTAL: 0, TIER_STAGING: 1, TIER_CORE: 2}


def _reconcile_with_audit(parsed: dict[str, str]) -> dict[str, str]:
    """文件与追加式审计不一致时取「信任度更低」的一侧（隔离优先）。

    合法但**缺条目**的文件（或并发读写丢失更新写出的文件）会把审计日志里已隔离的 skill 读成
    缺省 core。审计是「写入序在前」的追加日志，作为一致性底线：任何 skill 在审计中更受限，
    就以审计为准（宁可隔离过度，不可隔离消失）。
    """
    audit = rebuild_tiers_from_audit()
    if not audit:
        return parsed
    merged = dict(parsed)
    mismatched: list[str] = []
    for skill, atier in audit.items():
        ctier = merged.get(skill)
        if ctier is None or _TRUST_RANK.get(atier, 2) < _TRUST_RANK.get(ctier, 2):
            merged[skill] = atier
            mismatched.append(skill)
    if mismatched:
        _record_tier_fault(
            "tier_file_audit_mismatch",
            "tiers_file_inconsistent_with_audit",
            "file omits/downgrades skills present in audit; less-trusted (audit) tier applied",
            sorted(mismatched),
        )
    return merged


def _adopt_tiers(tiers: dict[str, str]) -> dict[str, str]:
    global _cached_tiers, _cached_tiers_mtime, _cached_tiers_path, _degraded, _degraded_reason
    path = deployment_tiers_path()
    _cached_tiers = tiers
    _cached_tiers_path = str(path)
    _cached_tiers_mtime = path.stat().st_mtime if path.is_file() else -1.0
    _degraded = False
    _degraded_reason = ""
    return tiers


def _recover_or_isolate(reason: str, detail: str) -> dict[str, str]:
    """分区元数据缺失/损坏时的 fail-safe 处理。

    1) 优先按追加式审计（写入序：审计先行）重放，采用「最近有效快照」并原子回写自愈；
    2) 无可重放记录时进入隔离态：不返回空表冒充全员 core，让 get_tier 对未知 skill 强制
       experimental，并留下可诊断的故障记录。
    """
    global _cached_tiers, _cached_tiers_path, _cached_tiers_mtime, _degraded, _degraded_reason
    rebuilt = rebuild_tiers_from_audit()
    if rebuilt:
        try:
            _write_tiers_atomic(
                deployment_tiers_path(),
                {"version": 1, "generated_at": _utc_now(), "tiers": rebuilt},
            )
        except Exception:
            pass
        _record_tier_fault("tier_metadata_recovered", reason, detail, sorted(rebuilt))
        return _adopt_tiers(rebuilt)
    _degraded = True
    _degraded_reason = reason
    _cached_tiers = None
    _cached_tiers_path = str(deployment_tiers_path())
    _cached_tiers_mtime = -1.0
    _record_tier_fault("tier_metadata_lost_isolated", reason, detail, ["<all skills>"])
    return {}


def get_tier_map() -> dict[str, str]:
    """读分区现状表；mtime+路径任一变化自动刷新。

    文件缺失/损坏时不再无条件返回空表（全员 core）：已初始化运行时优先从审计恢复，无法
    恢复则进入隔离态（见 tier_state_degraded）。
    """
    global _cached_tiers, _cached_tiers_path, _cached_tiers_mtime, _degraded, _degraded_reason
    path = deployment_tiers_path()
    key = str(path)
    if not path.is_file():
        if _degraded and _cached_tiers_path == key:
            return {}
        if _cached_tiers_path == key and _cached_tiers is not None:
            return _cached_tiers
        reset_tier_cache()
        if _runtime_initialized():
            return _recover_or_isolate("tiers_file_missing", f"{path} not found")
        _cached_tiers = {}
        _cached_tiers_path = key
        return _cached_tiers
    mtime = path.stat().st_mtime
    if _cached_tiers is not None and key == _cached_tiers_path and mtime == _cached_tiers_mtime:
        return _cached_tiers
    parsed = _parse_tiers_file(path)
    if parsed is not None:
        return _adopt_tiers(_reconcile_with_audit(parsed))
    return _recover_or_isolate("tiers_file_corrupt", f"{path} unreadable or invalid tier values")


def get_tier(skill_name: str) -> str:
    name = str(skill_name or "").strip()
    tiers = get_tier_map()
    if _degraded:
        # 隔离态：分区信息不可信，未知 skill 不得被当作正式能力。
        return tiers.get(name, TIER_EXPERIMENTAL)
    tier = tiers.get(name, DEFAULT_TIER)
    return tier if tier in VALID_TIERS else DEFAULT_TIER


def _write_tiers_atomic(path: Path, value: dict[str, Any]) -> None:
    # 同目录 tmp + os.replace：读侧要么看到旧整文件要么看到新整文件，不读半截。
    # 两个 Windows 真实缺陷（曾让 test_cli_tier_commands 在门禁里偶发 PermissionError，
    # 而单跑全绿——正是竞态的特征）：
    #   1. 固定 tmp 名（path.name + ".tmp"）：两个并发写者会互相覆盖/互撞；
    #   2. os.replace 无重试：Windows 上新写文件常被杀毒/索引器短暂占用，replace 立刻抛
    #      PermissionError，一次性失败让分区表写入丢失（生产上等于"晋级凭空没生效"）。
    # 修复：唯一 tmp 名（对齐 agents/session.py 的 atomic_write_text）+ 短退避重试。
    if skill_state_read_only():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(str(tmp), str(path))
            return
        except PermissionError as error:  # Windows 瞬时锁：退避后重试
            last_error = error
            time.sleep(0.05 * (attempt + 1))
    # 重试用尽：清理残留 tmp 后把错误抛给调用方（set_tier 会记审计失败），不静默丢写。
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass
    raise last_error if last_error is not None else RuntimeError("tiers write failed")


def set_tier(
    skill_name: str,
    to_tier: str,
    *,
    trigger: str,
    actor: str = "system",
    producer: str = "",
    kind: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """唯一写入口：追加审计行 → 原子写分区表 → 重置缓存。from==to 也留痕（如 repair）。"""
    if skill_state_read_only():
        return {"ok": False, "error": "holdout read-only mode: tier writes disabled"}
    skill = str(skill_name or "").strip()
    if not skill:
        return {"ok": False, "error": "skill name is required"}
    target = to_tier if to_tier in VALID_TIERS else DEFAULT_TIER
    from_tier = get_tier(skill)
    row = {
        "event": "tier_change",
        "time": _utc_now(),
        "skill": skill,
        "producer": producer,
        "kind": kind,
        "from_tier": from_tier,
        "to_tier": target,
        "trigger": trigger,
        "actor": actor,
        "evidence": evidence or {},
        "error": "",
    }
    _append_jsonl(tier_audit_path(), row)
    tiers = dict(get_tier_map())
    tiers[skill] = target
    _write_tiers_atomic(
        deployment_tiers_path(),
        {"version": 1, "generated_at": _utc_now(), "tiers": tiers},
    )
    reset_tier_cache()
    return {"ok": True, "skill": skill, "from_tier": from_tier, "to_tier": target, "trigger": trigger}


def register_evolution_write(skill_name: str, *, producer: str = "", evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """自进化写出的新 skill / 修改过的 skill 一律（回）进实验区（设计契约）。"""
    return set_tier(
        skill_name,
        TIER_EXPERIMENTAL,
        trigger="evolution_write",
        actor="system",
        producer=producer or "online_evolution",
        evidence=evidence,
    )


def register_repair(skill_name: str, *, kind: str = "", evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """修复不改 tier（已评审偏离：人工触发/作用于已损坏文件/失败自动回滚），只留痕记 kind。

    豁免不外推到未来自动修复路径——全自动修复必须走 register_evolution_write。
    """
    return set_tier(
        skill_name,
        get_tier(skill_name),
        trigger="repair",
        actor="system",
        producer="repair",
        kind=kind,
        evidence=evidence,
    )


def _manual_transition(skill_name: str, to_tier: str, trigger: str) -> dict[str, Any]:
    skill = str(skill_name or "").strip()
    if not skill:
        return {"ok": False, "error": "skill name is required"}
    expected = TIER_EXPERIMENTAL if to_tier == TIER_STAGING else TIER_STAGING
    current = get_tier(skill)
    if current != expected:
        return {
            "ok": False,
            "error": f"skill '{skill}' is tier {current}, {trigger} requires {expected}",
        }
    return set_tier(skill, to_tier, trigger=trigger, actor="user", producer="manual")


def manual_promote(skill_name: str) -> dict[str, Any]:
    """人工 experimental→staging：有 champion 血统的修订版卡实验区的唯一释放阀。"""
    return _manual_transition(skill_name, TIER_STAGING, "manual_promote")


def manual_promote_core(skill_name: str) -> dict[str, Any]:
    """人工 staging→core（沿用 champion 晋级纯人工哲学）。"""
    return _manual_transition(skill_name, TIER_CORE, "manual_core_promote")


def rollback(skill_name: str, *, reason: str = "") -> dict[str, Any]:
    """一键回退：staging→experimental（只作用于预发区，不降级 core/experimental）。"""
    skill = str(skill_name or "").strip()
    if not skill:
        return {"ok": False, "error": "skill name is required"}
    current = get_tier(skill)
    if current != TIER_STAGING:
        return {"ok": False, "error": f"skill '{skill}' is tier {current}, rollback applies to staging only"}
    evidence = {"reason": reason} if reason else {}
    return set_tier(skill, TIER_EXPERIMENTAL, trigger="manual_rollback", actor="user", producer="manual", evidence=evidence)


def _maybe_auto_rollback(item: dict[str, Any], name: str) -> bool:
    if not _auto_rollback_enabled():
        return False
    if get_tier(name) != TIER_STAGING:
        return False
    if str(item.get("status") or "") != "watch":
        return False
    replay = item.get("replay") if isinstance(item.get("replay"), dict) else {}
    if int(replay.get("count", 0) or 0) <= 0:
        return False
    eval_data = item.get("eval") if isinstance(item.get("eval"), dict) else {}
    hard_failures = int(eval_data.get("hard_failures", 0) or 0)
    if hard_failures <= 0:
        return False
    set_tier(
        name,
        TIER_EXPERIMENTAL,
        trigger="auto_guard_hard_failure",
        actor="system",
        producer="online_eval",
        evidence={"status": "watch", "hard_failures": hard_failures},
    )
    return True


def apply_eval_promotions(report: Any) -> dict[str, Any]:
    """消费评测报告，experimental→staging 自动晋升：status==healthy + champion 门双断言。

    fail-closed 契约：只认报告内 skills[].artifacts.promotion，绝不回读
    status_map.json——write_artifacts=False 的干跑也会物化 status_map（见
    tests/test_eval_rule_ir.py 的干跑用例），回读会让干跑路径触发晋升。
    artifacts.promotion 缺失 → 不晋升，这是特性不是缺陷。
    双断言保留为防御纵深：_promotion_decision 对非 healthy 提前返回 promoted=False，
    (b) 蕴含 (a)，但显式断言 healthy 让门槛可读。
    """
    report_dict = report if isinstance(report, dict) else {}
    if tier_state_degraded():
        # 分区元数据不可信：新增 Skill 缺少可信分区信息，禁止直接晋升。
        return {
            "promoted": [],
            "skipped": [
                {"skill": "<all>", "why": f"tier metadata degraded ({_degraded_reason}); promotion blocked"}
            ],
            "rolled_back": [],
        }
    skills = report_dict.get("skills") if isinstance(report_dict.get("skills"), list) else []
    thresholds = report_dict.get("thresholds") if isinstance(report_dict.get("thresholds"), dict) else {}
    generated_at = str(report_dict.get("generated_at") or "")
    promoted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    rolled_back: list[str] = []
    rollback_errors: list[dict[str, Any]] = []
    for item in skills:
        if not isinstance(item, dict):
            continue
        name = str(item.get("skill") or "").strip()
        if not name:
            continue
        try:
            if _maybe_auto_rollback(item, name):
                rolled_back.append(name)
        except Exception as error:  # noqa: BLE001
            # 自动回退没生效却无声 = 该退的技能还在预发区，必须让调用方知道。
            rollback_errors.append({"skill": name,
                                    "error": f"{type(error).__name__}: {error}"[:200]})
        if get_tier(name) != TIER_EXPERIMENTAL:
            continue
        status = str(item.get("status") or "")
        if status != "healthy":
            skipped.append({"skill": name, "why": f"status={status or 'unknown'} (need healthy)"})
            continue
        artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
        promotion = artifacts.get("promotion") if isinstance(artifacts.get("promotion"), dict) else {}
        if not promotion:
            skipped.append({"skill": name, "why": "artifacts.promotion missing (dry-run or no champion gate output)"})
            continue
        if not promotion.get("promoted"):
            skipped.append({"skill": name, "why": f"champion gate rejected ({promotion.get('status') or 'rejected'})"})
            continue
        replay = item.get("replay") if isinstance(item.get("replay"), dict) else {}
        eval_data = item.get("eval") if isinstance(item.get("eval"), dict) else {}
        evidence = {
            "status": status,
            "reasons": [str(r) for r in (item.get("reasons") or []) if str(r).strip()],
            "score_samples": int(replay.get("count", 0) or 0),
            "promotion_test_samples": int(replay.get("promotion_test", 0) or 0),
            "retrieved": int(item.get("retrieved", 0) or 0),
            "relevant": int(item.get("relevant", 0) or 0),
            "used": int(item.get("used", 0) or 0),
            "used_rate": float(item.get("used_rate", 0.0) or 0.0),
            "relevance_rate": float(item.get("relevance_rate", 0.0) or 0.0),
            "rule_pass_rate": float(eval_data.get("pass_rate", 0.0) or 0.0),
            "hard_failures": int(eval_data.get("hard_failures", 0) or 0),
            "champion_gate": promotion,
            "status_map_generated_at": generated_at,
            "thresholds": thresholds,
        }
        try:
            result = set_tier(
                name,
                TIER_STAGING,
                trigger="auto_evidence",
                actor="system",
                producer="online_eval",
                evidence=evidence,
            )
        except Exception as exc:
            skipped.append({"skill": name, "why": f"set_tier failed: {type(exc).__name__}"})
            continue
        if result.get("ok"):
            promoted.append({"skill": name, "from_tier": result.get("from_tier"), "to_tier": result.get("to_tier")})
    return {"promoted": promoted, "skipped": skipped, "rolled_back": rolled_back,
            "rollback_errors": rollback_errors}


def fork_blocked_reason(skill_name: str) -> str:
    """fork 禁令裁决（agent 分发点调用）：试用期状态机门 + 实验区分区门共一出口。

    返回禁令原因；空串=放行。运行时动态读两张表——晋升/回退发生在另一进程时，
    运行中会话的下一次 fork 请求立即按新 tier 裁决。
    """
    from .skill_status import PROVISIONAL_STATUSES, get_effective_status_map

    skill = str(skill_name or "").strip()
    trial_status = get_effective_status_map().get(skill, "")
    if trial_status in PROVISIONAL_STATUSES:
        return f"trial period (status={trial_status})"
    if get_tier(skill) == TIER_EXPERIMENTAL:
        return "deployment tier experimental"
    return ""


def rebuild_tiers_from_audit() -> dict[str, str]:
    """重放追加式审计：每 skill 取最后一条 tier_change 行的 to_tier。"""
    out: dict[str, str] = {}
    path = tier_audit_path()
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict) or row.get("event") != "tier_change":
            continue
        skill = str(row.get("skill") or "").strip()
        tier = str(row.get("to_tier") or "").strip()
        if skill and tier in VALID_TIERS:
            out[skill] = tier
    return out


def restore_from_audit() -> dict[str, str]:
    """按审计重放原子重建分区表（deployment_tiers.json 损坏时的恢复路径）。"""
    tiers = rebuild_tiers_from_audit()
    _write_tiers_atomic(
        deployment_tiers_path(),
        {"version": 1, "generated_at": _utc_now(), "tiers": tiers},
    )
    reset_tier_cache()
    return tiers


def format_tiers() -> str:
    tiers = get_tier_map()
    if not tiers:
        return "No deployment tier overrides recorded (all skills default to core)."
    lines = ["Deployment tiers:"]
    for name in sorted(tiers):
        lines.append(f"  {name}: {tiers[name]}")
    return "\n".join(lines)
