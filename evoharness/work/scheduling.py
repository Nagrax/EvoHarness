"""岗位队列、资源租约与确定性调度（DE-40）。

指南 §18.2 的产品核心，全部落在原 Task/Run 服务上：

- 调度器读取同一 Task/Run 状态，结合岗位能力证据、可用授权、预算和资源选
  可执行工作；确定性排序（优先级 = due_at 升序，其次 task.id），首期单组织
  有界并发。不另建调度状态、不另起主管 Agent：调度只"决定"，Run 的租约/
  代际/恢复语义仍归 execution（DE-11），等待不计失败次数。
- 岗位是责任索引：按任务所需能力（acceptance 条目里的 capability）与可用
  授权找合格任职；已有临时组队（DE-38 form_task_crew）时执行人必须取自组队
  executor 名单（保持执行/审阅分离）。找不到合格岗位 → 转澄清等待负责人，
  绝不创建新 Agent 或复制任务。
- 共享代码分支/文档/知识页/发送意图受资源租约与 CAS 控制：同一资源一行
  租约，acquire/version-write 都是条件 UPDATE（rowcount 即裁决）；双岗位
  争同一资源时后者等待，不并发覆盖。
- 优先级中断只在可恢复边界生效：pause_run_for_priority 由持有租约的 worker
  在检查点边界显式调用，保存旧 Run 检查点并结算真实费用一次；恢复从检查点
  续跑（resume_paused_run），不重复执行、不重复扣费（已结算用量只记一次）。
- 预算接 DE-15：调度为 Run 做预估预留（按 run_id 幂等，续跑用新预留记新工作，
  旧调用已结算用量不重复入账）；budget_exhausted 是等待，不是失败。
- 协调提案（DE-21 wakeups.coordinate 复核后 request_run）产生的 Run 与手动
  派工进入同一执行队列：调度器发现任务已有排队 Run 时直接复用，不再新建。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import Integer, String, UniqueConstraint, func, or_, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from evoharness.clock import utcnow
from evoharness.models import Base, Task
from evoharness.governance import budget
from evoharness.governance.models import BudgetReservation
from evoharness.organization.models import Position, TaskCrewMember, UnitSpaceBinding
from evoharness.work import execution, service
from evoharness.work.models import ExecutionRun

# 可租约的资源种类：共享代码分支/文档/知识页/发送意图/其他外部资源
RESOURCE_KINDS = frozenset({"code_branch", "document", "knowledge_page",
                            "send_intent", "external"})

# 正在消耗执行容量的 Run 状态（有界并发按此计数）
ACTIVE_EXECUTE_STATES = ("claimed", "running")

# 等待原因标记：澄清幂等去重，避免调度轮询反复写同一事件
NO_POSITION_MARKER = "no_qualified_position"

DEFAULT_LEASE_TTL_SECONDS = 600
DEFAULT_ESTIMATED_TOKENS = 1000
DEFAULT_MAX_CONCURRENCY = 2


class SchedulingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# ── 资源租约：同一资源一行，CAS 条件更新裁决 ─────────────────────────────────


class ResourceLease(Base):
    """资源租约（DE-40）：holder/epoch/version 全部经条件 UPDATE 变更。

    同一 (org, kind, key) 只有一行：held 表示被 holder_id 独占；
    version 是资源内容 CAS 版本（外部写入前必须以 expected_version 换新，
    落后版本/非持有者的写入一律拒绝 → 双岗位不并发覆盖）。
    """
    __tablename__ = "work_resource_leases"
    __table_args__ = (UniqueConstraint("org_id", "resource_kind", "resource_key",
                                       name="uq_resource_lease"),)
    lease_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    resource_kind: Mapped[str] = mapped_column(String(32))
    resource_key: Mapped[str] = mapped_column(String(255))
    holder_id: Mapped[str] = mapped_column(String(64), default="")
    holder_epoch: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    last_writer: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="free")
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    acquired_at: Mapped[datetime | None] = mapped_column(nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(nullable=True)

    def state(self) -> dict:
        return {"lease_id": self.lease_id, "resource_kind": self.resource_kind,
                "resource_key": self.resource_key, "holder_id": self.holder_id,
                "holder_epoch": self.holder_epoch, "version": self.version,
                "last_writer": self.last_writer, "status": self.status,
                "lease_until": self.lease_until.isoformat() if self.lease_until else ""}


def _lease(session: Session, *, org_id: str, resource_kind: str, resource_key: str) -> ResourceLease | None:
    return session.scalar(select(ResourceLease).where(
        ResourceLease.org_id == org_id,
        ResourceLease.resource_kind == resource_kind,
        ResourceLease.resource_key == resource_key))


def acquire_lease(session: Session, *, org_id: str, resource_kind: str,
                  resource_key: str, holder_id: str,
                  ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
                  now: datetime | None = None) -> ResourceLease | None:
    """获取资源租约：独占成功返回租约行；被其他 holder 持有且未过期返回 None。

    CAS 语义：holder/epoch/lease_until 只经条件 UPDATE（rowcount==1）变更；
    过期租约由同一条件语句原子接管，不需要独立清理步骤。
    """
    if resource_kind not in RESOURCE_KINDS:
        raise SchedulingError("invalid_resource_kind")
    if not holder_id:
        raise SchedulingError("lease_holder_required")
    now = now or utcnow()
    deadline = now + timedelta(seconds=ttl_seconds)
    row = _lease(session, org_id=org_id, resource_kind=resource_kind,
                 resource_key=resource_key)
    if row is None:
        row = ResourceLease(lease_id=uuid4().hex, org_id=org_id,
                            resource_kind=resource_kind, resource_key=resource_key)
        session.add(row)
        session.flush()
    result = session.execute(update(ResourceLease).where(
        ResourceLease.lease_id == row.lease_id,
        or_(ResourceLease.status == "free",
            ResourceLease.lease_until.is_(None),
            ResourceLease.lease_until <= now)).values(
        status="held", holder_id=holder_id, holder_epoch=row.holder_epoch + 1,
        lease_until=deadline, acquired_at=now, released_at=None))
    session.flush()
    if result.rowcount != 1:
        return None  # 被他人持有且未过期：等待，不抢占
    session.refresh(row)
    return row


def renew_lease(session: Session, lease_id: str, *, holder_id: str,
                ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
                now: datetime | None = None) -> ResourceLease:
    now = now or utcnow()
    row = session.get(ResourceLease, lease_id)
    if row is None or row.status != "held":
        raise SchedulingError("lease_not_held")
    result = session.execute(update(ResourceLease).where(
        ResourceLease.lease_id == lease_id, ResourceLease.status == "held",
        ResourceLease.holder_id == holder_id).values(
        lease_until=now + timedelta(seconds=ttl_seconds)))
    session.flush()
    if result.rowcount != 1:
        raise SchedulingError("lease_holder_mismatch")
    session.refresh(row)
    return row


def release_lease(session: Session, lease_id: str, *, holder_id: str,
                  now: datetime | None = None) -> ResourceLease:
    now = now or utcnow()
    row = session.get(ResourceLease, lease_id)
    if row is None:
        raise SchedulingError("lease_not_found")
    result = session.execute(update(ResourceLease).where(
        ResourceLease.lease_id == lease_id, ResourceLease.status == "held",
        ResourceLease.holder_id == holder_id).values(
        status="free", holder_id="", lease_until=None, released_at=now))
    session.flush()
    if result.rowcount != 1:
        raise SchedulingError("lease_holder_mismatch")
    session.refresh(row)
    return row


def expire_leases(session: Session, *, now: datetime | None = None) -> list[str]:
    """把过期租约原子释放：崩溃/超时的持有者不再阻塞队列（可对账不死锁）。"""
    now = now or utcnow()
    rows = list(session.scalars(select(ResourceLease).where(
        ResourceLease.status == "held", ResourceLease.lease_until.is_not(None),
        ResourceLease.lease_until <= now)))
    expired: list[str] = []
    for row in rows:
        result = session.execute(update(ResourceLease).where(
            ResourceLease.lease_id == row.lease_id, ResourceLease.status == "held",
            ResourceLease.lease_until <= now).values(
            status="free", holder_id="", lease_until=None, released_at=now))
        if result.rowcount == 1:
            expired.append(row.lease_id)
    session.flush()
    return expired


def lease_write(session: Session, lease: ResourceLease, *, holder_id: str,
                expected_version: int, writer: str = "") -> int:
    """资源内容写入的 CAS 闸门：只有当前持有者且版本吻合才能推进 version。

    落后版本（读-改-写期间被别人推进过）或非持有者一律拒绝，
    写方必须重新读取资源后基于新版本重试 → 双岗位不会并发覆盖。
    """
    result = session.execute(update(ResourceLease).where(
        ResourceLease.lease_id == lease.lease_id, ResourceLease.status == "held",
        ResourceLease.holder_id == holder_id,
        ResourceLease.version == expected_version).values(
        version=expected_version + 1, last_writer=writer[:64]))
    session.flush()
    if result.rowcount != 1:
        raise SchedulingError("lease_version_conflict")
    session.refresh(lease)
    return lease.version


# ── 岗位责任索引：能力证据 × 可用授权 → 合格任职 ─────────────────────────────


def _loads(raw: str, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except ValueError:
        return fallback


def required_capabilities(task: Task) -> list[str]:
    """任务声明所需能力：acceptance 条目为 dict 时取 capability 字段。"""
    caps: list[str] = []
    for item in _loads(task.acceptance_json, []):
        if isinstance(item, dict):
            cap = str(item.get("capability") or "").strip()
            if cap and cap not in caps:
                caps.append(cap)
    return caps


def required_resources(task: Task) -> list[dict]:
    """任务声明要独占修改的资源：acceptance 条目 dict 的 resource={kind,key}。"""
    resources: list[dict] = []
    for item in _loads(task.acceptance_json, []):
        if not isinstance(item, dict):
            continue
        spec = item.get("resource")
        if isinstance(spec, dict):
            kind = str(spec.get("kind") or "").strip()
            key = str(spec.get("key") or "").strip()
            if kind and key and {"kind": kind, "key": key} not in resources:
                resources.append({"kind": kind, "key": key})
    return resources


def _position_covers(session: Session, position: Position, *, org_id: str,
                     space_id: str, capabilities: list[str]) -> bool:
    """岗位合格判定：能力证据覆盖所需能力，且岗位单元绑定任务空间。

    DE-42 能力证据门（EVOHARNESS_CAPABILITY_EVIDENCE_GATE，默认 off 保持既有
    行为；开启后）：合格判定引用 CapabilityEvidence——搜索池/未认证候选与
    无证据能力一律不是合格接单资格（证据缺失/待验证/范围不符 → 不合格，
    原因见 organization.capabilities.capability_status，任务走"等待负责人"
    路径，不自动派工）。
    """
    if position.org_id != org_id or position.status != "active":
        return False
    binding = session.scalar(select(UnitSpaceBinding.id).where(
        UnitSpaceBinding.unit_id == position.unit_id,
        UnitSpaceBinding.space_id == space_id))
    if binding is None:
        return False
    if not capabilities:
        return True
    held = set(_loads(position.capability_refs_json, []))
    if not set(capabilities) <= held:
        return False
    from evoharness.organization import capabilities as org_capabilities  # DE-42 接缝（惰性导入防环）

    if not org_capabilities.evidence_gate_enabled():
        return True
    return all(org_capabilities.capability_status(
        session, org_id=org_id, position_id=position.position_id,
        capability=cap, space_id=space_id)["qualified"] for cap in capabilities)


def qualified_assignments(session: Session, task: Task) -> list[dict]:
    """按岗位责任索引找合格任职（确定性排序 assignment_id）：

    岗位 active、能力覆盖、单元绑定任务空间；任职主体在任（成员 active 且
    指派在有效期）。返回 [{assignment_id, membership_id, position_id}]。
    """
    from evoharness.organization.models import Assignment, Membership

    now = utcnow()

    # 与 organization.service._live 同口径：存储时钟是 naive UTC
    def _live(row) -> bool:
        return (row.status == "active"
                and (row.valid_from is None or row.valid_from <= now)
                and (row.valid_until is None or now < row.valid_until))

    capabilities = required_capabilities(task)
    rows: list[dict] = []
    positions = [p for p in session.scalars(select(Position).where(
        Position.org_id == task.org_id))
        if _position_covers(session, p, org_id=task.org_id,
                            space_id=task.space_id, capabilities=capabilities)
        # 岗位是责任索引：任务已有责任岗位时，执行人必须来自该岗位
        # （更换责任岗位走交接，不走调度器改写）
        and (not task.assigned_position or p.position_id == task.assigned_position)]
    for position in positions:
        for assignment in session.scalars(select(Assignment).where(
                Assignment.org_id == task.org_id,
                Assignment.position_id == position.position_id)):
            member = session.get(Membership, assignment.membership_id)
            if member is None or not _live(member) or not _live(assignment):
                continue
            if task.space_id and task.space_id not in set(_loads(assignment.space_ids_json, [])):
                continue
            if "work.execute" not in set(_loads(assignment.actions_json, [])):
                continue
            rows.append({"assignment_id": assignment.assignment_id,
                         "membership_id": assignment.membership_id,
                         "position_id": position.position_id})
    rows.sort(key=lambda item: item["assignment_id"])
    return rows


def _crew(session: Session, task_id: int) -> dict:
    """读取 DE-38 临时组队（如有）：执行人取组队 executor，审阅者随 Run 传递。"""
    members = [row for row in session.scalars(select(TaskCrewMember).where(
        TaskCrewMember.task_id == task_id, TaskCrewMember.status == "active"))]
    return {
        "executors": sorted(m.assignment_id for m in members if m.role == "executor"),
        "reviewers": sorted(m.assignment_id for m in members if m.role == "reviewer"),
        "accountable": sorted(m.assignment_id for m in members if m.role == "accountable"),
    }


# ── 优先级中断：只在可恢复边界生效 ───────────────────────────────────────────


def pause_run_for_priority(session: Session, run_id: str, *, worker: str,
                           generation: int, validate_context,
                           checkpoint: dict, reason: str = "",
                           reservation_id: str = "", used_tokens: int | None = None) -> ExecutionRun:
    """高优先级接手：低优先级 Run 在检查点边界显式暂停。

    - 只接受持有租约的 worker 在 claimed/running 状态调用（execution.transition
      的租约/代际 CAS 校验不通过即拒绝）——不是任意时刻可撕掉在途工作；
    - checkpoint 原样落盘到旧 Run；任务状态 running→paused（同一状态机）；
    - 结算真实费用一次：预算预留按已发生 usage settle；重复结算被 DE-15
      拒绝（reservation_already_closed）→ 已发生用量不会重复入账。
    """
    run = execution.transition(session, run_id, worker=worker, generation=generation,
                               state="paused", validate_context=validate_context,
                               checkpoint=checkpoint)
    if reservation_id and used_tokens is not None:
        budget.settle(session, reservation_id, used_tokens=int(used_tokens), ok=True)
    return run


def resume_paused_run(session: Session, run_id: str, *, actor: str = "scheduler") -> ExecutionRun:
    """容量恢复后把被让路的 Run 放回队列：CAS paused→queued，从检查点续跑。

    - 只有 paused Run 可恢复；恢复不是新失败：attempts 不增、checkpoint 保留；
    - 任务经唯一状态机 paused→queued 放行（ready_gate 仍把关合同齐备）；
    - 认领与执行仍由 execution.claim 的租约/代际语义裁决，调度器不绕过。
    """
    run = session.get(ExecutionRun, run_id)
    if run is None:
        raise SchedulingError("run_not_found")
    if run.state != "paused":
        raise SchedulingError("run_not_paused")
    result = session.execute(update(ExecutionRun).where(
        ExecutionRun.id == run_id, ExecutionRun.state == "paused").values(
        state="queued", generation=run.generation + 1, lease_owner="",
        lease_until=None))
    session.flush()
    if result.rowcount != 1:
        raise SchedulingError("stale_paused_run")
    session.refresh(run)
    task = session.get(Task, run.task_id)
    service.transition_to(session, task.id, to_state="queued", actor=actor,
                          reason="resumed_from_preemption")
    return run


# ── 预算：按 run_id 幂等的预估预留 ───────────────────────────────────────────


def _ensure_reservation(session: Session, task: Task, run: ExecutionRun | None, *,
                        org_id: str, space_id: str, principal_id: str,
                        estimated_tokens: int) -> str:
    """同一 Run 只保留一笔开放预留（重复调度不重复预留）。

    - 已有 Run：按 run_id 幂等预留并直接绑定；
    - 尚无 Run：先以 run_id="" 挂任务预留——预算耗尽时不建 Run（无预算不派工），
      派工成功后再把预留绑定到 Run；续跑的工作在旧调用结算后按新预留记录，
      旧用量不重复入账。
    """
    if run is not None:
        existing = session.scalar(select(BudgetReservation).where(
            BudgetReservation.run_id == run.id, BudgetReservation.kind == "envelope",
            BudgetReservation.status == "reserved"))
        if existing is not None:
            return existing.reservation_id
        row = budget.reserve(session, org_id=org_id, space_id=space_id,
                             principal_id=principal_id, work_item_id=task.id,
                             run_id=run.id, category="work.run",
                             tokens=max(1, int(estimated_tokens)), estimated=True,
                             kind="envelope")
        return row.reservation_id
    existing = session.scalar(select(BudgetReservation).where(
        BudgetReservation.work_item_id == task.id, BudgetReservation.run_id == "",
        BudgetReservation.kind == "envelope", BudgetReservation.status == "reserved"))
    if existing is not None:
        return existing.reservation_id
    row = budget.reserve(session, org_id=org_id, space_id=space_id,
                         principal_id=principal_id, work_item_id=task.id,
                         run_id="", category="work.run",
                         tokens=max(1, int(estimated_tokens)), estimated=True,
                         kind="envelope")
    return row.reservation_id


def _attach_reservation(session: Session, reservation_id: str, run_id: str) -> None:
    """派工成功后把任务级预留绑定到 Run（仅开放预留可绑定）。"""
    session.execute(update(BudgetReservation).where(
        BudgetReservation.reservation_id == reservation_id,
        BudgetReservation.status == "reserved").values(run_id=run_id))
    session.flush()


def _open_run(session: Session, task: Task) -> ExecutionRun | None:
    return session.scalar(select(ExecutionRun).where(
        ExecutionRun.task_id == task.id,
        ExecutionRun.state.in_(("queued", *ACTIVE_EXECUTE_STATES))).limit(1))


# ── 确定性调度：单组织有界并发，等待不计失败 ─────────────────────────────────


def schedule_pending(session: Session, *, org_id: str, context_factory,
                     now: datetime | None = None, max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
                     worker: str = "scheduler", output_root: str = "out",
                     estimated_tokens: int = DEFAULT_ESTIMATED_TOKENS,
                     lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
                     max_candidates: int = 50) -> dict:
    """对单组织做一轮确定性调度，返回可审计报告（不新增调度状态）。

    - context_factory(session, task, assignment) -> (context, validate_context)：
      授权快照与适配器由装配层注入（生产装配组织授权，离线测试注入 fake）。
    - 优先级：due_at 升序（NULL 最后），其次 task.id——确定性、无市场竞拍。
    - 每个失败的准入闸门都是"等待"：任务保持 queued、Run 不建、attempts 不增，
      报告里给出原因（no_capacity/budget_exhausted/resource_leased/
      no_qualified_position/authorization…）。
    - 找不到合格岗位 → 转澄清等待负责人（同一澄清入口，幂等去重），
      绝不创建 Agent、不复制任务。
    """
    now = now or utcnow()
    expire_leases(session, now=now)
    report: dict = {"org_id": org_id, "capacity": max_concurrency,
                    "resumed": [], "scheduled": [], "waiting": [],
                    "active_runs": 0}
    paused_runs = list(session.scalars(select(ExecutionRun).where(
        ExecutionRun.state == "paused").order_by(ExecutionRun.created_at)))
    for run in paused_runs:
        if len(report["resumed"]) + _active_count(session) >= max_concurrency:
            break
        task = session.get(Task, run.task_id)
        if task is None or task.org_id != org_id:
            continue
        resume_paused_run(session, run.id)
        report["resumed"].append({"run_id": run.id, "task_key": task.task_key})

    candidates = list(session.scalars(select(Task).where(
        Task.org_id == org_id, Task.workflow_kind == "delegated_delivery",
        Task.status == "queued").order_by(
        Task.due_at.is_(None), Task.due_at, Task.id).limit(max_candidates)))
    for task in candidates:
        if _active_count(session) >= max_concurrency:
            report["waiting"].append({"task_key": task.task_key,
                                      "reason": "no_capacity"})
            continue
        crew = _crew(session, task.id)
        assignments = qualified_assignments(session, task)
        if crew["executors"]:
            allowed = set(crew["executors"])
            assignments = [a for a in assignments if a["assignment_id"] in allowed]
        if not assignments:
            _wait_no_position(session, task)
            report["waiting"].append({"task_key": task.task_key,
                                      "reason": "no_qualified_position"})
            continue
        executor = assignments[0]
        acquired: list[ResourceLease] = []
        blocked: dict | None = None
        for spec in required_resources(task):
            lease = acquire_lease(session, org_id=org_id,
                                  resource_kind=spec["kind"],
                                  resource_key=spec["key"],
                                  holder_id=str(task.id),
                                  ttl_seconds=lease_ttl_seconds, now=now)
            if lease is None:
                blocked = spec
                break
            acquired.append(lease)
        if blocked is not None:
            # 资源被他人持有：本轮等待；本轮已拿到的其他租约全部释放，不留空占
            for lease in acquired:
                release_lease(session, lease.lease_id, holder_id=str(task.id))
            report["waiting"].append({"task_key": task.task_key,
                                      "reason": "resource_leased",
                                      "resource": blocked})
            continue
        try:
            context, validate_context = context_factory(session, task, executor)
            run = _open_run(session, task)
            reservation_id = _ensure_reservation(
                session, task, run, org_id=org_id, space_id=task.space_id,
                principal_id=executor["membership_id"],
                estimated_tokens=estimated_tokens)
            if run is None:
                try:
                    run = execution.request_run(
                        session, task_id=task.id, context=context,
                        actor_id=executor["assignment_id"], output_root=output_root,
                        idempotency_key=f"sched|{task.task_key}|{task.revision}",
                        validate_context=validate_context)
                except Exception:
                    # 派工失败不泄漏预留（等待/失败同样归集，没有免费副作用）
                    budget.release(session, reservation_id)
                    raise
                _attach_reservation(session, reservation_id, run.id)
            report["scheduled"].append({
                "task_key": task.task_key, "run_id": run.id,
                "reservation_id": reservation_id,
                "executor_assignment": executor["assignment_id"],
                "position": executor["position_id"],
                "reviewers": crew["reviewers"]})
        except budget.BudgetError as exc:
            for lease in acquired:
                release_lease(session, lease.lease_id, holder_id=str(task.id))
            report["waiting"].append({"task_key": task.task_key,
                                      "reason": "budget_exhausted",
                                      "detail": str(exc)})
        except ValueError as exc:
            for lease in acquired:
                release_lease(session, lease.lease_id, holder_id=str(task.id))
            report["waiting"].append({"task_key": task.task_key,
                                      "reason": "not_schedulable",
                                      "detail": str(exc)})
    report["active_runs"] = _active_count(session)
    session.flush()
    return report


def _active_count(session: Session) -> int:
    return int(session.scalar(
        select(func.count()).select_from(ExecutionRun).where(
            ExecutionRun.state.in_(ACTIVE_EXECUTE_STATES))) or 0)


def _wait_no_position(session: Session, task: Task) -> None:
    """无合格岗位：转澄清等待负责人，带具体原因；已是澄清态则不再重复写。"""
    missing = required_capabilities(task)
    question = (f"{NO_POSITION_MARKER}: 缺少具备能力 "
                f"{','.join(missing) if missing else '（未声明能力，且责任岗位无在任主体）'} "
                f"的在任岗位；等待负责人指派或澄清。")
    if task.status == "queued" and NO_POSITION_MARKER not in (task.waiting_reason or ""):
        service.request_clarification(session, task.id, actor="scheduler",
                                      question=question, blocking=True)
