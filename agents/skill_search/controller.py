"""DE-34 持久多代搜索控制器与全成本预算（搜索增强 S1 第六步）。

对应 SKILL-EVOLUTION-SEARCH §11（多代搜索状态、成本与可靠恢复）/§4（就绪条件）、
NATIVE-ARCHITECTURE-INTEGRATION §6/§7 与 tasks.json DE-34：

- **阶段化状态机**：``draft → readiness_check → baseline_evaluating → searching
  → finalizing → awaiting_review → completed``；阻塞走 Later 语义（``paused /
  blocked_budget / blocked_environment``，不消耗失败次数），终态含
  ``cancelled / failed / cleanup_failed / blocked_source_changed /
  blocked_protocol_drift``。轮次内断点：``parent_selection → feedback → mutation
  → validation → deployment → evaluation → judging``。
- **每阶段可恢复**：阶段断点（父代与 RNG 状态、patch、候选制品与快照、逐 Case
  终态、开放预留）在 DE-29 搜索命名空间内原子落盘（``store.py``）。恢复先对账
  （协议/源/manifest 绑定 → worker 租约 → 开放预留按回执结算或释放），再从断点
  续跑——不重抽父代（父代选择是前沿+RNG 状态的确定函数，RNG 状态与父代同笔
  原子落盘）、不生成另一 patch（patch 已落盘即复用）、不重复扣费（先预留落盘
  再调用，完成按真实 usage 结算，崩溃恢复按执行器回执对账一次）、不丢已完成
  Case（逐 Case 终态先落盘再进入下一阶段）。
- **全成本预算（DE-15）**：七个成本类别 ``baseline / reflection / mutation /
  execution / judge / retry / final_verification`` 全部先原子预留（预留先于副作用
  落盘）、后按真实 usage 结算（失败/取消同样归集，不留免费副作用）。预算不足是
  Later（``blocked_budget``）：不计失败次数，补预算后从原阶段断点续跑。
- **并发与漂移**：初期单变异链（同一时刻只有一条活动轮次）、Case 有界并发
  （``max_case_workers``）；generation CAS + worker 租约保证两 worker/旧
  generation 不能覆盖新状态；manifest/源/Case 协议漂移拒绝续用旧分数；取消后
  无新副作用（已开始的调用按回执对账）；清理失败标记 ``cleanup_failed`` 而非
  completed，``can_publish=False`` 阻断发布。
- **隔离**：部署走注入的 isolation 端口（候选挂载回执），完成后恢复隔离种子
  挂载并保存回执；无隔离端口或恢复失败 → ``cleanup_failed`` + 发布阻断
  （fail-closed）。评测协议 payload 绑定搜索命名空间（tenant/space/lineage/
  search_id 由 manifest 冻结），多空间不串账。
- **启用条件（fail-closed）**：``ControllerConfig.prerequisites`` 必须显式声明
  DE-30 评测、DE-31 反馈、DE-32 变异与原局部对照就绪，缺则拒绝启动并给原因，
  零副作用（不初始化状态、不预留预算）。
- **作用域边界（S4）**：控制器是独立调用入口，不是常驻拦截器——不 import、
  不拦截 ``agents.extraction_window``（会话抽取路径不受关闭/暂停影响）；不写
  生产技能目录、不触发 tier 晋级（复用 DE-14/15 的持久 Run/租约与预算原语，
  不复制另一调度器）。

真实模型能力实验（预算/审核真值/隔离执行环境）另行 blockedOn；本模块离线用
注入端口（合成执行器/确定性归因器/patch 提供者/固定时钟/fake 预算端口）合成验证。
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import evaluator as ev
from . import feedback as fb
from . import mutator as mu
from . import pool as pl
from . import protocol as sp
from . import selection as sel
from . import store as st
from .contracts import SKILL_TEXT_NAME, SearchManifest, search_namespace
from .modules import ModuleSpec, spec_from_dict
from .protocol import CaseFreeze, verify_case_freeze

SEED_CANDIDATE_ID = "seed"
CONTROLLER_ALGORITHM_VERSION = st.CONTROLLER_ALGORITHM_VERSION

# 全成本类别（§11：基线/反思/变异/执行/judge/重试/最终验证分开计数）
CATEGORY_BASELINE = "baseline"
CATEGORY_REFLECTION = "reflection"
CATEGORY_MUTATION = "mutation"
CATEGORY_EXECUTION = "execution"
CATEGORY_JUDGE = "judge"
CATEGORY_RETRY = "retry"
CATEGORY_FINAL_VERIFICATION = "final_verification"
ALL_CATEGORIES = (
    CATEGORY_BASELINE,
    CATEGORY_REFLECTION,
    CATEGORY_MUTATION,
    CATEGORY_EXECUTION,
    CATEGORY_JUDGE,
    CATEGORY_RETRY,
    CATEGORY_FINAL_VERIFICATION,
)

REQUIRED_PREREQUISITES = ("de30_evaluator", "de31_feedback", "de32_mutator", "local_baseline")

CASE_PENDING = "pending"
CASE_IN_FLIGHT = "in_flight"
CASE_DONE = "done"
CASE_INFRA_DEAD = "infra_dead"


class FaultInjected(Exception):
    """测试注入的阶段故障：控制器不吞、直接向上抛（模拟进程在阶段边界/调用中崩溃）。"""


@dataclass
class ControllerConfig:
    """控制器运行配置（预算估计/并发上限/代数上限；阈值是待试点标定的配置）。"""

    max_iterations: int = 2
    max_case_workers: int = 2
    max_infra_retries_per_case: int = 1
    sampler_seed: int = 0
    lease_seconds: float = 300.0
    # 预算估计（DE-15 原子预留单位；结算按真实 usage 归集）
    case_estimate_tokens: int = 10
    retry_estimate_tokens: int = 10
    reflection_tokens: int = 8
    mutation_tokens: int = 8
    judge_tokens: int = 6
    final_case_tokens: int = 10
    # 前置就绪标记（DE-30/31/32 + 原局部对照）：缺任一 → 拒绝启动
    prerequisites: dict[str, bool] = field(default_factory=dict)
    # 逐 Case 轨迹引用（真实路径来自原 Trace 存储；{parent} 占位符替换为父代 ID）
    trace_refs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_iterations": self.max_iterations,
            "max_case_workers": self.max_case_workers,
            "max_infra_retries_per_case": self.max_infra_retries_per_case,
            "sampler_seed": self.sampler_seed,
            "lease_seconds": self.lease_seconds,
            "case_estimate_tokens": self.case_estimate_tokens,
            "retry_estimate_tokens": self.retry_estimate_tokens,
            "reflection_tokens": self.reflection_tokens,
            "mutation_tokens": self.mutation_tokens,
            "judge_tokens": self.judge_tokens,
            "final_case_tokens": self.final_case_tokens,
            "prerequisites": dict(self.prerequisites),
        }


class De15BudgetPort:
    """``evoharness.governance.budget``（DE-15）的控制器端口适配。

    - ``reserve(category, tokens, memo)``：memo 进入 BudgetReservation.run_id，
      预算库是预留真相；崩溃后 ``open_handles()`` 可按 scope 找回未关闭预留；
    - ``settle/release``：真实 usage 结算 / 取消释放（失败同样结算归集）；
    - 线程锁只串行化端口内的 session 访问；限额判断仍由 DE-15 原子预留完成。
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        org_id: str = "",
        space_id: str = "",
        principal_id: str = "",
        scope_prefix: str = "search",
    ):
        self._factory = session_factory
        self._org = org_id
        self._space = space_id
        self._principal = principal_id
        self._prefix = scope_prefix
        self._lock = threading.Lock()

    def _memo(self, memo: str) -> str:
        return f"{self._prefix}:{memo}"

    def reserve(self, category: str, tokens: int, memo: str) -> dict[str, Any]:
        from evoharness.governance import budget as de15

        with self._lock:
            with self._factory() as session:
                row = de15.reserve(
                    session,
                    org_id=self._org,
                    space_id=self._space,
                    principal_id=self._principal,
                    run_id=self._memo(memo),
                    category=str(category),
                    tokens=int(tokens),
                    estimated=True,
                )
                session.commit()
                return {"handle": row.reservation_id, "tokens": int(tokens), "category": str(category)}

    def settle(self, handle: str, used_tokens: int, *, ok: bool = True) -> None:
        from evoharness.governance import budget as de15

        if not str(handle or "").strip():
            return
        with self._lock:
            with self._factory() as session:
                de15.settle(session, str(handle), used_tokens=max(0, int(used_tokens)), ok=ok)
                session.commit()

    def release(self, handle: str) -> None:
        from evoharness.governance import budget as de15

        if not str(handle or "").strip():
            return
        with self._lock:
            with self._factory() as session:
                de15.release(session, str(handle))
                session.commit()

    def open_handles(self) -> dict[str, str]:
        from evoharness.governance import budget as de15
        from evoharness.governance.models import BudgetReservation
        from sqlalchemy import select

        de15.usage_summary  # 端口与 DE-15 绑定（防误接其他预算实现）
        with self._lock:
            with self._factory() as session:
                rows = list(session.scalars(select(BudgetReservation).where(
                    BudgetReservation.status == "reserved")))
                return {
                    str(row.run_id): row.reservation_id
                    for row in rows
                    if str(row.run_id or "").startswith(f"{self._prefix}:")
                }

    def usage_summary(self) -> dict[str, Any]:
        from evoharness.governance import budget as de15

        with self._lock:
            with self._factory() as session:
                return de15.usage_summary(session, org_id=self._org, space_id=self._space)

    def set_limits(self, *, org_tokens: int | None = None, space_tokens: int | None = None) -> None:
        """调整限额（补预算后从原阶段续跑的运维入口；DE-15 set_limit 语义）。"""
        from evoharness.governance import budget as de15

        with self._lock:
            with self._factory() as session:
                if org_tokens is not None:
                    de15.set_limit(session, scope_kind="org", scope_id=self._org, limit_tokens=int(org_tokens))
                if space_tokens is not None:
                    de15.set_limit(session, scope_kind="space", scope_id=self._space, limit_tokens=int(space_tokens))
                session.commit()


