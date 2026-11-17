#!/usr/bin/env python3
"""DE-36 搜索增强独立验收评估器：原生基线 vs 新策略公平对照 + 双线报告。

对应 tasks.json DE-36 五条 implementation_steps 与 SKILL-EVOLUTION-SEARCH：
1. **搜索运行面（S1）**：``SkillSearchRuntime`` 暴露 DE-34 控制器的
   create/status/pause/resume/cancel/locate_failure/detail 契约——状态/预算/
   逐 Case 得失/差异/下一动作/认证五状态/证据全部按同一 search ID 输出，
   Agent 定位失败不需要读巨大 Trace（证据用引用：trace ref、attempt id、
   回执/产物路径与 sha256，不复制轨迹正文）。CLI 与 server API 共用本模块
   同一函数（``server/app.py`` /api/skills 与 /api/skills/search/action）。
2. **公平对照框架（S4/step2）**：``run_fair_comparison`` 在同任务（同一冻结
   Case 集）、同模型（同一合成执行器 adapter 与每次调用用量）、同预算上限下
   对照原生基线（单冠军不动 / 局部变体单轮 dev 选优）与新策略（DE-33 互补池
   + DE-32 有界编辑 + 成功保护）。**安全门不做放开组**：安全/权限检查在全部
   臂恒开启（``SAFETY_GATES_ALWAYS_ON``），不作为实验变量；实际总成本逐臂
   并列披露，任一臂超限即 Later 停止不超支。
3. **发布门（S2）**：``release_gate`` 组合断言——跨能力非回退（修好一题不得
   破坏另一题）+ V 验证 + ``certification.promotion_gate``（搜索报告不晋级
   tier）。探索允许留存（互补/不可比候选留在池中），正式交付按发布门否决。
4. **兼容矩阵（step4）**：``verify_compat_matrix`` 把
   docs/digital-employee/NATIVE-ARCHITECTURE-INTEGRATION.md §10 表逐行映射为
   真实回归文件与测试节点存在性核对；``native_paths_with_search_disabled``
   做新组件关闭组合实测（在线抽取/tier/冠军门/优化器默认关闭原路径可用）。
5. **双线报告（step5）**：``build_dual_line_report`` 输出基础试点与搜索增强
   两条验收结果：无预算/无真实样本/收益未证 → 搜索线 ``not_executed`` 且
   生产开关保持关闭；已实现/部分/未执行/阻塞分开；基础通过不使 DE-29～36
   自动 done。``compare_suite_statistics`` 提供回归前后统计对照表结构。

离线合成演示机制：全部对照用确定性合成模型（文本关键词打分），真实模型实验
（预算/审核真值/隔离执行环境）单列 blockedOn；本模块输出不构成启用生产搜索
的依据（收益未证保持关闭）。

用法：
  python eval/skill_search_eval.py --demo [--out PATH]
  python eval/skill_search_eval.py --dual-report --out PATH
  python eval/skill_search_eval.py --matrix-check --out PATH
  python eval/skill_search_eval.py --list
  python eval/skill_search_eval.py --detail --search-id s-xxxx
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agents.skill_search import certification as cert  # noqa: E402
from agents.skill_search import contracts as sc  # noqa: E402
from agents.skill_search import controller as ct  # noqa: E402
from agents.skill_search import evaluator as ev  # noqa: E402
from agents.skill_search import modules as sm  # noqa: E402
from agents.skill_search import pool as pl  # noqa: E402
from agents.skill_search import protocol as sp  # noqa: E402
from agents.skill_search import selection as sel  # noqa: E402
from agents.skill_search import store as st  # noqa: E402
from agents.skill_search.certification import promotion_gate  # noqa: E402
from agents.skill_search.contracts import build_search_manifest, freeze_protocol  # noqa: E402
from agents.skill_search.controller import ControllerConfig, SearchController  # noqa: E402

EVAL_VERSION = "de36/skill-search-eval/v1"
SEARCH_LINE_TASKS = ("DE-29", "DE-30", "DE-31", "DE-32", "DE-33", "DE-34", "DE-35", "DE-36")
# 安全/权限检查不是实验变量：对照框架任何臂都不提供关闭安全门的选项。
SAFETY_GATES_ALWAYS_ON = True
STRATEGY_NATIVE_SINGLE_CHAMPION = "native_single_champion"
STRATEGY_NATIVE_LOCAL_VARIANTS = "native_local_variants"
STRATEGY_SEARCH_POOL_BOUNDED = "search_pool_bounded_guarded"


# ═══════════════════════════════════════════════════════════════════════════
# §1 搜索运行面（S1：Agent 可创建/查状态/暂停恢复取消并定位失败）
# ═══════════════════════════════════════════════════════════════════════════


def default_search_base() -> Path:
    """搜索命名空间根（跟随既有进化台账根，不另造第二份调度真相）。"""
    return cert.adoption_index_path().parent


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def find_searches(base_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """扫描全部搜索命名空间（<base>/<tenant>/<space>/<lineage>/<search_id>/state.json）。

    只读；坏文件如实标 unreadable（不猜状态、不跳过不报）。
    """
    base = Path(base_dir) if base_dir else default_search_base()
    out: list[dict[str, Any]] = []
    if not base.is_dir():
        return out
    for state_path in sorted(base.glob("*/*/*/*/state.json")):
        namespace = state_path.parent
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            out.append({
                "search_id": "",
                "namespace": str(namespace),
                "state_sha256": _sha256_file(state_path),
                "readable": False,
                "error": exc.__class__.__name__,
            })
            continue
        if not isinstance(data, dict):
            out.append({
                "search_id": "",
                "namespace": str(namespace),
                "state_sha256": _sha256_file(state_path),
                "readable": False,
                "error": "state_not_dict",
            })
            continue
        out.append({
            "search_id": str(data.get("search_id") or ""),
            "tenant": str(data.get("tenant") or ""),
            "space": str(data.get("space") or ""),
            "lineage": str(data.get("lineage") or ""),
            "status": str(data.get("status") or ""),
            "phase": str(data.get("phase") or ""),
            "namespace": str(namespace),
            "state_sha256": _sha256_file(state_path),
            "readable": True,
        })
    return out


def open_store(search_id: str, base_dir: str | Path | None = None) -> st.SearchStore:
    """按 search ID 打开状态句柄（命名空间由落盘状态定位，不猜目录）。"""
    sid = str(search_id or "").strip()
    if not sid:
        raise ValueError("search_id_required")
    base = Path(base_dir) if base_dir else default_search_base()
    for state_path in sorted(base.glob("*/*/*/*/state.json")):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and str(data.get("search_id") or "") == sid:
            return st.SearchStore(state_path.parent)
    raise FileNotFoundError(f"search_state_not_found:{sid}")


def _flag_with_retry(search_id: str, flag: str, note: str, *, base_dir=None) -> bool:
    """跨进程请求暂停/取消/恢复：CAS 重试写状态文档（与控制器同语义）。"""
    store = open_store(search_id, base_dir)
    last_conflict: Exception | None = None
    for _ in range(4):
        try:
            store.load()
            state = store.state
            if state is None:
                return False
            if flag == "clear_pause":
                state["pause_requested"] = False
                state["pause_reason"] = ""
            else:
                state[flag] = True
                if flag == "cancel_requested":
                    state["cancel_note"] = note
                else:
                    state["pause_reason"] = note
            store.save()
            return True
        except st.StoreConflict as exc:
            last_conflict = exc
            store.reload()
            continue
    if last_conflict is not None:
        raise last_conflict
    return False


def _flag_on_store(store: "st.SearchStore", flag: str, note: str) -> bool:
    """在已挂载控制器的 store 上写标记（不另开句柄，避免与运行中控制器 CAS 冲突）。"""
    for _ in range(4):
        try:
            if store.state is None:
                store.load()
            state = store.state
            if state is None:
                return False
            if flag == "clear_pause":
                state["pause_requested"] = False
                state["pause_reason"] = ""
            else:
                state[flag] = True
                if flag == "cancel_requested":
                    state["cancel_note"] = note
                else:
                    state["pause_reason"] = note
            store.save()
            return True
        except st.StoreConflict:
            store.reload()
            continue
    return False


def infer_next_action(state: dict[str, Any], *, can_publish: bool = False) -> dict[str, str]:
    """下一动作推断（只读纯函数）：状态/阶段 → Agent 可直接执行的动作与原因。"""
    status = str(state.get("status") or "")
    phase = str(state.get("phase") or "")
    cleanup_blockers = list((state.get("cleanup") or {}).get("release_blockers") or [])
    table = {
        st.STATUS_PAUSED: ("resume", "已暂停：清除暂停标记后 resume，从原阶段断点续跑"),
        st.STATUS_BLOCKED_BUDGET: (
            "add_budget_then_resume",
            "预算耗尽是 Later：补充预算（set_limits）后 resume，不计失败次数",
        ),
        st.STATUS_BLOCKED_ENVIRONMENT: (
            "restore_environment_then_resume",
            "执行环境阻塞：恢复隔离环境后 resume",
        ),
        st.STATUS_BLOCKED_SOURCE_CHANGED: (
            "reopen_new_search",
            "种子源已修订：旧搜索的分数不得续用，重开新搜索（不 rebase）",
        ),
        st.STATUS_BLOCKED_PROTOCOL_DRIFT: (
            "refreeze_protocol_then_reopen",
            "协议/Case 漂移：重新冻结协议后重开新搜索（跨协议分数不可混用）",
        ),
        st.STATUS_CANCELLED: ("reopen_new_search", "已取消（终态）：证据保留，需要时重开新搜索"),
        st.STATUS_FAILED: ("inspect_then_reopen", "失败（终态）：先 locate_failure 看逐 Case 失败清单，再决定重开"),
        st.STATUS_CLEANUP_FAILED: (
            "fix_release_blockers",
            "清理失败（非 completed）：处理 release_blockers 后重新收尾，can_publish=False 阻断发布",
        ),
    }
    if phase == st.PHASE_COMPLETED and status in {st.PHASE_COMPLETED, st.STATUS_RUNNING}:
        if can_publish:
            return {
                "code": "request_certification",
                "why": "已完成且隔离恢复回执有效：走 DE-35 认证（锁定候选 → 认证 → 明确采用）",
            }
        if cleanup_blockers:
            return {"code": "fix_release_blockers", "why": "已完成但发布阻断：" + "、".join(cleanup_blockers)}
        return {"code": "review_finalization", "why": "已完成：复核最终验证与冠军后再决定认证"}
    if status == st.STATUS_RUNNING:
        return {"code": "continue_run", "why": f"运行中：调用 run/resume 从断点续跑（当前阶段 {phase or 'draft'}）"}
    code, why = table.get(status, ("inspect_state", f"未知状态 {status or '∅'}：先查状态文档"))
    return {"code": code, "why": why}


def _bucket_scores(bucket: dict[str, Any]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for case_id, entry in (bucket or {}).items():
        if not isinstance(entry, dict):
            continue
        outcome = entry.get("outcome")
        if isinstance(outcome, dict) and entry.get("status") == ct.CASE_DONE:
            try:
                scores[str(case_id)] = float(outcome.get("score") or 0.0)
            except (TypeError, ValueError):
                continue
    return scores


def locate_failure(search_id: str, *, base_dir=None, trace_store: Any = None) -> dict[str, Any]:
    """定位失败：逐 Case 失败清单 + 证据引用（trace ref / attempt id / 产物），不复制轨迹正文。

    ``trace_store``（DE-31 引用读取边界）可选：提供时按 ``trace://<case_id>``
    引用原轨迹（只在引用确实存在时列出，不复制正文）；缺省只给执行回执引用。
    """
    store = open_store(search_id, base_dir)
    store.load()
    state = store.state or {}
    failures: list[dict[str, Any]] = []
    buckets = (
        ("baseline_evaluating", (state.get("baseline") or {}).get("cases") or {}),
        ("searching", (state.get("round") or {}).get("cases") or {}),
    )
    config_refs = dict((state.get("config") or {}).get("trace_refs") or {})
    for stage, bucket in buckets:
        for case_id, entry in bucket.items():
            if not isinstance(entry, dict):
                continue
            outcome = entry.get("outcome") if isinstance(entry.get("outcome"), dict) else {}
            terminal = str(outcome.get("terminal_status") or "")
            done = entry.get("status") == ct.CASE_DONE
            failed = (not done) or terminal != ev.TERMINAL_PASS
            if not failed:
                continue
            evidence: list[dict[str, str]] = []
            ref = str(config_refs.get(str(case_id)) or f"trace://{case_id}")
            resolve = getattr(trace_store, "resolve", None)
            if callable(resolve) and resolve(ref) is not None:
                evidence.append({"kind": "trace_ref", "ref": ref})
            if outcome.get("response_ref"):
                evidence.append({"kind": "response_ref", "ref": str(outcome.get("response_ref"))})
            failures.append({
                "stage": stage,
                "case_id": str(case_id),
                "status": str(entry.get("status") or ""),
                "terminal_status": terminal,
                "score": outcome.get("score"),
                "attempts": int(entry.get("attempts") or 0),
                "last_error": str(entry.get("last_error") or ""),
                "evidence": evidence,
            })
    pool = None
    try:
        pool = store.load_pool(search_id=str(state.get("search_id") or ""))
    except Exception:
        pool = None
    failed_attempts = []
    if pool is not None:
        for attempt in pool.attempts:
            if str(attempt.get("status") or "") == pl.ATTEMPT_FAILED:
                failed_attempts.append({
                    "attempt_id": int(attempt.get("attempt_id") or 0),
                    "candidate_id": str(attempt.get("candidate_id") or ""),
                    "reason": str(attempt.get("reason") or ""),
                })
    summary: dict[str, Any] = {
        "ok": True,
        "search_id": str(state.get("search_id") or search_id),
        "failures": failures,
        "failed_attempts": failed_attempts,
        "next_action": infer_next_action(state),
        "note": "证据为引用（trace/response/attempt），完整轨迹留在原 Trace 存储与命名空间产物内，不在本报告复制正文",
    }
    if str(state.get("status") or "") == st.STATUS_FAILED:
        summary["block_reason"] = str(state.get("block_reason") or "")
    return summary


def case_gains_report(state: dict[str, Any], *, pool: pl.CandidatePool | None = None) -> dict[str, dict[str, Any]]:
    """逐 Case 得失：基线分 → 池内最优分（含 delta），供详情与前端直接渲染。"""
    baseline = _bucket_scores((state.get("baseline") or {}).get("cases") or {})
    best = dict(baseline)
    best_candidate: dict[str, str] = {}
    if pool is not None:
        for record in pool.candidates:
            vector = record.get("score_vector") if isinstance(record.get("score_vector"), dict) else {}
            for case_id, raw in vector.items():
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if value > best.get(str(case_id), float("-inf")):
                    best[str(case_id)] = value
                    best_candidate[str(case_id)] = str(record.get("candidate_id") or "")
    out: dict[str, dict[str, Any]] = {}
    for case_id in sorted(set(baseline) | set(best)):
        base = baseline.get(case_id)
        top = best.get(case_id)
        out[case_id] = {
            "baseline": base,
            "best": top,
            "delta": None if (base is None or top is None) else round(top - base, 9),
            "best_candidate": best_candidate.get(case_id, ""),
        }
    return out


def diffs_report(state: dict[str, Any], *, pool: pl.CandidatePool | None = None) -> dict[str, Any]:
    """差异：轮次 patch 元数据 + 冠军候选的相对种子累计修改量与制品 hash。"""
    patches: list[dict[str, Any]] = []
    active_patch = (state.get("round") or {}).get("patch_meta")
    if isinstance(active_patch, dict) and active_patch:
        patches.append(dict(active_patch))
    for done in state.get("completed_rounds") or []:
        meta = (done or {}).get("patch_meta") if isinstance(done, dict) else None
        if isinstance(meta, dict) and meta:
            patches.append(dict(meta))
    champion_id = str((state.get("finalization") or {}).get("champion") or "")
    champion_meta: dict[str, Any] = {}
    if pool is not None and champion_id:
        record = pool.get_candidate(champion_id)
        if record is not None:
            champion_meta = {
                "candidate_id": champion_id,
                "content_hash": str(record.get("content_hash") or ""),
                "cumulative_seed_diff": record.get("cumulative_seed_diff"),
            }
    return {
        "patches": patches,
        "champion": champion_meta,
        "note": "候选正文在搜索命名空间内（candidates/<id>/），冻结源目录只读：差异不写回投影或 active",
    }


def evidence_report(store: st.SearchStore) -> dict[str, Any]:
    """证据：命名空间路径 + 文件 sha256 + 池 digest + 回执清单（同一 search ID 可复核）。"""
    namespace = store.namespace
    receipts: list[dict[str, str]] = []
    receipts_dir = namespace / "receipts"
    if receipts_dir.is_dir():
        for path in sorted(receipts_dir.glob("*.json")):
            receipts.append({"name": path.name, "sha256": _sha256_file(path)})
    pool_digest = ""
    try:
        pool = st.SearchStore(namespace).load_pool()
        pool_digest = pool.content_digest() if pool is not None else ""
    except Exception:
        pool_digest = ""
    state_sha = _sha256_file(store.state_path) if store.state_path.is_file() else ""
    return {
        "namespace": str(namespace),
        "state_sha256": state_sha,
        "pool_digest": pool_digest,
        "receipts": receipts,
    }


def build_detail_report(search_id: str, *, base_dir=None) -> dict[str, Any]:
    """详情报告（CLI 与 server API 共用同一函数、同一 search ID 与证据）：

    状态 / 预算 / 逐 Case 得失 / 差异 / 下一动作 / 认证五状态 / 证据。只读。
    """
    store = open_store(search_id, base_dir)
    store.load()
    state = store.state
    if state is None:
        raise FileNotFoundError(f"search_state_not_found:{search_id}")
    pool = None
    try:
        pool = store.load_pool(search_id=str(state.get("search_id") or ""))
    except Exception:
        pool = None
    can_publish = bool((state.get("cleanup") or {}).get("can_publish"))
    skill_name = str(state.get("lineage") or "")
    costs = store.cost_summary()
    reserved_total = sum(int((v or {}).get("reserved_tokens") or 0) for v in costs.values())
    used_total = sum(int((v or {}).get("used_tokens") or 0) for v in costs.values())
    return {
        "kind": "skill-search-detail",
        "eval_version": EVAL_VERSION,
        "search_id": str(state.get("search_id") or ""),
        "skill": skill_name,
        "tenant": str(state.get("tenant") or ""),
        "space": str(state.get("space") or ""),
        "state": {
            "phase": str(state.get("phase") or ""),
            "status": str(state.get("status") or ""),
            "round_index": (state.get("round") or {}).get("index"),
            "round_phase": (state.get("round") or {}).get("phase"),
            "generation": state.get("generation"),
            "cancel_requested": bool(state.get("cancel_requested")),
            "pause_requested": bool(state.get("pause_requested")),
            "pause_reason": str(state.get("pause_reason") or ""),
            "cancel_note": str(state.get("cancel_note") or ""),
            "can_publish": can_publish,
            "release_blockers": list((state.get("cleanup") or {}).get("release_blockers") or []),
        },
        "budget": {
            "categories": costs,
            "reserved_tokens_total": reserved_total,
            "used_tokens_total": used_total,
            "open_reservations": len(state.get("open_reservations") or {}),
        },
        "case_gains": case_gains_report(state, pool=pool),
        "diffs": diffs_report(state, pool=pool),
        "next_action": infer_next_action(state, can_publish=can_publish),
        "certification": cert.certification_state(skill_name) if skill_name else {},
        "evidence": evidence_report(store),
        "pool_size": len(pool.candidates) if pool is not None else 0,
        "attempts": len(pool.attempts) if pool is not None else 0,
    }


def skill_search_summary(skill_name: str, *, base_dir=None) -> dict[str, Any]:
    """Skill 详情接线（server /api/skills 与前端 Skill 抽屉共用）：

    按技能名（lineage）定位搜索命名空间，输出轻量摘要；完整详情用 search_id
    调 build_detail_report（CLI/API 引用同一 search ID 与证据）。
    """
    name = str(skill_name or "").strip()
    if not name:
        return {"found": False}
    base = Path(base_dir) if base_dir else default_search_base()
    matches = [
        item
        for item in find_searches(base)
        if item.get("readable") and str(item.get("lineage") or "") == name
    ]
    if not matches:
        # 认证索引补充：已有 champion/证书/采用记录的技能即使 state 缺失也可定位
        index_entry = cert._index_entry_for(name)
        if index_entry.get("search_id"):
            return {"found": True, "search_id": str(index_entry.get("search_id")), "summary": {}, "via": "adoption-index"}
        return {"found": False}
    latest = matches[-1]
    try:
        detail = build_detail_report(str(latest["search_id"]), base_dir=base)
    except FileNotFoundError:
        return {"found": True, "search_id": str(latest["search_id"]), "summary": {}, "via": "namespace"}
    active_state = next(
        (s["state"] for s in detail["certification"].get("states", {}).values() if s.get("active")),
        "",
    )
    return {
        "found": True,
        "search_id": detail["search_id"],
        "via": "namespace",
        "summary": {
            "status": detail["state"]["status"],
            "phase": detail["state"]["phase"],
            "next_action": detail["next_action"],
            "budget_tokens": detail["budget"]["used_tokens_total"],
            "certification_state": active_state,
        },
    }


@dataclass
class SearchComponents:
    """搜索运行组件（真实模型路径另行 blockedOn；离线为合成端口注入）。"""

    executor: Any
    budget: Any
    attributor: Any = None
    patch_provider: Any = None
    judge: Any = None
    isolation: Any = None
    trace_store: Any = None
    config: ControllerConfig | None = None
    clock: Callable[[], float] | None = None


class SkillSearchRuntime:
    """DE-34 控制器的 Agent/CLI 运行面：create / status / pause / resume / cancel。

    - status/detail：只读（落盘状态文档），跨进程安全；
    - pause/cancel：CAS 写标记（跨进程安全；运行中的控制器在阶段边界停下）；
    - resume：清除暂停标记；持有运行组件时直接从断点续跑（run），未持有组件时
      报告 resume_pending（组件化重启后 attach 续跑，不重抽父代/不重扣费）。
    """

    def __init__(self, *, base_dir: str | Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else None
        self._attached: dict[str, SearchController] = {}
        self._trace_stores: dict[str, Any] = {}

    def create_search(
        self,
        *,
        skill_dir: str | Path,
        cases: list[dict[str, Any]],
        tenant: str = "",
        space: str = "",
        module_defs: list[dict[str, Any]] | None = None,
        components: SearchComponents,
        search_id: str | None = None,
        worker: str = "agent",
    ) -> dict[str, Any]:
        """冻结种子 → 冻结协议 → manifest → 控制器 start（复用 DE-29/30/34 原语）。"""
        raw_text = (Path(skill_dir) / sc.SKILL_TEXT_NAME).read_text(encoding="utf-8-sig")
        spec_out = sm.build_module_spec(raw_text, module_defs or [], skill_name="")
        if not spec_out.get("ok"):
            return {"ok": False, "error": "module_spec_failed", "detail": spec_out}
        assigned = sp.assign_protocol_roles(cases)
        if not assigned.get("ok"):
            return {"ok": False, "error": "protocol_roles_rejected", "detail": assigned}
        freeze_out = sp.freeze_case_protocol(assigned["cases"])
        if not freeze_out.get("ok"):
            return {"ok": False, "error": "protocol_freeze_failed", "detail": freeze_out}
        freeze = freeze_out["freeze"]
        manifest_out = build_search_manifest(
            skill_dir=skill_dir,
            tenant=tenant,
            space=space,
            protocol=freeze_protocol(
                splits=sp.protocol_freeze_entry(freeze),
                algorithm_version=EVAL_VERSION,
                models={"evaluator": "synthetic", "reflection": "synthetic"},
            ),
            module_spec=spec_out["spec"],
            search_id=search_id,
        )
        if not manifest_out.get("ok"):
            return {"ok": False, "error": str(manifest_out.get("error") or "manifest_rejected"), "detail": manifest_out}
        manifest = manifest_out["manifest"]
        sc.save_manifest(manifest, base_dir=self.base_dir)
        config = components.config or ControllerConfig(
            prerequisites={key: True for key in ct.REQUIRED_PREREQUISITES}
        )
        scope_guard = None
        if os.environ.get("EVOHARNESS_SCOPE_GUARD", "").strip().lower()                 in {"1", "true", "on", "yes"}:
            from agents.skill_search.scope_guard import build_scope_guard

            scope_guard = build_scope_guard()
        controller = SearchController(
            manifest=manifest,
            scope_guard=scope_guard,
            cases=assigned["cases"],
            freeze=freeze,
            module_spec=spec_out["spec"],
            executor=components.executor,
            budget=components.budget,
            config=config,
            base_dir=self.base_dir,
            attributor=components.attributor,
            patch_provider=components.patch_provider,
            judge=components.judge,
            isolation=components.isolation,
            trace_store=components.trace_store,
            clock=components.clock,
        )
        # 先挂载再启动：启动中途故障（FaultInjected）后运行面仍持有控制器可 resume
        self._attached[str(manifest.search_id)] = controller
        if components.trace_store is not None:
            self._trace_stores[str(manifest.search_id)] = components.trace_store
        result = controller.start(worker=worker)
        return {"ok": bool(result.get("ok")), "search_id": str(manifest.search_id), "result": result}

    def status(self, search_id: str) -> dict[str, Any]:
        store = open_store(search_id, self.base_dir)
        store.load()
        state = store.state or {}
        can_publish = bool((state.get("cleanup") or {}).get("can_publish"))
        return {
            "ok": True,
            "search_id": str(state.get("search_id") or search_id),
            "phase": str(state.get("phase") or ""),
            "status": str(state.get("status") or ""),
            "round_phase": (state.get("round") or {}).get("phase"),
            "next_action": infer_next_action(state, can_publish=can_publish),
            "cancel_requested": bool(state.get("cancel_requested")),
            "pause_requested": bool(state.get("pause_requested")),
        }

    def pause(self, search_id: str, note: str = "") -> dict[str, Any]:
        controller = self._attached.get(str(search_id))
        if controller is not None:
            changed = _flag_on_store(controller.store, "pause_requested", note or "pause_requested")
        else:
            changed = _flag_with_retry(search_id, "pause_requested", note or "pause_requested", base_dir=self.base_dir)
        return {"ok": changed, "action": "pause", "search_id": str(search_id)}

    def cancel(self, search_id: str, note: str = "") -> dict[str, Any]:
        controller = self._attached.get(str(search_id))
        if controller is not None:
            changed = _flag_on_store(controller.store, "cancel_requested", note or "cancel_requested")
        else:
            changed = _flag_with_retry(search_id, "cancel_requested", note or "cancel_requested", base_dir=self.base_dir)
        return {"ok": changed, "action": "cancel", "search_id": str(search_id)}

    def resume(self, search_id: str, note: str = "", *, worker: str = "agent") -> dict[str, Any]:
        """恢复：清除暂停标记（CAS），持有组件则直接从断点续跑。"""
        controller = self._attached.get(str(search_id))
        if controller is not None:
            store = controller.store
            if store.state is None:
                store.load()
            if store.state is not None and not store.state.get("pause_requested"):
                return {"ok": False, "error": "not_paused", "search_id": str(search_id)}
            _flag_on_store(store, "clear_pause", "")
            result = controller.run(worker=worker)
            return {"ok": True, "action": "resume", "search_id": str(search_id), "result": result}
        _flag_with_retry(str(search_id), "clear_pause", "", base_dir=self.base_dir)
        return {
            "ok": True,
            "action": "resume",
            "search_id": str(search_id),
            "resume_pending": True,
            "note": "暂停标记已清除：需持有运行组件的运行器（CLI/worker）从断点续跑；不重抽父代、不重扣费",
        }

    def run(self, search_id: str, *, worker: str = "agent") -> dict[str, Any]:
        """worker 入口：从当前断点推进（不改变暂停/取消标记；标记由 run 语义处理）。"""
        controller = self._attached.get(str(search_id))
        if controller is None:
            return {
                "ok": False,
                "error": "not_attached",
                "search_id": str(search_id),
                "note": "跨进程推进需持有运行组件的运行器（CLI/worker）重新 attach 后 run",
            }
        try:
            result = controller.run(worker=worker)
        except st.StoreConflict:
            # 外部进程写入了暂停/取消等标记（generation 已前进）：重读后从断点续跑，
            # 新读到的标记按 run 语义生效（不覆盖、不丢外部操作者意图）。
            controller.store.reload()
            result = controller.run(worker=worker)
        return {"ok": True, "action": "run", "search_id": str(search_id), "result": result}

    def locate_failure(self, search_id: str) -> dict[str, Any]:
        return locate_failure(
            search_id,
            base_dir=self.base_dir,
            trace_store=self._trace_stores.get(str(search_id)),
        )

    def detail(self, search_id: str) -> dict[str, Any]:
        return build_detail_report(search_id, base_dir=self.base_dir)


# ═══════════════════════════════════════════════════════════════════════════
# §2 发布门与跨能力回退场景（S2：探索留存，正式交付按发布门否决）
# ═══════════════════════════════════════════════════════════════════════════


def release_gate(
    *,
    candidate_id: str,
    per_case_scores: dict[str, float],
    seed_scores: dict[str, float],
    v_results: dict[str, float] | None = None,
    epsilon: float = pl.SCORE_EPSILON,
) -> dict[str, Any]:
    """正式交付发布门（certification/promotion_gate 组合断言，fail-closed）。

    - 跨能力非回退（成功保护）：任一 Case 相对种子回退超过容差 → 否决
      （"修好一题破坏另一题"在交付面被明确拒绝，探索池可留存该候选）；
    - V 验证：最终验证 Case 未全通过 → 否决（v_results 为空表示未执行 → 否决）；
    - tier 晋级门：搜索报告（eval_source=skill_search）一律被
      ``certification.promotion_gate`` 拒绝 —— 池/认证结果只是候选资格，
      tier 晋级仍需真实线上成绩（搜索线与 tier 门正交，恒记录不通过）。
    """
    v_results = dict(v_results or {})
    checks: dict[str, Any] = {}
    regressions = {
        case_id: {"seed": seed_scores.get(case_id), "candidate": value}
        for case_id, value in sorted(per_case_scores.items())
        if case_id in seed_scores and float(value) < float(seed_scores[case_id]) - float(epsilon)
    }
    checks["cross_capability_non_regression"] = {
        "ok": not regressions,
        "regressions": regressions,
        "why": "任一 Case 相对种子回退 → 正式交付否决（探索允许留存，交付不允许）",
    }
    v_failed = {case_id: value for case_id, value in sorted(v_results.items()) if float(value) < 1.0}
    checks["v_verification_passed"] = {
        "ok": bool(v_results) and not v_failed,
        "failed": v_failed,
        "why": "无最终验证结果视为未验证（fail-closed），不得冒称已提升",
    }
    gate_item = {
        "artifacts": {
            "eval_source": cert.EVAL_SOURCE_SEARCH,
            cert.SEARCH_POOL_REPORT_FIELD: {"candidate_id": str(candidate_id)},
        }
    }
    tier_gate = promotion_gate(gate_item, "de36-release-gate-check")
    checks["tier_promotion_gate"] = {
        # 搜索报告永远不能作为 tier 晋级证据：该检查恒"不通过"（正交门，非缺陷）
        "ok": bool(tier_gate.get("ok")),
        "code": str(tier_gate.get("code") or ""),
        "why": str(tier_gate.get("why") or ""),
        "orthogonal": True,
    }
    ok = checks["cross_capability_non_regression"]["ok"] and checks["v_verification_passed"]["ok"]
    result: dict[str, Any] = {
        "ok": ok,
        "candidate_id": str(candidate_id),
        "checks": checks,
        "rejected_reason": "",
    }
    if not ok:
        if not checks["cross_capability_non_regression"]["ok"]:
            result["rejected_reason"] = "cross_capability_regression"
        else:
            result["rejected_reason"] = "v_verification_failed"
    return result


@dataclass
class ComparisonCase:
    case_id: str
    trigger: str
    expected_keywords: tuple[str, ...] = ()


@dataclass
class BoundedEdit:
    """有界编辑（DE-32 谱系）：单步 replace 操作，标注改进/破坏的 Case 与安全性。"""

    edit_id: str
    find: str
    replace: str
    fixes: tuple[str, ...] = ()
    breaks: tuple[str, ...] = ()
    unsafe: bool = False


def build_regression_scenario() -> dict[str, Any]:
    """S2 跨能力回退场景（合成）：修好一题破坏另一题。

    种子通过 case-2（发布摘要回填）、失败 case-1（灰度比例）；
    回退候选一次有界替换同时改两处：修好 case-1、删掉发布摘要句破坏 case-2。
    池允许其留存（不可比 → 入池，探索保留），交付面由 release_gate 否决。
    """
    seed_text = (
        "## 工作流\n\n"
        "1. 灰度 5% 观察 10 分钟。\n"
        "2. 全量并回填结果，并生成发布摘要。\n"
    )
    regressor = BoundedEdit(
        edit_id="canary-drop-summary",
        find="1. 灰度 5% 观察 10 分钟。\n2. 全量并回填结果，并生成发布摘要。",
        replace="1. 灰度 10% 观察 10 分钟。\n2. 全量（已达标，无需回填）。",
        fixes=("case-1",),
        breaks=("case-2",),
    )
    good_fix = BoundedEdit(
        edit_id="fix-canary",
        find="灰度 5% 观察 10 分钟。",
        replace="灰度 10% 观察 10 分钟。",
        fixes=("case-1",),
    )
    cases = [
        ComparisonCase(case_id="case-1", trigger="deploy canary ratio", expected_keywords=("灰度 10%",)),
        ComparisonCase(case_id="case-2", trigger="deploy summary backfill", expected_keywords=("发布摘要",)),
    ]
    model = DeterministicSyntheticModel()
    seed_scores = model.score_vector(seed_text, cases)
    pool = pl.CandidatePool(search_id="de36-regression-scenario", case_ids=[c.case_id for c in cases])
    pool.register_seed(
        candidate_id="seed",
        score_vector=dict(seed_scores),
        content_hash=_sha256_bytes(seed_text.encode("utf-8")),
        protocol_hash="scenario-protocol",
    )
    texts = {"seed": seed_text}
    for edit in (regressor, good_fix):
        text = seed_text.replace(edit.find, edit.replace, 1)
        scores = model.score_vector(text, cases)
        decision = pool.admit(
            candidate_id=edit.edit_id,
            parent_id="seed",
            score_vector=scores,
            content_hash=_sha256_bytes(text.encode("utf-8")),
            protocol_hash="scenario-protocol",
            cumulative_seed_diff={"added_bytes": len(edit.replace) - len(edit.find), "ops": 1, "generations": 1},
        )
        if decision.get("ok"):
            texts[edit.edit_id] = text
    return {
        "seed_text": seed_text,
        "cases": cases,
        "seed_scores": seed_scores,
        "regressor": regressor,
        "good_fix": good_fix,
        "pool": pool,
        "texts": texts,
        "model": model,
    }


# ═══════════════════════════════════════════════════════════════════════════
# §3 公平对照框架（S4/step2：同任务/同模型/同实际总成本）
# ═══════════════════════════════════════════════════════════════════════════


class ComparisonLedger:
    """对照预算账本（DE-15 语义的框架内最小实现）：同上限、先记账后调用。"""

    def __init__(self, *, strategy: str, cap_tokens: int):
        self.strategy = str(strategy)
        self.cap_tokens = int(cap_tokens)
        self.spent = 0
        self.by_category: dict[str, int] = {}
        self.calls = 0
        self.blocked_later = False
        self.timeline: list[dict[str, Any]] = []

    def spend(self, category: str, tokens: int) -> bool:
        """预留并结算一笔调用成本；超限 → Later 停止（不超支、不计失败）。"""
        if self.spent + int(tokens) > self.cap_tokens:
            self.blocked_later = True
            return False
        self.spent += int(tokens)
        self.by_category[str(category)] = self.by_category.get(str(category), 0) + int(tokens)
        self.calls += 1
        return True

    def mark_timeline(self, best_average: float) -> None:
        self.timeline.append({"calls": self.calls, "cumulative_tokens": self.spent, "best_average": round(best_average, 9)})

    def summary(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "cap_tokens": self.cap_tokens,
            "actual_tokens": self.spent,
            "calls": self.calls,
            "by_category": dict(self.by_category),
            "blocked_later": self.blocked_later,
            "timeline": [dict(point) for point in self.timeline],
        }


class DeterministicSyntheticModel:
    """确定性合成"模型"：文本关键词打分，全部臂共用（同模型/同每次调用用量）。"""

    adapter_id = "synthetic-de36"

    def __init__(self, *, usage_per_call: int = 5):
        self.usage_per_call = int(usage_per_call)
        self.call_count = 0

    def score_text(self, text: str, case: ComparisonCase) -> float:
        self.call_count += 1
        if not case.expected_keywords:
            return 0.2
        hit = all(keyword in str(text) for keyword in case.expected_keywords)
        return 1.0 if hit else 0.2

    def score_vector(self, text: str, cases: list[ComparisonCase]) -> dict[str, float]:
        return {case.case_id: self.score_text(text, case) for case in cases}


def check_edit_safety(edit: BoundedEdit) -> dict[str, Any]:
    """安全门（恒开启，非实验变量）：unsafe 编辑一律拒绝，任何臂都无关闭开关。"""
    if not SAFETY_GATES_ALWAYS_ON:  # pragma: no cover - 恒真；框架不提供关闭开关
        raise RuntimeError("safety_gates_must_be_always_on")
    if edit.unsafe:
        return {"ok": False, "reason": "safety_violation", "why": "编辑触碰安全守卫内容：安全门恒开启拒绝"}
    return {"ok": True, "reason": ""}


def _apply_edit(text: str, edit: BoundedEdit) -> str:
    if edit.find and edit.find in text:
        return text.replace(edit.find, edit.replace, 1)
    return text


def _verify_on_v(model: DeterministicSyntheticModel, text: str, v_cases: list[ComparisonCase], ledger: ComparisonLedger) -> dict[str, float]:
    for _case in v_cases:
        if not ledger.spend("final_verification", model.usage_per_call):
            ledger.blocked_later = True
            return {}
    return model.score_vector(text, v_cases)


def _arm_result(strategy: str, ledger: ComparisonLedger, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"strategy": strategy, "cost": ledger.summary(), "safety_gates": "always_on"}
    out.update(extra)
    return out


def _run_native_single_champion_arm(world: dict[str, Any]) -> dict[str, Any]:
    """原生基线臂一：不动正文的现任冠军（种子）——只花基线评测成本。"""
    model: DeterministicSyntheticModel = world["model"]
    ledger: ComparisonLedger = world["ledgers"][STRATEGY_NATIVE_SINGLE_CHAMPION]
    scores = model.score_vector(world["seed_text"], world["s_cases"])
    for _case in world["s_cases"]:
        if not ledger.spend("baseline", model.usage_per_call):
            ledger.blocked_later = True
            break
    v_results = _verify_on_v(model, world["seed_text"], world["v_cases"], ledger)
    ledger.mark_timeline(sum(scores.values()) / max(1, len(scores)))
    gate = release_gate(candidate_id="seed", per_case_scores=scores, seed_scores=scores, v_results=v_results)
    return _arm_result(
        STRATEGY_NATIVE_SINGLE_CHAMPION,
        ledger,
        delivered_candidate="seed",
        delivered_text=world["seed_text"],
        per_case_scores=scores,
        v_results=v_results,
        release_gate=gate,
        retained_pool=[],
        vetoed=[],
    )


def _run_native_local_variants_arm(world: dict[str, Any]) -> dict[str, Any]:
    """原生基线臂二：局部变体单轮 dev 选优（原生 _build_candidate_eval_bundle_async 语义）。

    一轮内生成全部单步编辑变体 → S 集评测（每次评测计成本）→ (avg, worst) 选优
    → V 复测 → 成功保护对照种子。不做多代组合（原生局部优化是单轮少量变体）。
    """
    model: DeterministicSyntheticModel = world["model"]
    ledger: ComparisonLedger = world["ledgers"][STRATEGY_NATIVE_LOCAL_VARIANTS]
    seed_scores = model.score_vector(world["seed_text"], world["s_cases"])
    for _case in world["s_cases"]:
        if not ledger.spend("baseline", model.usage_per_call):
            ledger.blocked_later = True
            break
    variants: list[dict[str, Any]] = []
    for edit in world["edits"]:
        safety = check_edit_safety(edit)
        if not safety.get("ok"):
            variants.append({"edit": edit, "rejected": safety})
            continue
        text = _apply_edit(world["seed_text"], edit)
        if not ledger.spend("mutation", model.usage_per_call):
            ledger.blocked_later = True
            break
        eval_tokens = model.usage_per_call * len(world["s_cases"])
        if not ledger.spend("execution", eval_tokens):
            ledger.blocked_later = True
            break
        scores = model.score_vector(text, world["s_cases"])
        variants.append({"edit": edit, "text": text, "scores": scores, "rejected": None})
    usable = [v for v in variants if v.get("rejected") is None]
    best: tuple[tuple[float, float], dict[str, Any]] | None = None
    for v in usable:
        scores = v["scores"]
        avg = sum(scores.values()) / max(1, len(scores))
        worst = min(scores.values()) if scores else 0.0
        key = (-avg, -worst)
        if best is None or key < best[0]:
            best = (key, v)
    delivered = best[1] if best is not None else None
    delivered_text = delivered["text"] if delivered else world["seed_text"]
    delivered_scores = dict(delivered["scores"]) if delivered else dict(seed_scores)
    v_results = _verify_on_v(model, delivered_text, world["v_cases"], ledger)
    ledger.mark_timeline(sum(delivered_scores.values()) / max(1, len(delivered_scores)))
    gate = release_gate(
        candidate_id=str(delivered["edit"].edit_id) if delivered else "seed",
        per_case_scores=delivered_scores,
        seed_scores=seed_scores,
        v_results=v_results,
    )
    vetoed = [
        {"candidate_id": v["edit"].edit_id, "reason": v["rejected"]["reason"]}
        for v in variants
        if v.get("rejected") is not None
    ]
    return _arm_result(
        STRATEGY_NATIVE_LOCAL_VARIANTS,
        ledger,
        delivered_candidate=str(delivered["edit"].edit_id) if delivered else "seed",
        delivered_text=delivered_text,
        per_case_scores=delivered_scores,
        v_results=v_results,
        release_gate=gate,
        retained_pool=[],
        vetoed=vetoed,
    )


def _run_search_pool_bounded_arm(world: dict[str, Any]) -> dict[str, Any]:
    """新策略臂：多代互补池（DE-33 CandidatePool）+ 有界编辑 + 成功保护。

    轮 1：单步编辑变体入池（互补/不可比候选留存；安全违规拒绝）；冠军按 DE-33
    tie 链选取。轮 2 起：以当前冠军为父代组合剩余单步编辑（每 patch 有界：单
    replace），直到无改进或预算 Later。最终 V 验证 + 发布门。
    """
    model: DeterministicSyntheticModel = world["model"]
    ledger: ComparisonLedger = world["ledgers"][STRATEGY_SEARCH_POOL_BOUNDED]
    pool: pl.CandidatePool = world["pool"]
    protocol_hash = world["protocol_hash"]
    seed_scores = model.score_vector(world["seed_text"], world["s_cases"])
    for _case in world["s_cases"]:
        if not ledger.spend("baseline", model.usage_per_call):
            ledger.blocked_later = True
            break
    pool.register_seed(
        candidate_id="seed",
        score_vector=dict(seed_scores),
        content_hash=_sha256_bytes(world["seed_text"].encode("utf-8")),
        protocol_hash=protocol_hash,
    )
    vetoed: list[dict[str, Any]] = []
    texts: dict[str, str] = {"seed": world["seed_text"]}
    blocked = False

    for generation in range(1, int(world["max_generations"]) + 1):
        champion = sel.select_search_champion(pool.candidates, case_ids=pool.case_ids)
        if not champion.get("ok"):
            break
        parent_id = str(champion.get("search_champion") or "seed")
        progressed = False
        for edit in world["edits"]:
            safety = check_edit_safety(edit)
            candidate_id = f"{edit.edit_id}-g{generation}"
            if not safety.get("ok"):
                pool.record_failed_attempt(
                    candidate_id=candidate_id,
                    parent_id=parent_id,
                    reason=safety["reason"],
                    evidence=[{"kind": "safety_gate", "why": safety["why"]}],
                )
                vetoed.append({"candidate_id": candidate_id, "reason": safety["reason"]})
                continue
            text = _apply_edit(texts.get(parent_id, world["seed_text"]), edit)
            if not ledger.spend("mutation", model.usage_per_call):
                blocked = True
                break
            if not ledger.spend("execution", model.usage_per_call * len(world["s_cases"])):
                blocked = True
                break
            scores = model.score_vector(text, world["s_cases"])
            decision = pool.admit(
                candidate_id=candidate_id,
                parent_id=parent_id,
                score_vector=scores,
                content_hash=_sha256_bytes(text.encode("utf-8")),
                protocol_hash=protocol_hash,
                cumulative_seed_diff={
                    "added_bytes": len(edit.replace) - len(edit.find),
                    "ops": generation,
                    "generations": generation,
                },
            )
            if decision.get("ok"):
                texts[candidate_id] = text
                progressed = True
        champion = sel.select_search_champion(pool.candidates, case_ids=pool.case_ids)
        best_avg = 0.0
        if champion.get("ok"):
            record = pool.get_candidate(str(champion.get("search_champion") or ""))
            vector = (record or {}).get("score_vector") or {}
            best_avg = sum(float(v) for v in vector.values()) / max(1, len(vector)) if vector else 0.0
        ledger.mark_timeline(best_avg)
        if blocked or not progressed:
            break

    champion = sel.select_search_champion(pool.candidates, case_ids=pool.case_ids)
    if not champion.get("ok"):
        return _arm_result(
            STRATEGY_SEARCH_POOL_BOUNDED,
            ledger,
            delivered_candidate="seed",
            delivered_text=world["seed_text"],
            per_case_scores=dict(seed_scores),
            v_results={},
            release_gate={"ok": False, "rejected_reason": "champion_empty", "checks": {}},
            retained_pool=[],
            vetoed=vetoed,
        )
    champion_id = str(champion.get("search_champion") or "seed")
    champion_text = texts.get(champion_id, world["seed_text"])
    record = pool.get_candidate(champion_id) or {}
    champion_scores = {k: float(v) for k, v in (record.get("score_vector") or {}).items()} or dict(seed_scores)
    v_results = _verify_on_v(model, champion_text, world["v_cases"], ledger)
    ledger.mark_timeline(sum(champion_scores.values()) / max(1, len(champion_scores)))
    gate = release_gate(
        candidate_id=champion_id,
        per_case_scores=champion_scores,
        seed_scores=seed_scores,
        v_results=v_results,
    )
    retained = [
        str(r.get("candidate_id") or "")
        for r in pool.candidates
        if str(r.get("candidate_id") or "") != champion_id
    ]
    return _arm_result(
        STRATEGY_SEARCH_POOL_BOUNDED,
        ledger,
        delivered_candidate=champion_id,
        delivered_text=champion_text,
        per_case_scores=champion_scores,
        v_results=v_results,
        release_gate=gate,
        retained_pool=retained,
        vetoed=vetoed,
        champion_tie_key=champion.get("champion_tie_key") or {},
    )


def build_comparison_world(
    *,
    cases: list[ComparisonCase],
    verify_case_ids: list[str],
    seed_text: str,
    edits: list[BoundedEdit],
    budget_tokens: int,
    usage_per_call: int = 5,
    max_generations: int = 3,
) -> dict[str, Any]:
    """构造同条件对照世界：同 Case/同模型/同预算上限；每臂独立账本。"""
    model = DeterministicSyntheticModel(usage_per_call=usage_per_call)
    verify_ids = set(verify_case_ids)
    s_cases = [c for c in cases if c.case_id not in verify_ids]
    v_cases = [c for c in cases if c.case_id in verify_ids]
    return {
        "cases": list(cases),
        "s_cases": s_cases,
        "v_cases": v_cases,
        "seed_text": seed_text,
        "edits": list(edits),
        "model": model,
        "ledgers": {
            strategy: ComparisonLedger(strategy=strategy, cap_tokens=budget_tokens)
            for strategy in (
                STRATEGY_NATIVE_SINGLE_CHAMPION,
                STRATEGY_NATIVE_LOCAL_VARIANTS,
                STRATEGY_SEARCH_POOL_BOUNDED,
            )
        },
        "pool": pl.CandidatePool(search_id="de36-comparison", case_ids=[c.case_id for c in s_cases]),
        "protocol_hash": _sha256_bytes(json.dumps([c.case_id for c in s_cases]).encode("utf-8")),
        "max_generations": max_generations,
    }


def run_fair_comparison(world: dict[str, Any]) -> dict[str, Any]:
    """执行三臂对照并输出公平性视图（同任务/同模型/同预算上限；实际花费并列）。

    benefit_proven（合成机制层收益）：新策略在同预算上限内交付的任务成功数
    严格多于两个原生臂、发布门通过、且未超支。收益未证 → 生产开关保持关闭；
    真实模型收益另行 blockedOn 实验。
    """
    arms = [
        _run_native_single_champion_arm(world),
        _run_native_local_variants_arm(world),
        _run_search_pool_bounded_arm(world),
    ]
    per_arm: dict[str, Any] = {}
    for arm in arms:
        scores = arm["per_case_scores"]
        ledger = arm["cost"]
        per_arm[arm["strategy"]] = {
            "task_success_count": sum(1 for v in scores.values() if float(v) >= 1.0),
            "average_score": round(sum(float(v) for v in scores.values()) / max(1, len(scores)), 9),
            "worst_score": round(min(float(v) for v in scores.values()), 9) if scores else 0.0,
            "actual_tokens": ledger["actual_tokens"],
            "calls": ledger["calls"],
            "blocked_later": bool(ledger["blocked_later"]),
            "release_gate_ok": bool(arm["release_gate"].get("ok")),
            "delivered_candidate": arm["delivered_candidate"],
            "retained_pool_count": len(arm.get("retained_pool") or []),
            "vetoed": arm.get("vetoed") or [],
        }
    spends = [arm["cost"]["actual_tokens"] for arm in arms]
    min_spend = min(spends) if spends else 0
    at_parity: dict[str, float] = {}
    for arm in arms:
        timeline = arm["cost"]["timeline"] or [{"cumulative_tokens": 0, "best_average": 0.0}]
        best = 0.0
        for point in timeline:
            if int(point.get("cumulative_tokens") or 0) <= min_spend:
                best = max(best, float(point.get("best_average") or 0.0))
        at_parity[arm["strategy"]] = round(best, 9)
    native_keys = (STRATEGY_NATIVE_SINGLE_CHAMPION, STRATEGY_NATIVE_LOCAL_VARIANTS)
    search_arm = next(arm for arm in arms if arm["strategy"] == STRATEGY_SEARCH_POOL_BOUNDED)
    benefit_proven = (
        per_arm[STRATEGY_SEARCH_POOL_BOUNDED]["task_success_count"] > max(per_arm[k]["task_success_count"] for k in native_keys)
        and bool(search_arm["release_gate"].get("ok"))
        and not per_arm[STRATEGY_SEARCH_POOL_BOUNDED]["blocked_later"]
    )
    conditions = {
        "same_case_ids": [c.case_id for c in world["cases"]],
        "same_model": world["model"].adapter_id,
        "same_usage_per_call": world["model"].usage_per_call,
        "budget_cap_tokens": next(iter(world["ledgers"].values())).cap_tokens,
        "safety_gates": "always_on_not_a_variable",
        "safety_gate_group_opened": False,
    }
    return {
        "kind": "skill-search-fair-comparison",
        "eval_version": EVAL_VERSION,
        "conditions": conditions,
        "arms": arms,
        "outcome": {"per_arm": per_arm},
        "parity_view": {
            "min_actual_tokens": min_spend,
            "best_average_at_min_tokens": at_parity,
            "note": "parity 视图按各臂花费时间线取最小实际花费时点的最好均分；主对照以同预算上限下的任务结果与实际成本并列为准",
        },
        "benefit_proven": bool(benefit_proven),
        "claims": [
            "同任务（同一冻结 Case 集）/同模型（同一合成执行器与每次调用用量）/同预算上限；实际总成本逐臂并列披露",
            "安全门不做放开组：安全/权限检查在全部臂恒开启，不作为实验变量",
            "收益未证（benefit_proven=false）→ 生产搜索开关保持关闭；真实模型收益另行 blockedOn 实验",
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════
# §4 统计对照（S3：全部原有测试及新回归前后统计可核对）
# ═══════════════════════════════════════════════════════════════════════════


def parse_pytest_counts(log_text: str) -> dict[str, int]:
    """解析 pytest 汇总行（"... passed / ... failed / ... skipped" 与 errors）。"""
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    text = str(log_text or "")
    summary_line = ""
    for line in reversed(text.splitlines()):
        if any(marker in line for marker in (" passed", " failed", " error")):
            summary_line = line
            break
    if summary_line:
        tokens = summary_line.replace("=", " ").replace(",", " ").split()
        for index, token in enumerate(tokens):
            if token.isdigit() and index + 1 < len(tokens):
                key = tokens[index + 1].strip()
                if key in counts:
                    counts[key] = int(token)
    for line in text.splitlines():
        if " error" not in line:
            continue
        tokens = line.replace("=", " ").replace(",", " ").split()
        for index, token in enumerate(tokens):
            if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1].strip() in {"errors", "error"}:
                counts["errors"] = max(counts["errors"], int(token))
    return counts


def compare_suite_statistics(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    """前后统计对照表结构：failed/errors 不增、passed 不降（门禁判据）。"""
    deltas = {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in ("passed", "failed", "errors", "skipped")}
    gates = {
        "failed_not_increased": deltas["failed"] <= 0,
        "errors_not_increased": deltas["errors"] <= 0,
        "passed_not_decreased": deltas["passed"] >= 0,
    }
    return {
        "before": dict(before),
        "after": dict(after),
        "deltas": deltas,
        "gates": gates,
        "ok": all(gates.values()),
    }


def build_test_statistics(repo_root: str | Path | None, *, before_path: str | Path, after_path: str | Path) -> dict[str, Any]:
    """从两份 pytest 日志构建前后统计对照（证据类：不跑测试，只对照计数）。"""
    root = Path(repo_root) if repo_root else REPO
    before_file = Path(before_path) if Path(before_path).is_absolute() else root / before_path
    after_file = Path(after_path) if Path(after_path).is_absolute() else root / after_path
    before = parse_pytest_counts(before_file.read_text(encoding="utf-8", errors="replace")) if before_file.is_file() else {}
    after = parse_pytest_counts(after_file.read_text(encoding="utf-8", errors="replace")) if after_file.is_file() else {}
    return {
        "kind": "skill-search-suite-statistics",
        "before_path": str(before_path),
        "after_path": str(after_path),
        "before_found": bool(before),
        "after_found": bool(after),
        "comparison": compare_suite_statistics(before, after) if before and after else None,
    }


# ═══════════════════════════════════════════════════════════════════════════
# §5 兼容矩阵（step4：NATIVE-ARCHITECTURE-INTEGRATION §10 逐行落为真实回归）
# ═══════════════════════════════════════════════════════════════════════════

COMPAT_MATRIX: list[dict[str, Any]] = [
    {
        "scenario": "新技能尚无 score",
        "expectation": "不被认证加权惩罚；experimental 标注且禁 fork，可按范围试用",
        "sources": [
            ("tests/test_skill_deployment_tiers.py", ["test_no_promotion_without_evidence", "test_experimental_injection_annotated", "test_experimental_fork_downgraded"]),
        ],
        "owner": "DE-18",
    },
    {
        "scenario": "strict IR 依赖不可用",
        "expectation": "原兼容结果与 provenance 可见（fail-open）；搜索认证独立拒绝缺证据",
        "sources": [
            ("tests/test_online_skill_ir_gate.py", ["test_ir_gate_fail_open", "test_off_mode_write_bytes_unchanged"]),
            ("tests/test_skill_search_certification.py", ["test_s2_birth_evidence_never_usage_score_and_certificate_counts_are_truthful"]),
        ],
        "owner": "DE-18/32",
    },
    {
        "scenario": "出生题重答成功",
        "expectation": "evidence 保持来源身份，不增加线上健康成绩",
        "sources": [
            ("tests/test_evolution_isolation.py", ["test_candidate_generation_sees_only_dev_failures"]),
            ("tests/test_skill_search_certification.py", ["test_s2_birth_evidence_never_usage_score_and_certificate_counts_are_truthful"]),
        ],
        "owner": "DE-18/30",
    },
    {
        "scenario": "用户明确反转同名偏好",
        "expectation": "旧内容留历史，新规则不被强制 union merge",
        "sources": [
            ("tests/test_skill_lifecycle_seams.py", ["test_exact_identity_contradiction_revise_replaces"]),
        ],
        "owner": "DE-18",
    },
    {
        "scenario": "私人/空间同名技能",
        "expectation": "scope、provenance、usage、tier、champion 不串账（scoped ref 迁移未实施；锚点=命名空间/台账隔离）",
        "sources": [
            ("tests/test_skill_search_contracts.py", ["test_same_name_across_spaces_separate_ledgers_and_cli_compat"]),
            ("tests/test_skill_search_controller.py", ["test_s4_multi_space_searches_do_not_cross_accounts"]),
        ],
        "owner": "DE-06/09/18",
    },
    {
        "scenario": "两个组织 Run 并行",
        "expectation": "不切换进程全局环境或读到对方缓存",
        "sources": [
            ("tests/test_work_executor.py", ["test_parallel_runs_use_separate_workspaces"]),
            ("tests/test_work_execution.py", ["test_claim_is_single_active_and_fenced_by_context"]),
        ],
        "owner": "DE-11/12/18",
    },
    {
        "scenario": "投影文件被选作搜索目标",
        "expectation": "拒绝直接编辑；源修订后按原投影链重建",
        "sources": [
            ("tests/test_skill_search_contracts.py", ["test_projection_and_unknown_targets_rejected_with_reason", "test_frozen_source_readonly_and_candidate_artifacts_isolated"]),
            ("tests/test_skill_lifecycle_seams.py", ["test_restore_content_rejects_projection"]),
        ],
        "owner": "DE-06/29/32",
    },
    {
        "scenario": "搜索期间用户又修订 active",
        "expectation": "搜索可保留结果但采用时报漂移，不能覆盖",
        "sources": [
            ("tests/test_skill_search_certification.py", ["test_s4_adopt_refuses_source_changed_and_projection_takeover"]),
        ],
        "owner": "DE-18/34/35",
    },
    {
        "scenario": "候选 A 获胜而 active 仍是 B",
        "expectation": "B 不因 A 的报告晋级，展示两者 hash",
        "sources": [
            ("tests/test_skill_search_certification.py", ["test_s1_champion_scores_cannot_promote_untouched_active_and_search_reports_never_promote"]),
            ("tests/test_skill_lifecycle_seams.py", ["test_promotion_rejects_drifted_active_and_promotes_on_match"]),
        ],
        "owner": "DE-18/35",
    },
    {
        "scenario": "晋级或回退",
        "expectation": "tier 与内容回退分开记录，实际读取版本与报告一致",
        "sources": [
            ("tests/test_skill_deployment_tiers.py", ["test_one_click_rollback"]),
            ("tests/test_skill_lifecycle_seams.py", ["test_rollback_only_changes_tier_restore_only_changes_content"]),
        ],
        "owner": "DE-18/35",
    },
    {
        "scenario": "只改 when_to_use",
        "expectation": "测正负查询/竞争，不用正文强制注入得分代替（组合重放锚点；触发冠军回归保持）",
        "sources": [
            ("tests/test_skill_combo_replay.py", ["test_conflict_pair_detected_and_blocked", "test_clean_pair_not_flagged"]),
            ("tests/test_skill_search_protocol.py", ["test_freeze_requires_roles_and_is_deterministic"]),
        ],
        "owner": "DE-30/36",
    },
    {
        "scenario": "多技能分别通过、组合冲突",
        "expectation": "复用现有组合冲突/组合 replay，组合必须重新验收",
        "sources": [
            ("tests/test_skill_combo_replay.py", ["test_conflict_pair_detected_and_blocked", "test_duplicate_pair_traced_to_four_way_decision", "test_main_report_all_assertions_hold"]),
        ],
        "owner": "DE-30/36",
    },
    {
        "scenario": "旧 holdout 已逐轮查看",
        "expectation": "只报曲线/selection；新的独立 H 另行封存",
        "sources": [
            ("tests/test_evolution_isolation.py", ["test_split_manifest_is_deterministic_and_append_only", "test_holdout_snapshot_and_hash_guard"]),
            ("tests/test_skill_search_protocol.py", ["test_observed_final_holdout_split_cannot_be_renamed_as_new_h"]),
        ],
        "owner": "DE-24/35",
    },
    {
        "scenario": "未启动新优化器",
        "expectation": "原 CLI/Web/在线抽取、触发冠军和 tier 回归保持（新增组件关闭时原生路径可用）",
        "sources": [
            ("tests/test_skill_search_mutator.py", ["test_s5_policy_disabled_keeps_legacy_routes_and_native_paths_uninterrupted"]),
            ("tests/test_collect_extraction.py", []),
            ("tests/test_skill_deployment_tiers.py", ["test_cli_tier_commands"]),
            ("tests/test_skill_search_eval.py", ["test_native_paths_usable_with_search_components_closed"]),
        ],
        "owner": "DE-18/36",
    },
]


def verify_compat_matrix(repo_root: str | Path | None = None) -> dict[str, Any]:
    """逐行核对：回归文件存在且引用的测试节点真实出现在文件文本中。"""
    root = Path(repo_root) if repo_root else REPO
    rows: list[dict[str, Any]] = []
    checked_files = 0
    for row in COMPAT_MATRIX:
        anchors: list[dict[str, Any]] = []
        row_ok = True
        for rel_path, nodes in row["sources"]:
            path = root / rel_path
            exists = path.is_file()
            found_nodes: list[str] = []
            missing_nodes: list[str] = []
            if exists:
                checked_files += 1
                text = path.read_text(encoding="utf-8", errors="replace")
                for node in nodes:
                    if f"def {node}(" in text:
                        found_nodes.append(node)
                    else:
                        missing_nodes.append(node)
            else:
                missing_nodes.extend(nodes)
            ok = exists and not missing_nodes
            row_ok = row_ok and ok
            anchors.append({"file": rel_path, "exists": exists, "found": found_nodes, "missing": missing_nodes, "ok": ok})
        rows.append({
            "scenario": row["scenario"],
            "expectation": row["expectation"],
            "owner": row["owner"],
            "anchors": anchors,
            "ok": row_ok,
        })
    return {
        "kind": "skill-search-compat-matrix",
        "eval_version": EVAL_VERSION,
        "source": "docs/digital-employee/NATIVE-ARCHITECTURE-INTEGRATION.md#10-给接手-agent-的不可省略场景",
        "rows": rows,
        "checked_files": checked_files,
        "ok": all(row["ok"] for row in rows),
    }


def native_paths_with_search_disabled(evolution_dir: str | Path) -> dict[str, Any]:
    """关键开关关闭组合实测：干净台账根（无搜索命名空间）上，原生路径可用。

    - 在线抽取 pending window：save → claim → complete 全流程独立可用；
    - tier：set/get 语义可用（experimental 注入/禁 fork 语义由
      tests/test_skill_deployment_tiers.py 继续锁定，这里只做关闭组合冒烟）；
    - 冠军/tier 晋级门：promotion_gate 对无认证记录的原生技能放行（零变化）；
    - 新优化器默认关闭：DE-32 MutationPolicy 默认 enabled=False（与 mutator S5
      回归共同证明：不 import、不拦截在线路径）。
    """
    root = Path(evolution_dir)
    out: dict[str, Any] = {"kind": "skill-search-native-paths-check", "evolution_dir": str(root)}
    from agents import extraction_window as ew

    window = {"messages": [{"role": "user", "content": "把灰度比例改成 10 percent"}], "next_user_feedback": "ok"}
    saved = ew.save_pending_window("sess-native-check", window)
    out["extraction_save"] = bool(saved.get("status") == "pending")
    claimed = ew.claim_window("sess-native-check", saved["windowId"], feedback="ok", now=123.0)
    out["extraction_claim"] = bool(claimed is not None and claimed.get("status") == "claimed")
    completed = ew.mark_completed("sess-native-check", saved["windowId"], summary="extracted")
    out["extraction_complete"] = bool(completed.get("status") == "completed")

    from agents import skill_tiers as tiers_mod

    set_out = tiers_mod.set_tier("native-check-skill", "experimental", trigger="de36-native-check")
    out["tier_set"] = bool(set_out.get("ok"))
    out["tier_get"] = str(tiers_mod.get_tier("native-check-skill") or "")

    native_item = {"artifacts": {"eval_source": "online_eval"}}
    out["promotion_gate_native_passthrough"] = bool(promotion_gate(native_item, "native-check-skill").get("ok"))

    from agents.skill_search.mutator import MutationPolicy

    policy = MutationPolicy()
    out["new_optimizer_default_off"] = bool(getattr(policy, "enable_bounded_module_edit", True) is False)

    out["search_namespaces_present"] = len(find_searches(default_search_base()))
    out["ok"] = all(
        out[key]
        for key in (
            "extraction_save",
            "extraction_claim",
            "extraction_complete",
            "tier_set",
            "promotion_gate_native_passthrough",
            "new_optimizer_default_off",
        )
    )
    return out


# ═══════════════════════════════════════════════════════════════════════════
# §6 双线报告（step5：基础试点与搜索增强两条验收结果）
# ═══════════════════════════════════════════════════════════════════════════


def build_dual_line_report(
    *,
    basic_pilot: dict[str, Any] | None = None,
    search_evidence: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
    matrix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """两条验收线分开报告（不以方案交付结项）。

    - ``basic_pilot``：基础数字员工试点线（DE-24/25 口径），status 由调用方
      证据决定（implemented/partial/not_executed/blocked），本函数不伪造；
    - ``search_enhancement``：搜索增强线（DE-29～36）。无预算或无真实样本 →
      status=not_executed + decision=keep_disabled；收益未证 → 保持关闭；
      已实现/部分/未执行/阻塞四态分开；
    - ``de_status_impact``：基础通过不使 DE-29～36 自动 done。
    """
    basic = dict(basic_pilot or {})
    basic_status = str(basic.get("status") or "not_executed")
    evidence = dict(search_evidence or {})
    budget_present = bool(evidence.get("budget_present"))
    real_samples = int(evidence.get("real_samples") or 0)
    review_ground_truth = bool(evidence.get("review_ground_truth"))
    isolated_env = bool(evidence.get("isolated_execution_environment"))
    benefit_proven = bool((comparison or {}).get("benefit_proven")) if comparison else False
    matrix_ok = bool((matrix or {}).get("ok")) if matrix else False
    implementation_landed = bool(evidence.get("implementation_landed")) or matrix_ok

    gate = {
        "budget_present": budget_present,
        "real_samples": real_samples,
        "review_ground_truth": review_ground_truth,
        "isolated_execution_environment": isolated_env,
        "benefit_proven": benefit_proven,
    }
    blockers = [name for name, value in gate.items() if not value]
    if not budget_present or real_samples <= 0:
        search_status = "not_executed"
        decision = "keep_disabled"
    elif not benefit_proven:
        search_status = "partial" if implementation_landed else "not_executed"
        decision = "keep_disabled"
    elif blockers:
        search_status = "blocked"
        decision = "keep_disabled"
    else:
        search_status = "implemented"
        decision = "eligible_for_pilot"
    if not implementation_landed and search_status == "partial":
        search_status = "not_executed"
    return {
        "kind": "skill-search-dual-line-report",
        "eval_version": EVAL_VERSION,
        "basic_pilot": {
            "line": "native_business（DE-00～25/43）",
            "status": basic_status,
            "detail": basic,
            "note": "基础试点口径与证据由 DE-24/25 验收文档维护；本报告不重判",
        },
        "search_enhancement": {
            "line": "search_extension（" + "、".join(SEARCH_LINE_TASKS) + "）",
            "status": search_status,
            "gate": gate,
            "blockers": blockers,
            "decision": decision,
            "synthetic_mechanism_demo": bool(comparison),
            "compat_matrix_ok": matrix_ok,
            "note": "已实现（机制与回归）/部分（合成验证完成、真实实验未做）/未执行（无预算或无真实样本）/阻塞（外部条件）四态分开；不以方案交付结项",
        },
        "claims": {
            "lines_reported_separately": True,
            "no_budget_reports_not_executed": (not budget_present) and search_status == "not_executed",
            "no_real_samples_reports_not_executed": real_samples <= 0 and search_status == "not_executed",
            "benefit_unproven_keeps_disabled": (not benefit_proven) and decision == "keep_disabled",
            "basic_pass_does_not_auto_done_search": True,
            "search_pass_does_not_imply_basic": True,
        },
        "de_status_impact": {
            "auto_done": False,
            "tasks_affected": list(SEARCH_LINE_TASKS),
            "why": "基础试点通过只更新基础线工单；DE-29～36 各自按其 completion_rule 与真实证据判定",
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# §7 演示世界与 CLI
# ═══════════════════════════════════════════════════════════════════════════

DEMO_SEED_TEXT = """---
name: deploy-rollout
description: 部署发布独立 SOP
when_to_use: deploy release canary rollout
producer-kind: executable_sop
version: 1.0.0
---

