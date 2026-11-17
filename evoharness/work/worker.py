"""生产 Worker（R03）：消费 Run 队列的常驻执行装配层。

审核定位（R03/P1）：``api.start_run`` 只建立 queued Run，没有任何 Worker 把
queued Run 交给 ``executor.execute_run`` / 受限适配器执行——排队成功不等于
数字员工开始工作。本模块补上缺失的生产装配：

- **队列消费**：轮询 Run 表条件认领（state=queued，按 created_at 确定性排序），
  认领经 ``execution.claim``（R05：租约 + generation + 授权重验 + 单活执行位
  CAS），不另造调度状态；``scheduling.schedule_pending`` 仍负责"派工"侧
  （决定建哪些 Run），本 Worker 只负责"执行"侧（把已有 queued Run 做完）。
- **执行前预算预留意图**（DE-15 / governance.budget.reserve，按 org/category）：
  预算耗尽/未配置是等待不是失败——Run 保持 queued（不计失败、不刷 attempts），
  恢复后下一轮重试；预留按真实 usage 结算，未执行即释放，不留免费副作用。
- **受限执行**：注入式 ExecutionAdapter（生产装配 ``work.agent_adapter
  .RestrictedAgentAdapter``），复用 ``executor.execute_run`` 的 R05 心跳续租、
  R06 工作区复用与 R07 产物登记（检查回执由适配器在真实检查后签发）。
- **取消传播**：任务已取消的 Run 不再认领执行，排队/暂停 Run 同步关闭，
  在途取消事件消费留痕；执行期取消由 execution 的租约 fencing 拒绝继续。
- **崩溃恢复接管（R06）**：每轮先 ``execution.recover_expired`` 把租约过期
  Run 归入 waiting_recovery；``resume_recovered`` 先按持久调用回执对账再放回
  队列，对账结论随 checkpoint 带给接管执行——不盲重放。
- **单实例锁**：复用 ``ops.single_active`` 数据库租约语义（SystemState CAS），
  第二实例明确拒绝启动并给出持有者信息。

N02 事务重构（审核 P1）：``process_run`` 不再把认领、费用和外部动作包在一个
未提交事务里。父服务是业务事实写入者，按短事务分相提交：
- 相 A（短事务）：验证 Task/取消传播，原子提交 Run 级预算预留（envelope，
  幂等复用崩溃遗留的开放预留）并 COMMIT——执行意图先于适配器持久可见；
- 相 B：``executor.execute_run`` 持久模式——认领（generation/lease_owner）在
  适配器执行前独立短事务 COMMIT；每个模型调用的预算预留/动作意图（稳定
  call_id）与调用回执由网关经独立短事务提交；适配器全程运行在数据库写事务
  之外；心跳用独立 session_factory 连接续租，管理员取消/撤权可并发提交；
- 相 C（短事务）：Run envelope 结算（N04：envelope 不聚合第二份实际消费，
  真实 usage 以唯一 call_id 记在调用行，互斥合计）。

离线确定性：轮询/认领/结算全部经注入 Session 工厂与注入适配器，测试用内存
或临时 SQLite + FakeProvider 即可覆盖（不触网、不消耗真实费用）。SQLite 单
写者（DE-23/裁决 §4）由短事务序列保证：任何时刻最多一个短写事务在途。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from evoharness.governance import budget
from evoharness.governance.budget import BudgetError
from evoharness.governance.models import BudgetReservation
from evoharness.models import Task
from ops.single_active import (
    claim_db_lease,
    release_db_lease,
    renew_db_lease,
)
from evoharness.work import execution, executor, supplements
from evoharness.work.models import ExecutionRun

DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_SINGLE_ACTIVE_KEY = "work.worker.single_active"
# Run 级预留类别（与 scheduling._ensure_reservation 同类，账本单一口径）
DEFAULT_RUN_CATEGORY = "work.run"


@dataclass
class WorkerConfig:
    """Worker 行为配置：轮询间隔、租约、预算预估与单实例锁。"""

    worker_id: str = ""
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    lease_seconds: int = execution.DEFAULT_LEASE_SECONDS
    heartbeat_interval_seconds: float = executor.DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    estimated_tokens: int = 1000
    max_runs_per_cycle: int = 4
    single_active_key: str = DEFAULT_SINGLE_ACTIVE_KEY
    single_active_lease_seconds: int = 90
    run_category: str = DEFAULT_RUN_CATEGORY
    output_root_note: str = ""   # 观测口径：Run 的 output_root 由 start_run 固化


class WorkWorker:
    """常驻 Worker：认领 → 预算预留 → 受限执行 → 产物/回执 → 结算/失败回写。"""

    def __init__(self, session_factory, adapter, validate_context, *,
                 config: WorkerConfig | None = None, phase_hook=None):
        if session_factory is None:
            raise ValueError("session_factory_required")
        if adapter is None:
            raise ValueError("execution_adapter_required")
        if validate_context is None:
            raise ValueError("authorization_adapter_required")
        self._session_factory = session_factory
        self.adapter = adapter
        self.validate_context = validate_context
        self.config = config or WorkerConfig()
        self.worker_id = self.config.worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._single_holder: dict | None = None
        # N02：持久相观测点（claimed/reserved/... 提交后回调）；生产缺省空操作
        self._phase = phase_hook or (lambda name: None)

    # ── 会话 ────────────────────────────────────────────────────────────────

    def session(self):
        return self._session_factory()

    def _begin(self):
        return self._session_factory.begin()

    # ── 单实例锁（ops/single_active 语义复用） ──────────────────────────────

    def acquire_single_active(self) -> dict:
        """认领单实例租约；已有实例持锁即 SingleActiveRefused（含持有者信息）。"""
        if self._single_holder is not None:
            return dict(self._single_holder)
        holder = {"holder_id": self.worker_id, "pid": os.getpid(),
                  "role": "work.worker"}
        with self._begin() as session:
            claim_db_lease(session, key=self.config.single_active_key, holder=holder,
                           lease_seconds=self.config.single_active_lease_seconds)
        self._single_holder = holder
        return dict(holder)

    def renew_single_active(self) -> bool:
        if self._single_holder is None:
            return False
        with self._begin() as session:
            return renew_db_lease(session, key=self.config.single_active_key,
                                  holder_id=self.worker_id,
                                  lease_seconds=self.config.single_active_lease_seconds)

    def release_single_active(self) -> bool:
        if self._single_holder is None:
            return False
        with self._begin() as session:
            released = release_db_lease(session, key=self.config.single_active_key,
                                        holder_id=self.worker_id)
        self._single_holder = None
        return released

    # ── 队列消费 ────────────────────────────────────────────────────────────

    def claimable_run_ids(self, *, limit: int | None = None) -> list[str]:
        """按 created_at 确定性排序的 queued Run（不认领，只观测）。"""
        limit = limit or self.config.max_runs_per_cycle
        with self._begin() as session:
            rows = list(session.scalars(select(ExecutionRun).where(
                ExecutionRun.state == "queued").order_by(ExecutionRun.created_at)))
            return [row.id for row in rows[:max(1, int(limit))]]

    def _ensure_run_reservation(self, session, task, run):
        """Run 级托管预留（envelope），按 run_id 幂等：崩溃遗留的开放预留直接
        复用（接管续用同一托管额度），不存在才新预留（N02 相 A）。"""
        existing = session.scalar(select(BudgetReservation).where(
            BudgetReservation.run_id == run.id,
            BudgetReservation.kind == "envelope",
            BudgetReservation.status == "reserved"))
        if existing is not None:
            return existing
        return budget.reserve(
            session, org_id=task.org_id or "", space_id=task.space_id or "",
            principal_id=run.actor_id or "", work_item_id=task.id,
            run_id=run.id, category=self.config.run_category,
            tokens=max(1, int(self.config.estimated_tokens)), estimated=True,
            kind="envelope")

    def process_run(self, run_id: str) -> dict:
        """认领并执行一个 Run（N02 分相短事务）：预算预留 → 受限执行 → 回执 → 结算。

        - 相 A（短事务 COMMIT）：取消传播检查 + Run 级预算预留（幂等 envelope）；
        - 相 B：execute_run 持久模式——认领在适配器执行前独立提交；调用预留/
          回执由网关经独立短事务提交（稳定 call_id）；适配器在写事务之外执行；
        - 相 C（短事务 COMMIT）：envelope 结算（残差口径，不双记真实 usage）。

        返回结构化摘要（含预算账目引用）；预算耗尽返回 waiting 语义
        （Run 保持 queued，等待下一轮），不留免费副作用。
        """
        # ── 相 A：执行意图与托管额度先落盘（短事务） ────────────────────────
        with self._begin() as session:
            run = session.get(ExecutionRun, str(run_id))
            if run is None:
                return {"ok": False, "reason": "run_not_found", "run_id": str(run_id)}
            task = session.get(Task, run.task_id)
            if task is None:
                return {"ok": False, "reason": "task_not_found", "run_id": run.id}
            # 取消传播：任务已取消 → 排队/暂停 Run 同步关闭，在途取消事件消费留痕
            if task.status == "cancelled":
                consumed: list[str] = []
                if run.state in {"queued", "paused"}:
                    run.state = "cancelled"
                    for row in supplements.pending(session, task.id):
                        if row.event_kind == "cancellation":
                            supplements.consume(session, row.id)
                            consumed.append(row.event_id)
                    # 取消同时释放崩溃遗留的开放托管额度，escrow 不泄漏
                    for stale in session.scalars(select(BudgetReservation).where(
                            BudgetReservation.run_id == run.id,
                            BudgetReservation.kind == "envelope",
                            BudgetReservation.status == "reserved")):
                        budget.release(session, stale.reservation_id)
                    session.flush()
                return {"ok": True, "reason": "task_cancelled", "run_id": run.id,
                        "run_state": run.state, "cancelled_events": consumed}
            if run.state != "queued":
                return {"ok": False, "reason": f"run_state:{run.state}", "run_id": run.id}
            # 执行前提交预算预留意图（按 org/category；耗尽是等待，Run 保持 queued）
            try:
                reservation = self._ensure_run_reservation(session, task, run)
            except BudgetError as exc:
                return {"ok": False, "reason": "budget_waiting", "run_id": run.id,
                        "error": f"{exc.code}:{exc.scope}",
                        "next": "预算恢复或配置限额后由下一轮 Worker 认领"}
        # 相 A 已 COMMIT：认领与托管额度在适配器执行前持久可见（崩溃可对账）
        self.adapter.bind_run(run_id=run.id, org_id=task.org_id or "",
                              space_id=task.space_id or "",
                              principal_id=run.actor_id or "",
                              work_item_id=task.id,
                              grant_revision=run.task_revision,
                              session=None)
        self._phase("reserved")
        # ── 相 B：认领独立短事务提交后再执行适配器（持久模式） ───────────────
        session = self.session()
        try:
            result = executor.execute_run(
                session, run.id, adapter=self.adapter, worker=self.worker_id,
                validate_context=self.validate_context,
                lease_seconds=self.config.lease_seconds,
                heartbeat_interval=self.config.heartbeat_interval_seconds,
                # N02：session_factory 给定 = 持久模式 + 心跳独立连接续租
                session_factory=self._session_factory,
                phase_hook=self._phase,
                # R07：真实检查回执由适配器在检查执行后签发（惰性读取）
                checker_receipts=lambda: dict(getattr(self.adapter,
                                                      "last_receipts", {}) or {}))
        finally:
            self.adapter.unbind_run()
            session.close()
        if not result.get("claimed"):
            # 未认领：释放本相预留（独立短事务），托管额度不占位
            with self._begin() as release_session:
                budget.release(release_session, reservation.reservation_id)
            return {"ok": False, "reason": "not_claimed", "run_id": run.id,
                    "reservation": "released"}
        # ── 相 C：envelope 结算（独立短事务；真实 usage 在调用行只记一次） ──
        usage = int(getattr(self.adapter, "last_usage_tokens", 0) or 0)
        with self._begin() as settle_session:
            budget.settle(settle_session, reservation.reservation_id,
                          used_tokens=usage, ok=bool(result.get("ok")),
                          outcome=("cancelled" if result.get("aborted")
                                   else "ok" if result.get("ok") else "failed"))
            call_used = budget.call_usage_total(settle_session, run.id)
        self._phase("envelope_settled")
        return {"ok": bool(result.get("ok")), "reason": "executed", "run_id": run.id,
                "error": str(result.get("error") or ""),
                "run_state": ("cancelled" if result.get("aborted")
                              else "waiting_acceptance" if result.get("ok") else "failed"),
                "claimed_done": bool(result.get("claimed_done")),
                "artifacts": list(result.get("artifacts") or []),
                "heartbeat_ticks": int(result.get("heartbeat_ticks") or 0),
                "workspace": result.get("workspace", ""),
                "budget": {"reservation_id": reservation.reservation_id,
                           "category": self.config.run_category,
                           "reserved_tokens": int(reservation.reserved_tokens),
                           # N04：envelope 不再聚合第二份实际消费（真实 usage 在调用行）
                           "used_tokens": 0,
                           "call_usage_tokens": call_used}}

    def run_once(self, *, single_active: bool = True) -> dict:
        """跑一轮：恢复过期租约（R06）→ 逐个认领执行 queued Run（有界）。"""
        if single_active:
            self.acquire_single_active()
        recovered = self.recover_expired()
        processed = []
        for run_id in self.claimable_run_ids():
            processed.append(self.process_run(run_id))
        return {"worker_id": self.worker_id, "recovered": recovered,
                "processed": processed}

    def run_forever(self, *, stop_event: threading.Event | None = None) -> str:
        """常驻循环：单实例锁 + 后台续租 + 每轮恢复/消费。

        DE-08：单实例租约由独立心跳线程按 ``single_active_lease_seconds/3``
        续期（长 Run 执行期间也持续有效）；续期失败（另一实例可能已接管）
        立即停止派工，返回 ``single_active_lost``，不让两个写者并行。
        """
        stop = stop_event or threading.Event()
        done = threading.Event()
        lost = threading.Event()
        interval = max(0.05, float(self.config.single_active_lease_seconds) / 3.0)
        self.acquire_single_active()

        def _renew_loop() -> None:
            while not done.wait(interval):
                try:
                    renewed = self.renew_single_active()
                except Exception:  # noqa: BLE001 — 续租异常与失锁同义：停止派工
                    renewed = False
                if not renewed:
                    lost.set()
                    return

        renewer = threading.Thread(target=_renew_loop,
                                   name=f"single-active-{self.worker_id[:12]}",
                                   daemon=True)
        renewer.start()
        reason = "stopped_gracefully"
        try:
            while not stop.is_set():
                if lost.is_set():
                    reason = "single_active_lost"
                    break
                self.run_once(single_active=False)
                if lost.is_set():
                    reason = "single_active_lost"
                    break
                stop.wait(max(0.01, float(self.config.poll_interval_seconds)))
        finally:
            done.set()
            renewer.join(timeout=5.0)
            self.release_single_active()
        return reason

    # ── 崩溃恢复（R06 复用） ────────────────────────────────────────────────

    def recover_expired(self) -> list[str]:
        """租约过期的 Run 归入 waiting_recovery（对账前不再盲跑）。"""
        with self._begin() as session:
            return list(execution.recover_expired(session))

    def _default_reconcile(self, session, run: ExecutionRun) -> bool:
        """N02 恢复对账：按持久调用回执对账，目录存在只是诊断事实。

        - 已回执的调用（settled）：已完成步骤，usage 已记账，接管执行跳过重付；
        - 执行中未回执的意图（intent_status=executing）：结果未知——保守归集
          （usage_status=unknown），不回滚成未发生、不盲重试；
        - 未执行未回执的意图（committed）：外部动作没开始，释放托管份额；
        - 对账结论写入 run checkpoint 的 reconciliation 段：接管执行的上下文
          携带该段（executor 经 frozen.reconciliation 传入适配器），续跑不是
          全新重放；工作区是否还在只作为诊断事实记录，不作为对账依据。
        """
        rows = list(session.scalars(select(BudgetReservation).where(
            BudgetReservation.run_id == run.id)))
        completed: list[dict] = []
        unknown: list[str] = []
        released: list[str] = []
        for row in rows:
            kind = row.kind or "call"
            if kind == "envelope":
                continue
            if row.status == "settled":
                completed.append({"call_id": row.call_id or row.reservation_id,
                                  "used_tokens": int(row.used_tokens or 0),
                                  "usage_status": row.usage_status or "recorded",
                                  "outcome": row.outcome or "ok"})
                continue
            if row.status != "reserved":
                continue   # released：已确认为未执行，无待对账项
            if (row.intent_status or "") == "executing":
                budget.settle(session, row.reservation_id,
                              used_tokens=int(row.reserved_tokens), ok=False,
                              usage_status="unknown", intent_status="unknown",
                              outcome="unknown")
                unknown.append(row.call_id or row.reservation_id)
            else:
                budget.release(session, row.reservation_id)
                released.append(row.call_id or row.reservation_id)
        try:
            checkpoint = json.loads(run.checkpoint_json or "{}")
        except ValueError:
            checkpoint = {}
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        frozen = checkpoint.get("frozen") if isinstance(checkpoint.get("frozen"), dict) else {}
        workspace = frozen.get("workspace") or run.output_root
        try:
            workspace_seen = Path(workspace).exists()
        except OSError:
            workspace_seen = False
        checkpoint["reconciliation"] = {
            "generation": int(run.generation or 0),
            "completed_calls": completed,
            "unknown_intents": unknown,
            "released_intents": released,
            "workspace_seen": workspace_seen,
        }
        run.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False)
        session.flush()
        return True

    def resume_recovered(self, run_id: str, *, reconcile=None, note: str = "") -> dict:
        """waiting_recovery 的显式恢复：先对账 checkpoint 与副作用，再放回队列。"""
        with self._begin() as session:
            run = execution.resume(session, str(run_id),
                                   reconcile=reconcile or self._default_reconcile,
                                   disposition="queued", note=note)
            return {"ok": True, "run_id": run.id, "state": run.state}