@dataclass
class _RunResult:
    status: str
    phase: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "phase": self.phase, "detail": self.detail}


# 停止推进主循环的 disposition（waiting/cancelled/failed/终态）
_STOP_DISPOSITIONS = {
    "waiting_budget",
    "cancelled",
    "failed",
    "cleanup_failed",
    "completed",
    st.STATUS_PAUSED,
    st.STATUS_BLOCKED_BUDGET,
    st.STATUS_BLOCKED_SOURCE_CHANGED,
    st.STATUS_BLOCKED_PROTOCOL_DRIFT,
    st.STATUS_CLEANUP_FAILED,
    st.PHASE_COMPLETED,
}


def _new_round_state(index: int) -> dict[str, Any]:
    return {
        "index": int(index),
        "round_id": f"round-{int(index):04d}",
        "phase": st.ROUND_PHASE_PARENT,
        "parent_id": "",
        "parent_draw": None,
        "feedback_ref": "",
        "patch": None,
        "patch_meta": {},
        "candidate_id": "",
        "candidate_ref": "",
        "candidate_meta": {},
        "cases": {},
        "finalize": None,
        "decision": "",
        "no_mutation_reason": "",
        "blocked_reason": "",
        "infra_failures": 0,
        "failure_attempts": 0,
    }