## 工作流

1. 灰度 5% 观察 10 分钟。
2. 全量并回填结果。

## 安全边界

只读保护内容：不得越权改生产配置。
"""

DEMO_CASES = [
    ComparisonCase(case_id="case-1", trigger="deploy canary ratio", expected_keywords=("灰度 10%",)),
    ComparisonCase(case_id="case-2", trigger="deploy summary backfill", expected_keywords=("发布摘要",)),
    ComparisonCase(case_id="case-v", trigger="deploy final acceptance", expected_keywords=("灰度 10%", "发布摘要")),
]

DEMO_EDITS = [
    BoundedEdit(edit_id="fix-canary", find="灰度 5% 观察 10 分钟。", replace="灰度 10% 观察 10 分钟。", fixes=("case-1",)),
    BoundedEdit(edit_id="add-summary", find="2. 全量并回填结果。", replace="2. 全量并回填结果，并生成发布摘要。", fixes=("case-2", "case-v")),
    # 安全违规编辑：触碰守卫小节（安全门恒开启，全部臂拒绝，无放开组）
    BoundedEdit(
        edit_id="weaken-guardrail",
        find="只读保护内容：不得越权改生产配置。",
        replace="必要时可越权改生产配置。",
        unsafe=True,
    ),
]


def run_demo(out_path: str | Path | None = None) -> dict[str, Any]:
    """离线合成演示：公平对照 + 兼容矩阵核对 + 双线报告，一条命令产出全部结构。"""
    world = build_comparison_world(
        cases=DEMO_CASES,
        verify_case_ids=["case-v"],
        seed_text=DEMO_SEED_TEXT,
        edits=DEMO_EDITS,
        budget_tokens=600,
        usage_per_call=5,
    )
    comparison = run_fair_comparison(world)
    matrix = verify_compat_matrix()
    report = build_dual_line_report(
        basic_pilot={
            "status": "partial",
            "basis": ["DE-24 评测框架与合成回归", "DE-25 blockedOn：真实影子/灰度/自然周期/独立样本"],
        },
        search_evidence={
            "implementation_landed": True,
            "budget_present": False,
            "real_samples": 0,
            "review_ground_truth": False,
            "isolated_execution_environment": False,
        },
        comparison=comparison,
        matrix=matrix,
    )
    out = {
        "kind": "skill-search-eval",
        "eval_version": EVAL_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "comparison": comparison,
        "compat_matrix": matrix,
        "dual_line_report": report,
    }
    target = Path(out_path) if out_path else REPO / "eval" / "runs" / "skill-search-eval.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DE-36 搜索增强独立验收评估器")
    parser.add_argument("--demo", action="store_true", help="离线合成演示（对照+矩阵+双线报告）")
    parser.add_argument("--dual-report", action="store_true", help="只输出双线报告结构")
    parser.add_argument("--matrix-check", action="store_true", help="只做 §10 兼容矩阵核对")
    parser.add_argument("--list", action="store_true", help="列出全部搜索命名空间")
    parser.add_argument("--detail", action="store_true", help="输出指定 search ID 的详情报告")
    parser.add_argument("--search-id", default="", help="详情/列表过滤用 search ID")
    parser.add_argument("--out", default="", help="报告落盘路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    args = parser.parse_args(argv)

    payload: dict[str, Any] | list[dict[str, Any]] | None = None
    if args.demo:
        target = None
        if args.out:
            target = Path(args.out) if Path(args.out).is_absolute() else REPO / args.out
        payload = run_demo(target)
    elif args.dual_report:
        payload = build_dual_line_report(
            basic_pilot={"status": "partial", "basis": ["见 docs/digital-employee/acceptance/DE-25/"]},
            search_evidence={"implementation_landed": True, "budget_present": False, "real_samples": 0},
            comparison=None,
            matrix=verify_compat_matrix(),
        )
    elif args.matrix_check:
        payload = verify_compat_matrix()
    elif args.list:
        payload = find_searches()
        if args.search_id:
            payload = [item for item in payload if item.get("search_id") == args.search_id]
    elif args.detail:
        if not args.search_id:
            parser.error("--detail 需要 --search-id")
        payload = build_detail_report(args.search_id)
    else:
        parser.print_help()
        return 0
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out and not args.demo:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out or args.demo:
        print(text)
    if isinstance(payload, dict) and payload.get("ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