class SearchController:
    """一次搜索任务（SearchManifest）的阶段化持久控制器（DE-34）。

    端口注入（全部离线可合成）：
    - ``executor``：DE-30 执行适配器（``execute(case,candidate,frozen_protocol)``
      → ``ExecutionOutcome``，sync 或 async）；可选 ``get_receipt(candidate_id,
      case_id)`` 供崩溃恢复按真实回执对账（已开始的外部调用不重发）；
    - ``budget``：DE-15 预算端口（``De15BudgetPort`` 或同契约注入端口）；
    - ``attributor``：归因提案生成（真实模型路径走 ``feedback`` 的 prompt 契约；
      离线注入确定性提案）；
    - ``patch_provider``：patch 生成（真实模型路径走 ``mutator`` 的 prompt 契约；
      离线注入确定性 patch）；
    - ``judge``：可选 judge 调用（真实路径 LLM judge；缺省为确定性入池门，
      仍按类别归集 judge 成本）；
    - ``isolation``：隔离端口（``mount(candidate_id, text)`` / ``restore_seed()``
      → 回执 dict）；缺省 → 清理失败（fail-closed，无隔离回执不发布）；
    - ``trace_store``：DE-31 ``TraceStore``（引用读取原 Trace，不复制轨迹）；
    - ``fault_hook``：测试故障注入（按阶段名调用；抛出即模拟进程崩溃）；
    - ``clock``：固定时钟（租约时间戳；离线可测）。
    """

    def __init__(
        self,
        *,
        manifest: SearchManifest,
        cases: list[dict[str, Any]],
        freeze: CaseFreeze,
        module_spec: ModuleSpec,
        executor: Any,
        budget: Any,
        config: ControllerConfig | None = None,
        store: st.SearchStore | None = None,
        base_dir: str | Path | None = None,
        attributor: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
        patch_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        judge: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        isolation: Any = None,
        trace_store: fb.TraceStore | None = None,
        fault_hook: Callable[[str], None] | None = None,
        clock: Callable[[], float] | None = None,
        scope_guard: Callable[[SearchManifest], dict[str, Any] | None] | None = None,
    ):
        self.manifest = manifest
        self.cases = list(cases or [])
        self.freeze = freeze
        self.spec = module_spec
        # 搜索集 S 是池/前沿/评分向量的口径；V 只用于最终验证（H 封存不进控制器）
        self.s_case_ids = sorted(self.freeze.roles.get("S") or [])
        self._derived_freezes: dict[tuple[str, ...], CaseFreeze] = {}
        self.executor = executor
        self.budget = budget
        self.config = config or ControllerConfig()
        self.attributor = attributor
        self.patch_provider = patch_provider
        self.judge = judge
        self.isolation = isolation
        self.trace_store = trace_store or fb.TraceStore({})
        self.fault_hook = fault_hook
        self._clock = clock or _default_clock
        # DE-06：搜索入口撤权守卫（返回 dict = 拒绝，None = 放行）
        self.scope_guard = scope_guard
        self._lock = threading.Lock()
        self.store = store or st.SearchStore(search_namespace(manifest, base_dir))
        self.base_dir = base_dir
        self._pool: pl.CandidatePool | None = None
        self._rejections: mu.RejectionHistory | None = None

    # ── 基础工具 ────────────────────────────────────────────────────────────

    def _now(self) -> float:
        return float(self._clock())

    def _scope_id(self) -> str:
        m = self.manifest
        return f"{m.tenant}/{m.space}/{m.lineage}/{m.search_id}"

    def _fault(self, stage: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(stage)

    def _require_state(self) -> dict[str, Any]:
        if self.store.state is None:
            self.store.load()
        if self.store.state is None:
            raise st.StoreCorruption("state_not_initialized", str(self.store.state_path))
        return self.store.state

    @property
    def pool(self) -> pl.CandidatePool:
        if self._pool is None:
            loaded = self.store.load_pool()
            if loaded is None:
                loaded = pl.CandidatePool(
                    search_id=self.manifest.search_id,
                    case_ids=list(self.s_case_ids),
                )
            self._pool = loaded
        return self._pool

    def _derived_freeze(self, case_ids: list[str]) -> CaseFreeze:
        """按角色子集派生冻结口径（池/评分向量只用 S；最终验证只用 V）。

        完整性门的 Case 顺序来自派生冻结；结果绑定的协议指纹仍是 manifest 的
        （``finalize_score_vector(manifest=...)`` 口径），跨协议分数依旧不可混用。
        """
        key = tuple(sorted(case_ids))
        if key not in self._derived_freezes:
            wanted = set(key)
            subset = [dict(c) for c in self.cases if str(c.get("case_id") or "") in wanted]
            result = sp.freeze_case_protocol(subset, protocol=self.freeze.protocol)
            if not result.get("ok"):
                raise ValueError(f"derived_freeze_failed:{result.get('error')}")
            self._derived_freezes[key] = result["freeze"]
        return self._derived_freezes[key]

    def _persist_pool(self) -> None:
        self.store.save_pool(self.pool)

    def _save_under_lock(self) -> None:
        with self._lock:
            self.store.save()

    # ── 就绪门（启用条件，fail-closed，零副作用） ────────────────────────────

    def readiness(self) -> dict[str, Any]:
        """前置就绪校验：DE-30/31/32 与原局部对照就绪标记、协议绑定、源完整性。"""
        missing = [
            key
            for key in REQUIRED_PREREQUISITES
            if not bool((self.config.prerequisites or {}).get(key))
        ]
        if missing:
            return {"ok": False, "error": "not_ready", "missing": missing}
        splits = (self.manifest.protocol or {}).get("splits") or {}
        if str(splits.get("fingerprint") or "") != self.freeze.fingerprint:
            return {
                "ok": False,
                "error": "protocol_not_bound_to_manifest",
                "missing": [],
                "reason": "CaseFreeze 指纹未绑定进 manifest.protocol['splits']：先经 DE-29 freeze_protocol 冻结协议",
            }
        if not (self.freeze.roles.get("S") or []):
            return {"ok": False, "error": "not_ready", "missing": ["s_case_set_empty"]}
        integrity = self.manifest.verify_integrity()
        if not integrity.get("ok"):
            return {
                "ok": False,
                "error": str(integrity.get("reason") or "manifest_integrity_failed"),
                "missing": [],
                "drifted": integrity.get("drifted", []),
            }
        if self.scope_guard is not None:
            denial = self.scope_guard(self.manifest)
            if denial:
                return {"ok": False, "missing": [], **denial}
        return {"ok": True, "missing": [], "s_case_ids": sorted(self.freeze.roles.get("S") or [])}

    # ── 启动/恢复主循环 ─────────────────────────────────────────────────────

    def start(self, *, worker: str = "worker-1") -> dict[str, Any]:
        """启动（新搜索）或恢复（已有状态）：readiness 不过 → 拒绝并给原因，零副作用。"""
        ready = self.readiness()
        if not ready.get("ok"):
            return {"ok": False, **ready}
        if not self.store.exists():
            self.store.initialize(
                manifest=self.manifest,
                case_ids=list(self.freeze.ordered_case_ids),
                config=self.config.to_dict(),
                sampler_state={"seed": int(self.config.sampler_seed), "state": None},
            )
            self._persist_pool()
        return self.run(worker=worker)

    def run(self, *, worker: str = "worker-1") -> dict[str, Any]:
        """主循环：从当前断点推进到终态或 Later 阻塞；可反复调用（每阶段可恢复）。"""
        state = self._require_state()
        drift = self._preflight_binding(state)
        if drift is not None:
            summary = self.status()
            summary["ok"] = False
            return summary
        if not self.store.claim_lease(worker, now=self._now(), lease_seconds=self.config.lease_seconds):
            return {"ok": False, "error": "lease_busy", "owner": str(state.get("owner") or "")}
        reconcile = self._reconcile_open_reservations()
        result: _RunResult | None = None
        while result is None:
            state = self._require_state()
            if state.get("cancel_requested"):
                result = self._finish_cancelled()
                break
            if state.get("pause_requested"):
                self._mark_status(st.STATUS_PAUSED, str(state.get("pause_reason") or "pause_requested"))
                result = _RunResult(st.STATUS_PAUSED, str(state.get("phase") or ""))
                break
            phase = str(state.get("phase") or st.PHASE_DRAFT)
            self._fault(phase)
            if phase == st.PHASE_DRAFT:
                self._advance_phase(st.PHASE_READINESS)
                continue
            if phase == st.PHASE_READINESS:
                ready = self.readiness()
                if not ready.get("ok"):
                    self._mark_failed(str(ready.get("error") or "not_ready"), detail=ready)
                    result = _RunResult(st.STATUS_FAILED, phase, str(ready.get("error")))
                    break
                self._advance_phase(st.PHASE_BASELINE)
                continue
            if phase == st.PHASE_BASELINE:
                disposition = self._run_baseline()
                if disposition in _STOP_DISPOSITIONS:
                    result = self._disposition_result(disposition, phase)
                continue
            if phase == st.PHASE_SEARCHING:
                disposition = self._run_searching()
                if disposition in _STOP_DISPOSITIONS:
                    result = self._disposition_result(disposition, phase)
                continue
            if phase == st.PHASE_FINALIZING:
                disposition = self._run_finalizing()
                if disposition in _STOP_DISPOSITIONS:
                    result = self._disposition_result(disposition, phase)
                continue
            if phase == st.PHASE_AWAITING_REVIEW:
                disposition = self._run_cleanup()
                result = self._disposition_result(disposition, phase)
                break
            if phase == st.PHASE_COMPLETED:
                result = _RunResult(st.PHASE_COMPLETED, phase)
                break
            self._mark_failed("unknown_phase", detail={"phase": phase})
            result = _RunResult(st.STATUS_FAILED, phase, "unknown_phase")
            break
        summary = self.status()
        summary["reconciled_reservations"] = reconcile
        return summary

    # 恢复语义：run() 可在任意阶段故障/阻塞后再次调用（先对账，再从断点续跑）。
    resume = run

    def status(self) -> dict[str, Any]:
        state = self._require_state()
        return {
            "ok": True,
            "search_id": state.get("search_id"),
            "phase": state.get("phase"),
            "status": state.get("status"),
            "generation": state.get("generation"),
            "round_index": (state.get("round") or {}).get("index"),
            "round_phase": (state.get("round") or {}).get("phase"),
            "champion": str((state.get("finalization") or {}).get("champion") or ""),
            "can_publish": bool((state.get("cleanup") or {}).get("can_publish")),
            "release_blockers": list((state.get("cleanup") or {}).get("release_blockers") or []),
            "costs": self.store.cost_summary(),
            "pool_size": len(self.pool.candidates),
            "attempts": len(self.pool.attempts),
            "cancel_requested": bool(state.get("cancel_requested")),
            "pause_requested": bool(state.get("pause_requested")),
        }

    # ── 取消/暂停（跨实例请求：CAS 重试写入） ─────────────────────────────────

    def request_cancel(self, note: str = "") -> bool:
        return self._flag_with_retry("cancel_requested", note or "cancel_requested")

    def request_pause(self, note: str = "") -> bool:
        return self._flag_with_retry("pause_requested", note or "pause_requested")

    def _flag_with_retry(self, flag: str, note: str) -> bool:
        for _ in range(4):
            try:
                if self.store.state is None:
                    self.store.load()
                state = self._require_state()
                state[flag] = True
                if flag == "cancel_requested":
                    state["cancel_note"] = note
                else:
                    state["pause_reason"] = note
                self.store.save()
                return True
            except st.StoreConflict:
                self.store.reload()
                continue
        return False

    # ── 绑定核验（协议/源漂移拒绝续用旧状态与旧分数） ────────────────────────

    def _preflight_binding(self, state: dict[str, Any]) -> "_RunResult | None":
        binding = self.store.verify_binding(self.manifest)
        if not binding.get("ok"):
            error = str(binding.get("error") or "manifest_drift")
            status = (
                st.STATUS_BLOCKED_SOURCE_CHANGED
                if error in {"bundle_changed", "manifest_drift"}
                else st.STATUS_BLOCKED_PROTOCOL_DRIFT
            )
            self._mark_status(status, str(binding.get("reason") or error))
            return _RunResult(status, str(state.get("phase") or ""), str(binding.get("reason") or error))
        integrity = self.manifest.verify_integrity()
        if not integrity.get("ok"):
            reason = str(integrity.get("reason") or "source_changed")
            status = (
                st.STATUS_BLOCKED_SOURCE_CHANGED
                if reason == "blocked_source_changed"
                else st.STATUS_BLOCKED_PROTOCOL_DRIFT
            )
            self._mark_status(status, reason, detail=integrity)
            return _RunResult(status, str(state.get("phase") or ""), reason)
        check = verify_case_freeze(self.freeze, self.cases, protocol=self.freeze.protocol)
        if not check.get("ok"):
            reason = str(check.get("error") or "case_protocol_drift")
            self._mark_status(st.STATUS_BLOCKED_PROTOCOL_DRIFT, reason, detail=check)
            return _RunResult(st.STATUS_BLOCKED_PROTOCOL_DRIFT, str(state.get("phase") or ""), reason)
        return None

    def _reconcile_open_reservations(self) -> dict[str, Any]:
        """崩溃恢复对账：有回执→按回执结算采纳；无回执→释放（不重复扣费，不重发调用）。"""
        state = self._require_state()
        open_items = dict(state.get("open_reservations") or {})
        settled: list[str] = []
        released: list[str] = []
        for key in sorted(open_items):
            record = self.store.pop_reservation(key)
            if record is None:
                continue
            if self._adopt_receipt_if_any(key, record):
                settled.append(key)
                continue
            self.budget.release(str(record.get("handle") or ""))
            self.store.record_release(
                category=str(record.get("category") or ""), tokens=int(record.get("tokens") or 0)
            )
            released.append(key)
            self._reset_case_waiting(key)
        if settled or released:
            self._save_under_lock()
        return {"settled": settled, "released": released}

    def _adopt_receipt_if_any(self, key: str, record: dict[str, Any]) -> bool:
        """Case 执行的开放预留：执行器留有回执则采纳终态并结算一次（不重发调用）。"""
        parsed = _parse_execution_key(key)
        if parsed is None:
            return False
        candidate_id, case_id, target = parsed
        getter = getattr(self.executor, "get_receipt", None)
        receipt = getter(candidate_id, case_id) if callable(getter) else None
        if not isinstance(receipt, dict):
            return False
        usage = _receipt_usage(receipt)
        self.budget.settle(str(record.get("handle") or ""), usage)
        self.store.record_settlement(
            category=str(record.get("category") or CATEGORY_EXECUTION),
            reserved_tokens=int(record.get("tokens") or 0),
            used_tokens=usage,
            ok=True,
        )
        self._persist_outcome(
            case_id=case_id,
            outcome_dict=receipt,
            target=target,
            usage=usage,
        )
        return True

    def _reset_case_waiting(self, key: str) -> None:
        parsed = _parse_execution_key(key)
        if parsed is None:
            return
        _candidate_id, case_id, target = parsed
        bucket = _cases_bucket(self._require_state(), target)
        entry = (bucket or {}).get(case_id)
        if entry is not None and entry.get("status") == CASE_IN_FLIGHT:
            entry["status"] = CASE_PENDING

    def _persist_outcome(self, *, case_id: str, outcome_dict: dict[str, Any], target: str, usage: int) -> None:
        """回执采纳路径的终态落盘（baseline/round/finalization 三桶之一）。"""
        bucket = _cases_bucket(self._require_state(), target)
        if bucket is None:
            return
        entry = bucket.setdefault(case_id, {"status": CASE_PENDING, "outcome": None, "attempts": 0})
        entry["status"] = CASE_DONE
        entry["outcome"] = outcome_dict
        entry["usage_tokens"] = usage
        entry.pop("reservation_key", None)

    # ── 阶段：基线评测（逐 Case 断点落盘，重启不重跑已完成 Case） ─────────────

    def _run_baseline(self) -> str:
        state = self._require_state()
        s_cases = self._cases_for_roles(("S",))
        candidate = self._seed_candidate()
        bucket = state["baseline"]["cases"]
        disposition = self._evaluate_cases_into(
            candidate=candidate,
            cases=s_cases,
            bucket=bucket,
            category=CATEGORY_BASELINE,
            key_prefix=f"baseline:{self.manifest.search_id}",
            estimate_tokens=self.config.case_estimate_tokens,
        )
        if disposition != "ok":
            return disposition
        finalize = ev.finalize_score_vector(
            freeze=self._derived_freeze(self.s_case_ids),
            outcomes=_outcomes_from_bucket(bucket),
            candidate=candidate,
            manifest=self.manifest,
        )
        state["baseline"]["finalize"] = finalize
        if not finalize.get("ok"):
            self._mark_failed(str(finalize.get("reason") or "baseline_invalid"), detail=finalize)
            return "failed"
        if not state["baseline"].get("admitted"):
            result = self.pool.register_seed(
                candidate_id=SEED_CANDIDATE_ID,
                score_vector=dict(finalize.get("score_vector") or {}),
                content_hash=str(candidate.get("content_hash") or ""),
                protocol_hash=str(self.manifest.eval_protocol_fingerprint or ""),
                finalize_result=finalize,
                evaluation_ref=f"baseline:{self.manifest.search_id}",
            )
            if not result.get("ok"):
                self._mark_failed("seed_admission_rejected", detail=result)
                return "failed"
            state["baseline"]["admitted"] = True
            self._persist_pool()
        self._advance_phase(st.PHASE_SEARCHING, round_index=1)
        return "ok"

    # ── 阶段：搜索轮次（初期单变异链） ──────────────────────────────────────

    def _run_searching(self) -> str:
        state = self._require_state()
        round_state = state["round"]
        round_phase = str(round_state.get("phase") or st.ROUND_PHASE_PARENT)
        self._fault(round_phase)
        if round_phase == st.ROUND_PHASE_PARENT:
            return "ok" if self._stage_parent_selection() else "failed"
        if round_phase == st.ROUND_PHASE_FEEDBACK:
            return self._stage_feedback()
        if round_phase == st.ROUND_PHASE_MUTATION:
            return self._stage_mutation()
        if round_phase == st.ROUND_PHASE_VALIDATION:
            return self._stage_validation()
        if round_phase == st.ROUND_PHASE_DEPLOYMENT:
            return self._stage_deployment()
        if round_phase == st.ROUND_PHASE_EVALUATION:
            return self._stage_evaluation()
        if round_phase == st.ROUND_PHASE_JUDGING:
            return self._stage_judging()
        self._mark_failed("unknown_round_phase", detail={"phase": round_phase})
        return "failed"

    def _stage_parent_selection(self) -> bool:
        """抽父代：前沿+RNG 的确定函数；父代与 RNG 推进同笔原子落盘（重启不重抽）。"""
        state = self._require_state()
        round_state = state["round"]
        if round_state.get("parent_draw") is not None and round_state.get("parent_id"):
            round_state["phase"] = st.ROUND_PHASE_FEEDBACK
            self._save_under_lock()
            return True
        frontier = sel.compute_active_frontier(
            self.pool.candidates, case_ids=list(self.s_case_ids)
        )
        sampler = sel.ParentSampler(
            seed=int((state.get("sampler") or {}).get("seed") or 0),
            state=(state.get("sampler") or {}).get("state"),
        )
        draw = sampler.draw(frontier)
        if not draw.get("ok"):
            self._mark_failed("parent_selection_failed", detail=draw)
            return False
        round_state["parent_id"] = str(draw.get("parent_id") or "")
        round_state["parent_draw"] = {
            "parent_id": draw.get("parent_id"),
            "draw_index": draw.get("draw_index"),
            "frontier": list(draw.get("frontier_order") or []),
            "weights": dict(draw.get("weights") or {}),
            "total_weight": draw.get("total_weight"),
            "sampler_version": draw.get("sampler_version"),
        }
        state["sampler"]["state"] = sampler.get_state()
        round_state["phase"] = st.ROUND_PHASE_FEEDBACK
        self._save_under_lock()
        return True

    def _stage_feedback(self) -> str:
        state = self._require_state()
        round_state = state["round"]
        if round_state.get("feedback_ref"):
            round_state["phase"] = st.ROUND_PHASE_MUTATION
            self._save_under_lock()
            return "ok"
        parent_id = str(round_state.get("parent_id") or "")
        parent = self.pool.get_candidate(parent_id)
        if parent is None:
            self._mark_failed("parent_not_in_pool", detail={"parent_id": parent_id})
            return "failed"
        evaluations = self._parent_evaluations(parent_id)
        if not evaluations:
            self._mark_failed("parent_evaluations_missing", detail={"parent_id": parent_id})
            return "failed"
        _parent_text, parent_spec, _cumulative = self._parent_materials(round_state)
        reservation_key = f"feedback:{state['search_id']}:r{round_state['index']}"
        reserve_error = self._reserve(reservation_key, CATEGORY_REFLECTION, self.config.reflection_tokens)
        if isinstance(reserve_error, str):
            return reserve_error
        handle = dict(state["open_reservations"][reservation_key])
        proposals: list[dict[str, Any]] = []
        if self.attributor is not None:
            proposals = list(
                self.attributor(
                    {
                        "parent": dict(parent),
                        "cases": self._cases_for_roles(("S",)),
                        "evaluations": evaluations,
                        "manifest": self.manifest,
                    }
                )
                or []
            )
        record = fb.compile_feedback(
            parent={"candidate_id": parent_id, "generation": int(parent.get("iteration") or 0)},
            freeze=self._derived_freeze(self.s_case_ids),
            cases=self._cases_for_roles(("S",)),
            evaluations=evaluations,
            trace_store=self.trace_store,
            attributions=proposals,
            module_spec=parent_spec,
            manifest=self.manifest,
            trace_refs=self._trace_refs(parent_id),
        )
        path = self.store.save_artifact(f"feedback-r{round_state['index']}.json", record, subdir="feedback")
        round_state["feedback_ref"] = str(path)
        usage = self.config.reflection_tokens
        self._settle(reservation_key, handle, usage, ok=True)
        gate = record.get("mutation_gate") or {}
        if not gate.get("allowed"):
            round_state["no_mutation_reason"] = str(gate.get("reason") or "mutation_gate_closed")
            round_state["phase"] = st.ROUND_PHASE_JUDGING
            self._save_under_lock()
            return "ok"
        round_state["phase"] = st.ROUND_PHASE_MUTATION
        self._save_under_lock()
        return "ok"

    def _stage_mutation(self) -> str:
        state = self._require_state()
        round_state = state["round"]
        if round_state.get("patch") is not None:
            round_state["phase"] = st.ROUND_PHASE_VALIDATION
            self._save_under_lock()
            return "ok"
        feedback_record = self._load_feedback(round_state)
        if feedback_record is None:
            self._mark_failed("feedback_record_missing", detail={})
            return "failed"
        target = str((feedback_record.get("mutation_gate") or {}).get("module_id") or "")
        if not target:
            self._mark_failed("mutation_target_missing", detail={})
            return "failed"
        reservation_key = f"mutation:{state['search_id']}:r{round_state['index']}"
        reserve_error = self._reserve(reservation_key, CATEGORY_MUTATION, self.config.mutation_tokens)
        if isinstance(reserve_error, str):
            return reserve_error
        handle = dict(state["open_reservations"][reservation_key])
        parent_text, parent_spec, cumulative = self._parent_materials(round_state)
        if parent_text is None:
            self._settle(reservation_key, handle, 0, ok=False)
            self._mark_failed("parent_materials_missing", detail={"parent_id": round_state.get("parent_id")})
            return "failed"
        brief = mu.build_mutation_input(
            spec=parent_spec,
            raw_text=parent_text,
            target_module=target,
            brief={
                "lesson": self._lesson_of(feedback_record),
                "feedback_ref": round_state.get("feedback_ref"),
                "search_id": self.manifest.search_id,
                "base_text_sha256": _sha256_text(parent_text),
            },
            rejection_history=self._rejections_history(),
            budget=mu.freeze_budget(),
        )
        patch: dict[str, Any] | None = None
        if self.patch_provider is not None:
            raw = self.patch_provider(brief)
            patch = raw if isinstance(raw, dict) else None
        round_state["patch"] = patch
        round_state["patch_meta"] = {
            "target_module": target,
            "base_text_sha256": _sha256_text(parent_text),
        }
        self._settle(reservation_key, handle, self.config.mutation_tokens, ok=True)
        round_state["phase"] = st.ROUND_PHASE_VALIDATION
        self._save_under_lock()
        return "ok"

    def _stage_validation(self) -> str:
        """确定性合并：apply_candidate_patch + 受信包装 + 候选制品落搜索命名空间。"""
        state = self._require_state()
        round_state = state["round"]
        if round_state.get("candidate_id") and round_state.get("candidate_ref"):
            round_state["phase"] = st.ROUND_PHASE_DEPLOYMENT
            self._save_under_lock()
            return "ok"
        patch = round_state.get("patch")
        if not isinstance(patch, dict):
            round_state["phase"] = st.ROUND_PHASE_JUDGING
            round_state["no_mutation_reason"] = round_state.get("no_mutation_reason") or "no_patch"
            self._save_under_lock()
            return "ok"
        parent_text, parent_spec, cumulative = self._parent_materials(round_state)
        applied = mu.apply_candidate_patch(
            parent_spec,
            parent_text or "",
            patch,
            policy=mu.MutationPolicy(enable_bounded_module_edit=True),
            budget=mu.freeze_budget(),
            gate_target_module=str((round_state.get("patch_meta") or {}).get("target_module") or ""),
            skill_text_sha256=str((round_state.get("patch_meta") or {}).get("base_text_sha256") or ""),
            cumulative=cumulative,
            rejection_history=self._rejections_history(),
            scope=self.manifest.search_id,
        )
        self._save_rejections()
        if not applied.get("ok"):
            round_state["patch_meta"]["rejected"] = {
                "error": str(applied.get("error") or ""),
                "reason": str(applied.get("reason") or ""),
            }
            round_state["phase"] = st.ROUND_PHASE_JUDGING
            self._save_under_lock()
            return "ok"
        lesson = self._lesson_of(self._load_feedback(round_state) or {})
        packaged = mu.package_candidate(
            str(applied.get("candidate_text") or ""), lesson=lesson, rationale="DE-34 bounded module edit"
        )
        if not packaged.get("ok"):
            self._mark_failed("candidate_packaging_failed", detail=packaged)
            return "failed"
        candidate_id = f"cand-r{int(round_state.get('index') or 0)}"
        candidate_dir = self.store.candidate_dir(candidate_id)
        saved = mu.save_candidate_text(self.manifest, str(candidate_dir), packaged)
        if not saved.get("ok"):
            self._mark_failed("candidate_artifact_rejected", detail=saved)
            return "failed"
        child_spec = applied.get("child_spec")
        if child_spec is not None:
            from .contracts import save_candidate_module_spec

            spec_saved = save_candidate_module_spec(self.manifest, str(candidate_dir), child_spec)
            if not spec_saved.get("ok"):
                self._mark_failed("candidate_spec_rejected", detail=spec_saved)
                return "failed"
        snapshot = {
            "name": str(packaged.get("bundle_name") or ""),
            "description": str(packaged.get("bundle_description") or ""),
            "when_to_use": str(packaged.get("bundle_when_to_use") or ""),
            "instructions": str(packaged.get("final_body") or ""),
        }
        normalized = ev.normalize_candidate({"candidate_id": candidate_id, "snapshot": dict(snapshot)})
        round_state["candidate_id"] = candidate_id
        round_state["candidate_ref"] = str(candidate_dir)
        round_state["candidate_meta"] = {
            "snapshot": snapshot,
            "content_hash": str(normalized.get("content_hash") or ""),
            "final_content_hash": str(packaged.get("final_content_hash") or ""),
            "final_text_sha256": str(packaged.get("final_text_sha256") or ""),
            "business_diff": str(applied.get("business_diff") or ""),
            "cumulative": dict(applied.get("cumulative") or {}),
            "candidate_dir": str(candidate_dir),
        }
        round_state["phase"] = st.ROUND_PHASE_DEPLOYMENT
        self._save_under_lock()
        return "ok"

    def _stage_deployment(self) -> str:
        """隔离载入：候选挂载回执（显式隔离载入通道，不通过改 tier 强行生效）。"""
        state = self._require_state()
        round_state = state["round"]
        if round_state.get("mount_receipt"):
            round_state["phase"] = st.ROUND_PHASE_EVALUATION
            self._save_under_lock()
            return "ok"
        receipt: dict[str, Any]
        if self.isolation is None:
            receipt = {"ok": True, "mode": "no_isolation_port", "candidate_id": round_state.get("candidate_id")}
        else:
            candidate = self._round_candidate(round_state)
            if candidate is None:
                self._mark_failed("candidate_unavailable", detail={})
                return "failed"
            text = str((round_state.get("candidate_meta") or {}).get("candidate_dir") or "")
            receipt = self.isolation.mount(str(round_state.get("candidate_id") or ""), text)
            if not isinstance(receipt, dict) or not receipt.get("ok"):
                self._mark_failed("candidate_mount_rejected", detail={"receipt": _jsonable(receipt)})
                return "failed"
        round_state["mount_receipt"] = receipt
        round_state["phase"] = st.ROUND_PHASE_EVALUATION
        self._save_under_lock()
        return "ok"

    def _stage_evaluation(self) -> str:
        state = self._require_state()
        round_state = state["round"]
        candidate = self._round_candidate(round_state)
        if candidate is None:
            self._mark_failed("candidate_unavailable", detail={})
            return "failed"
        bucket = round_state["cases"]
        disposition = self._evaluate_cases_into(
            candidate=candidate,
            cases=self._cases_for_roles(("S",)),
            bucket=bucket,
            category=CATEGORY_EXECUTION,
            key_prefix=f"eval:{state['search_id']}:r{round_state['index']}:{candidate['candidate_id']}",
            estimate_tokens=self.config.case_estimate_tokens,
            retry_category=CATEGORY_RETRY,
            retry_estimate=self.config.retry_estimate_tokens,
        )
        if disposition != "ok":
            return disposition
        dead = [cid for cid, entry in bucket.items() if entry.get("status") == CASE_INFRA_DEAD]
        finalize = ev.finalize_score_vector(
            freeze=self._derived_freeze(self.s_case_ids),
            outcomes=_outcomes_from_bucket(bucket),
            candidate=candidate,
            manifest=self.manifest,
        )
        round_state["finalize"] = finalize
        if dead or not finalize.get("ok"):
            round_state["blocked_reason"] = str(finalize.get("reason") or "infra_dead")
            round_state["phase"] = st.ROUND_PHASE_JUDGING
            self._save_under_lock()
            return "ok"
        round_state["phase"] = st.ROUND_PHASE_JUDGING
        self._save_under_lock()
        return "ok"

    def _stage_judging(self) -> str:
        state = self._require_state()
        round_state = state["round"]
        patch = round_state.get("patch")
        candidate_id = str(round_state.get("candidate_id") or "")
        if not isinstance(patch, dict):
            # 无变异轮（变异门关闭/无 patch）：关闭轮次，不产生入池尝试
            self._close_round(admitted=False, decision="no_patch")
            return "ok"
        patch_meta = round_state.get("patch_meta") or {}
        if patch_meta.get("rejected"):
            self.pool.record_failed_attempt(
                candidate_id=candidate_id,
                parent_id=str(round_state.get("parent_id") or ""),
                reason=str(patch_meta["rejected"].get("reason") or "patch_rejected"),
                candidate_hash="",
                protocol_hash=str(self.manifest.eval_protocol_fingerprint or ""),
                completed_cases=0,
                status=pl.ATTEMPT_FAILED,
            )
            self._persist_pool()
            self._close_round(admitted=False, decision="patch_rejected")
            return "ok"
        if round_state.get("blocked_reason"):
            self.pool.record_failed_attempt(
                candidate_id=candidate_id,
                parent_id=str(round_state.get("parent_id") or ""),
                reason=str(round_state.get("blocked_reason") or "round_blocked"),
                candidate_hash=str((round_state.get("candidate_meta") or {}).get("content_hash") or ""),
                protocol_hash=str(self.manifest.eval_protocol_fingerprint or ""),
                completed_cases=sum(
                    1 for e in (round_state.get("cases") or {}).values() if e.get("status") == CASE_DONE
                ),
                status=pl.ATTEMPT_BLOCKED,
            )
            self._persist_pool()
            self._close_round(admitted=False, decision="blocked")
            return "ok"
        reservation_key = f"judge:{state['search_id']}:r{round_state['index']}"
        reserve_error = self._reserve(reservation_key, CATEGORY_JUDGE, self.config.judge_tokens)
        if isinstance(reserve_error, str):
            return reserve_error
        handle = dict(state["open_reservations"][reservation_key])
        usage = self.config.judge_tokens
        if self.judge is not None:
            verdict = self.judge(
                {
                    "candidate_id": candidate_id,
                    "finalize": round_state.get("finalize"),
                    "cases": dict(round_state.get("cases") or {}),
                }
            )
            usage = int((verdict or {}).get("used_tokens") or usage)
        self._settle(reservation_key, handle, usage, ok=True)
        candidate_meta = round_state.get("candidate_meta") or {}
        decision = self.pool.admit(
            candidate_id=candidate_id,
            parent_id=str(round_state.get("parent_id") or ""),
            finalize_result=round_state.get("finalize"),
            content_hash=str(candidate_meta.get("content_hash") or ""),
            protocol_hash=str(self.manifest.eval_protocol_fingerprint or ""),
            cumulative_seed_diff=candidate_meta.get("cumulative"),
            evaluation_ref=f"{self.manifest.search_id}:r{round_state.get('index')}",
        )
        self._persist_pool()
        round_state["decision"] = str(decision.get("decision") or "")
        self._close_round(admitted=bool(decision.get("ok")), decision=str(decision.get("decision") or ""))
        return "ok"

    def _close_round(self, *, admitted: bool, decision: str) -> None:
        state = self._require_state()
        round_state = state["round"]
        summary = {
            "index": int(round_state.get("index") or 0),
            "round_id": str(round_state.get("round_id") or ""),
            "parent_id": round_state.get("parent_id") or "",
            "candidate_id": round_state.get("candidate_id") or "",
            "decision": decision,
            "admitted": bool(admitted),
            "cases": {cid: dict(entry) for cid, entry in (round_state.get("cases") or {}).items()},
            "candidate_meta": dict(round_state.get("candidate_meta") or {}),
            "failure_attempts": int(round_state.get("failure_attempts") or 0),
            "finalize": round_state.get("finalize"),
        }
        state["completed_rounds"].append(summary)
        state["history"].append({"event": "round_closed", "decision": decision, "admitted": admitted})
        self._save_rejections()
        if int(round_state.get("index") or 0) >= int(self.config.max_iterations):
            self._advance_phase(st.PHASE_FINALIZING)
            return
        # 单变异链：下一轮在上一轮关闭后才建立（并发评测完成顺序不改搜索路径）
        state["round"] = _new_round_state(int(round_state.get("index") or 0) + 1)
        self._save_under_lock()

    # ── 阶段：最终验证与冠军锁定 ────────────────────────────────────────────

    def _run_finalizing(self) -> str:
        state = self._require_state()
        champion_result = sel.select_search_champion(
            self.pool.candidates, case_ids=list(self.s_case_ids)
        )
        if not champion_result.get("ok"):
            self._mark_failed("champion_selection_failed", detail=champion_result)
            return "failed"
        champion_id = str(champion_result.get("search_champion") or "")
        state["finalization"]["champion"] = champion_id
        state["finalization"]["champion_meta"] = {
            "tie_key": _jsonable(champion_result.get("champion_tie_key") or {}),
            "algorithm_version": str(champion_result.get("algorithm_version") or ""),
        }
        v_case_ids = sorted(self.freeze.roles.get("V") or [])
        if v_case_ids:
            champion = self._candidate_of(champion_id)
            if champion is None:
                self._mark_failed("champion_unavailable", detail={"champion": champion_id})
                return "failed"
            v_cases = [dict(c) for c in self.cases if str(c.get("case_id") or "") in set(v_case_ids)]
            bucket = state["finalization"]["cases"]
            disposition = self._evaluate_cases_into(
                candidate=champion,
                cases=v_cases,
                bucket=bucket,
                category=CATEGORY_FINAL_VERIFICATION,
                key_prefix=f"final:{state['search_id']}:{champion_id}",
                estimate_tokens=self.config.final_case_tokens,
            )
            if disposition != "ok":
                return disposition
            finalize = ev.finalize_score_vector(
                freeze=self._derived_freeze(v_case_ids),
                outcomes=_outcomes_from_bucket(bucket),
                candidate=champion,
                manifest=self.manifest,
            )
            state["finalization"]["verification"] = {
                "kind": "V_replay",
                "case_ids": v_case_ids,
                "ok": bool(finalize.get("ok")),
                "finalize": finalize,
            }
        else:
            state["finalization"]["verification"] = {
                "kind": "dev_only",
                "executed": False,
                "grade": self.freeze.promotion_grade,
                "note": "仅 S 研发验证：搜索冠军不具备正式晋级证据（§4）",
            }
        self._advance_phase(st.PHASE_AWAITING_REVIEW)
        return "ok"

    # ── 阶段：清理（恢复隔离种子挂载；失败 → cleanup_failed 阻断发布） ────────

    def _run_cleanup(self) -> str:
        self._fault("cleanup")
        if self.isolation is None:
            receipt = {"ok": False, "mode": "no_isolation_port", "reason": "isolation_restore_receipt_missing"}
        else:
            receipt = self.isolation.restore_seed()
            if not isinstance(receipt, dict):
                receipt = {"ok": False, "reason": "restore_receipt_invalid", "raw": _jsonable(receipt)}
        return self._finish_cleanup(receipt)

    def _finish_cleanup(self, receipt: dict[str, Any]) -> str:
        state = self._require_state()
        self.store.save_receipt(f"restore-seed-{state['search_id']}.json", receipt)
        restored = bool(receipt.get("ok"))
        blockers: list[str] = []
        if not restored:
            blockers.append("isolation_restore_failed")
        verification = (state.get("finalization") or {}).get("verification") or {}
        if str(verification.get("kind") or "") == "dev_only":
            blockers.append("dev_only_grade_no_promotion_evidence")
        if str(verification.get("kind") or "") == "V_replay" and verification.get("ok") is False:
            blockers.append("v_verification_failed")
        champion = str((state.get("finalization") or {}).get("champion") or "")
        if not champion:
            blockers.append("champion_missing")
        state["cleanup"] = {
            "restored": restored,
            "receipt": receipt,
            "can_publish": restored and bool(champion),
            "release_blockers": blockers,
        }
        if not restored:
            state["status"] = st.STATUS_CLEANUP_FAILED
            state["block_reason"] = "isolation_restore_failed"
            self._save_under_lock()
            return "cleanup_failed"
        if state.get("cancel_requested"):
            state["status"] = st.STATUS_CANCELLED
            self._save_under_lock()
            return "cancelled"
        state["phase"] = st.PHASE_COMPLETED
        state["status"] = st.PHASE_COMPLETED
        self._save_under_lock()
        return "completed"

    def _finish_cancelled(self) -> _RunResult:
        """取消：停止新执行（主循环入口即检查），恢复挂载后落终态。"""
        state = self._require_state()
        if (state.get("cleanup") or {}).get("restored") is not True and state.get("phase") != st.PHASE_COMPLETED:
            disposition = self._run_cleanup()
            if disposition == "cleanup_failed":
                return _RunResult(st.STATUS_CLEANUP_FAILED, str(state.get("phase") or ""), "cleanup_failed")
        state = self._require_state()
        state["status"] = st.STATUS_CANCELLED
        self._save_under_lock()
        return _RunResult(st.STATUS_CANCELLED, str(state.get("phase") or ""), str(state.get("cancel_note") or ""))

    # ── Case 执行核心（预留→执行→落终态→结算；取消/预算/故障处理） ────────────

    def _evaluate_cases_into(
        self,
        *,
        candidate: dict[str, Any],
        cases: list[dict[str, Any]],
        bucket: dict[str, Any],
        category: str,
        key_prefix: str,
        estimate_tokens: int,
        retry_category: str | None = None,
        retry_estimate: int | None = None,
    ) -> str:
        """有界并发逐 Case 执行；返回 disposition（ok / waiting_budget / cancelled）。"""
        for attempt in range(int(self.config.max_infra_retries_per_case) + 1):
            pending = [
                dict(case)
                for case in cases
                if (bucket.get(str(case.get("case_id") or "")) or {}).get("status") in (None, CASE_PENDING)
            ]
            if not pending:
                return "ok"
            self._fault(f"{category}:attempt{attempt + 1}")
            category_now = category if attempt == 0 else (retry_category or category)
            estimate_now = estimate_tokens if attempt == 0 else (retry_estimate or estimate_tokens)
            reservations: dict[str, dict[str, Any]] = {}
            for case in pending:
                if self._require_state().get("cancel_requested"):
                    break
                case_id = str(case.get("case_id") or "")
                key = f"{key_prefix}:{case_id}:a{attempt + 1}"
                error = self._reserve(key, category_now, estimate_now)
                if isinstance(error, str):
                    self._save_under_lock()
                    return error  # waiting_budget：Later 语义，不消耗失败次数
                reservations[case_id] = {"key": key, "record": dict(self.store.state["open_reservations"][key])}
            if self._require_state().get("cancel_requested"):
                for item in reservations.values():
                    self._release_reservation(str(item["key"]), dict(item["record"]))
                self._save_under_lock()
                return "cancelled"
            if reservations:
                entries = {
                    case_id: bucket.setdefault(
                        case_id, {"status": CASE_PENDING, "outcome": None, "attempts": 0, "last_error": ""}
                    )
                    for case_id in reservations
                }
                for case_id, item in reservations.items():
                    entry = entries[case_id]
                    entry["status"] = CASE_IN_FLIGHT
                    entry["attempts"] = int(entry.get("attempts") or 0) + 1
                    entry["reservation_key"] = item["key"]
                self._save_under_lock()
                batch_cases = [c for c in pending if str(c.get("case_id") or "") in reservations]
                outcome = self._execute_batch(
                    candidate=candidate,
                    cases=batch_cases,
                    entries=entries,
                    reservations=reservations,
                    category=category_now,
                )
                if outcome == "cancelled":
                    return "cancelled"
        state = self._require_state()
        still_pending = [
            case_id
            for case_id, entry in bucket.items()
            if entry.get("status") in (CASE_PENDING, CASE_IN_FLIGHT)
        ]
        if still_pending:
            for case_id in still_pending:
                bucket[case_id]["status"] = CASE_INFRA_DEAD
            state["round"]["failure_attempts"] = int(state["round"].get("failure_attempts") or 0) + len(still_pending)
            self._save_under_lock()
        return "ok"

    def _execute_batch(
        self,
        *,
        candidate: dict[str, Any],
        cases: list[dict[str, Any]],
        entries: dict[str, dict[str, Any]],
        reservations: dict[str, dict[str, Any]],
        category: str,
    ) -> str:
        """有界并发执行一批 Case；完成即落终态+结算；FaultInjected 直接上抛（崩溃语义）。"""
        protocol_payload = {
            "fingerprint": self.manifest.eval_protocol_fingerprint,
            "bundle_hash": self.manifest.bundle_hash,
            # 评测 scope 绑定搜索命名空间：多空间/多搜索的缓存与回执不可能串账
            "scope": self._scope_id(),
        }
        max_workers = max(1, int(self.config.max_case_workers))
        if max_workers == 1 or len(cases) <= 1:
            for case in cases:
                if self._require_state().get("cancel_requested"):
                    self._release_inflight(reservations, entries)
                    self._save_under_lock()
                    return "cancelled"
                try:
                    value = self._execute_one(case, candidate, protocol_payload)
                except FaultInjected:
                    raise
                except Exception as exc:  # noqa: BLE001 - 执行器基础设施异常按重试策略处理
                    self._record_case_error(case, entries, reservations, exc, category)
                    continue
                self._record_case_outcome(case, entries, reservations, value, category)
            return "cancelled" if self._require_state().get("cancel_requested") else ""
        results: dict[str, Any] = {}
        workers = min(max_workers, len(cases))
        with ThreadPoolExecutor(max_workers=workers) as pool_exec:
            futures = {
                pool_exec.submit(self._execute_one, case, candidate, protocol_payload): str(case.get("case_id") or "")
                for case in cases
            }
            for future, case_id in futures.items():
                try:
                    results[case_id] = future.result()
                except BaseException as exc:  # noqa: BLE001 - 故障/异常统一归类后处理
                    results[case_id] = exc
        for case in cases:
            case_id = str(case.get("case_id") or "")
            value = results.get(case_id)
            if isinstance(value, BaseException):
                if isinstance(value, FaultInjected):
                    raise value
                self._record_case_error(case, entries, reservations, value, category)
                continue
            self._record_case_outcome(case, entries, reservations, value, category)
        return "cancelled" if self._require_state().get("cancel_requested") else ""

    def _execute_one(self, case: dict[str, Any], candidate: dict[str, Any], protocol: dict[str, Any]) -> Any:
        value = self.executor.execute(case=case, candidate=candidate, frozen_protocol=protocol)
        if inspect.isawaitable(value):
            value = asyncio.run(value)
        return value

    def _record_case_outcome(
        self,
        case: dict[str, Any],
        entries: dict[str, dict[str, Any]],
        reservations: dict[str, dict[str, Any]],
        outcome: Any,
        category: str,
    ) -> None:
        case_id = str(case.get("case_id") or "")
        outcome_dict = outcome.to_dict() if hasattr(outcome, "to_dict") else dict(outcome or {})
        usage = _receipt_usage(outcome_dict)
        item = reservations.get(case_id) or {}
        record = item.get("record") or {}
        with self._lock:
            self.budget.settle(str(record.get("handle") or ""), usage)
            self.store.pop_reservation(str(item.get("key") or ""))
            self.store.record_settlement(
                category=category,
                reserved_tokens=int(record.get("tokens") or 0),
                used_tokens=usage,
                ok=bool(outcome_dict.get("valid")),
            )
            entry = entries.get(case_id) or {}
            entry["status"] = CASE_DONE
            entry["outcome"] = outcome_dict
            entry["usage_tokens"] = usage
            entry.pop("reservation_key", None)
            self.store.save()

    def _record_case_error(
        self,
        case: dict[str, Any],
        entries: dict[str, dict[str, Any]],
        reservations: dict[str, dict[str, Any]],
        exc: BaseException,
        category: str,
    ) -> None:
        """执行器异常（infra）：终态无可比分数；结算归集后由重试策略处理（计入失败次数）。"""
        case_id = str(case.get("case_id") or "")
        item = reservations.get(case_id) or {}
        record = item.get("record") or {}
        with self._lock:
            self.budget.settle(str(record.get("handle") or ""), 0, ok=False)
            self.store.pop_reservation(str(item.get("key") or ""))
            self.store.record_settlement(
                category=category,
                reserved_tokens=int(record.get("tokens") or 0),
                used_tokens=0,
                ok=False,
            )
            entry = entries.get(case_id) or {}
            entry["status"] = CASE_PENDING
            entry["last_error"] = f"{type(exc).__name__}: {exc}"
            state = self._require_state()
            state["round"]["failure_attempts"] = int(state["round"].get("failure_attempts") or 0) + 1
            self.store.save()

    def _release_inflight(
        self, reservations: dict[str, dict[str, Any]], entries: dict[str, dict[str, Any]]
    ) -> None:
        for case_id, item in reservations.items():
            entry = entries.get(case_id) or {}
            if entry.get("status") == CASE_IN_FLIGHT:
                self._release_reservation(str(item.get("key") or ""), dict(item.get("record") or {}))
                entry["status"] = CASE_PENDING

    def _release_reservation(self, key: str, record: dict[str, Any]) -> None:
        self.budget.release(str(record.get("handle") or ""))
        if key:
            self.store.pop_reservation(key)
        self.store.record_release(
            category=str(record.get("category") or ""), tokens=int(record.get("tokens") or 0)
        )

    # ── 预算端口薄封装（Later 语义：budget_exhausted → waiting_budget，不计失败） ──

    def _reserve(self, key: str, category: str, tokens: int) -> str | None:
        """预留并先落盘（副作用前可见）；预算不足返回 waiting_budget（不计失败次数）。"""
        try:
            handle = self.budget.reserve(category, int(tokens), key)
        except Exception as exc:  # noqa: BLE001 - BudgetError 及同形异常按 Later 处理
            code = str(getattr(exc, "code", "") or "")
            if "budget" in code or "budget" in str(exc).lower():
                state = self._require_state()
                state["status"] = st.STATUS_BLOCKED_BUDGET
                state["block_reason"] = code or str(exc)
                self._save_under_lock()
                return "waiting_budget"
            raise
        with self._lock:
            state = self._require_state()
            state["status"] = st.STATUS_RUNNING
            state["block_reason"] = ""
            self.store.record_reservation(
                key=key,
                category=category,
                handle=str((handle or {}).get("handle") or ""),
                tokens=int((handle or {}).get("tokens") or tokens),
            )
        return None

    def _settle(self, key: str, handle: dict[str, Any], used: int, *, ok: bool) -> None:
        self.budget.settle(str(handle.get("handle") or ""), int(used), ok=ok)
        self.store.record_settlement(
            category=str(handle.get("category") or ""),
            reserved_tokens=int(handle.get("tokens") or 0),
            used_tokens=int(used),
            ok=ok,
        )
        self.store.pop_reservation(key)
        self._save_under_lock()

    # ── 状态推进小工具 ──────────────────────────────────────────────────────

    def _advance_phase(self, phase: str, *, round_index: int | None = None) -> None:
        state = self._require_state()
        state["phase"] = phase
        state["status"] = st.STATUS_RUNNING
        state["block_reason"] = ""
        if round_index is not None and not (state.get("round") or {}).get("round_id"):
            state["round"] = _new_round_state(int(round_index))
        self._save_under_lock()

    def _mark_status(self, status: str, reason: str, *, detail: Any = None) -> None:
        state = self._require_state()
        state["status"] = status
        state["block_reason"] = reason
        if detail is not None:
            state.setdefault("history", []).append(
                {"event": "blocked", "status": status, "reason": reason, "detail": _jsonable(detail)}
            )
        self._save_under_lock()

    def _mark_failed(self, code: str, *, detail: Any = None) -> None:
        self._mark_status(st.STATUS_FAILED, code, detail=detail)

    def _disposition_result(self, disposition: str, phase: str) -> _RunResult:
        state = self._require_state()
        return _RunResult(
            str(state.get("status") or disposition),
            str(state.get("phase") or phase),
            str(state.get("block_reason") or ""),
        )

    # ── 候选材料 / 反馈 / 评测桶 ────────────────────────────────────────────

    def _cases_for_roles(self, roles: tuple[str, ...]) -> list[dict[str, Any]]:
        wanted = set(roles)
        return [dict(c) for c in self.cases if str(c.get("protocol_role") or "") in wanted]

    def _seed_candidate(self) -> dict[str, Any]:
        return ev.normalize_candidate({"candidate_id": SEED_CANDIDATE_ID, "snapshot": self._seed_snapshot()})

    def _seed_snapshot(self) -> dict[str, Any]:
        from ..frontmatter import parse_frontmatter

        text = (Path(self.manifest.skill_dir) / SKILL_TEXT_NAME).read_text(encoding="utf-8-sig")
        parsed = parse_frontmatter(text)
        return {
            "name": str(parsed.meta.get("name") or self.manifest.skill_name),
            "description": str(parsed.meta.get("description") or ""),
            "when_to_use": str(parsed.meta.get("when_to_use") or parsed.meta.get("when-to-use") or ""),
            "instructions": parsed.body,
        }

    def _round_candidate(self, round_state: dict[str, Any]) -> dict[str, Any] | None:
        snapshot = (round_state.get("candidate_meta") or {}).get("snapshot")
        if isinstance(snapshot, dict) and snapshot:
            return ev.normalize_candidate(
                {"candidate_id": str(round_state.get("candidate_id") or ""), "snapshot": dict(snapshot)}
            )
        return None

    def _candidate_of(self, candidate_id: str) -> dict[str, Any] | None:
        if candidate_id == SEED_CANDIDATE_ID:
            return self._seed_candidate()
        state = self._require_state()
        for summary in reversed(state.get("completed_rounds") or []):
            if str(summary.get("candidate_id") or "") == candidate_id:
                snapshot = (summary.get("candidate_meta") or {}).get("snapshot")
                if isinstance(snapshot, dict) and snapshot:
                    return ev.normalize_candidate({"candidate_id": candidate_id, "snapshot": dict(snapshot)})
        round_state = state.get("round") or {}
        if str(round_state.get("candidate_id") or "") == candidate_id:
            return self._round_candidate(round_state)
        return None

    def _parent_materials(self, round_state: dict[str, Any]) -> tuple[str | None, ModuleSpec, dict[str, Any] | None]:
        parent_id = str(round_state.get("parent_id") or "")
        if parent_id == SEED_CANDIDATE_ID:
            path = Path(self.manifest.skill_dir) / SKILL_TEXT_NAME
            return path.read_text(encoding="utf-8-sig"), self.spec, None
        directory = self.store.candidate_dir(parent_id)
        text_path = directory / SKILL_TEXT_NAME
        spec_path = directory / "module-spec.json"
        if not text_path.is_file():
            return None, self.spec, None
        text = text_path.read_text(encoding="utf-8-sig")
        spec = self.spec
        if spec_path.is_file():
            try:
                spec = spec_from_dict(_load_json(spec_path))
            except Exception:
                spec = self.spec
        parent = self.pool.get_candidate(parent_id) or {}
        cumulative = parent.get("cumulative_seed_diff")
        return text, spec, dict(cumulative) if isinstance(cumulative, dict) else None

    def _load_feedback(self, round_state: dict[str, Any]) -> dict[str, Any] | None:
        ref = str(round_state.get("feedback_ref") or "")
        if not ref:
            return None
        try:
            return _load_json(Path(ref))
        except Exception:
            return None

    def _parent_evaluations(self, parent_id: str) -> list[dict[str, Any]]:
        if parent_id == SEED_CANDIDATE_ID:
            bucket = self._require_state()["baseline"]["cases"]
        else:
            bucket = {}
            for summary in reversed(self._require_state().get("completed_rounds") or []):
                if str(summary.get("candidate_id") or "") == parent_id:
                    bucket = summary.get("cases") or {}
                    break
            if not bucket:
                round_state = self._require_state().get("round") or {}
                if str(round_state.get("candidate_id") or "") == parent_id:
                    bucket = round_state.get("cases") or {}
        evaluations: list[dict[str, Any]] = []
        refs = self._trace_refs(parent_id)
        for case_id in sorted(bucket or {}):
            entry = bucket[case_id]
            if entry.get("status") != CASE_DONE or not isinstance(entry.get("outcome"), dict):
                continue
            record = dict(entry["outcome"])
            record.setdefault("trace_ref", refs.get(case_id, f"trace://{parent_id}/{case_id}"))
            evaluations.append(record)
        return evaluations

    def _trace_refs(self, parent_id: str) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for case_id, ref in (self.config.trace_refs or {}).items():
            resolved[case_id] = str(ref).replace("{parent}", parent_id)
        return resolved

    def _rejections_history(self) -> mu.RejectionHistory:
        if self._rejections is None:
            payload = self.store.load_artifact("rejection-history.json")
            if payload is not None:
                try:
                    self._rejections = mu.RejectionHistory.from_dict(payload)
                except Exception:
                    self._rejections = mu.RejectionHistory()
            else:
                self._rejections = mu.RejectionHistory()
        return self._rejections

    def _save_rejections(self) -> None:
        if self._rejections is not None:
            self.store.save_artifact("rejection-history.json", self._rejections.to_dict())

    def _lesson_of(self, feedback_record: dict[str, Any]) -> str:
        items = list(feedback_record.get("mutation_candidates") or [])
        if items:
            first = dict(items[0])
            atoms = ", ".join(str(a) for a in (first.get("failure_atoms") or [])[:3])
            return f"[search:{self.manifest.search_id}] module {first.get('module_id')}: {atoms}"
        gate = feedback_record.get("mutation_gate") or {}
        return f"[search:{self.manifest.search_id}] module {gate.get('module_id')}: bounded edit"


# ── 模块级工具（确定性；无墙钟/随机） ────────────────────────────────────────


def _default_clock() -> float:
    import time

    return time.time()


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


def _receipt_usage(receipt: dict[str, Any]) -> int:
    cost = receipt.get("cost") if isinstance(receipt.get("cost"), dict) else {}
    raw = cost.get("used_tokens", cost.get("tokens", receipt.get("usage_tokens", 0)))
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _outcomes_from_bucket(bucket: dict[str, Any]) -> list[ev.ExecutionOutcome]:
    field_names = set(ev.ExecutionOutcome.__dataclass_fields__)
    outcomes: list[ev.ExecutionOutcome] = []
    for case_id in sorted(bucket or {}):
        entry = bucket[case_id]
        if entry.get("status") != CASE_DONE or not isinstance(entry.get("outcome"), dict):
            continue
        payload = {k: v for k, v in dict(entry["outcome"]).items() if k in field_names}
        outcomes.append(ev.ExecutionOutcome(**payload))
    return outcomes


def _cases_bucket(state: dict[str, Any], target: str) -> dict[str, Any] | None:
    if target == "baseline":
        return state.setdefault("baseline", {}).setdefault("cases", {})
    if target == "final":
        return state.setdefault("finalization", {}).setdefault("cases", {})
    if target.startswith("round:"):
        return state.setdefault("round", {}).setdefault("cases", {})
    return None


def _parse_execution_key(key: str) -> tuple[str, str, str] | None:
    """执行类预留键解析：
    ``baseline:<search>:<case>:a<n>`` / ``eval:<search>:r<i>:<cand>:<case>:a<n>`` /
    ``final:<search>:<cand>:<case>:a<n>`` → (candidate_id, case_id, bucket_target)。
    """
    parts = str(key).split(":")
    if len(parts) < 4 or ":a" not in key:
        return None
    kind = parts[0]
    if kind == "baseline" and len(parts) == 4:
        return SEED_CANDIDATE_ID, parts[-2], "baseline"
    if kind == "eval" and len(parts) == 6:
        return parts[-3], parts[-2], "round:eval"
    if kind == "final" and len(parts) == 5:
        return parts[-3], parts[-2], "final"
    return None


def _jsonable(value: Any) -> Any:
    import json

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)
