"""Persistent run leases with revision and authorization fencing."""
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from sqlalchemy import and_, exists, or_, select, update
from evoharness.models import Task
from evoharness.ids import sha256_json
from evoharness.clock import utcnow
from evoharness.work import service
from evoharness.work.models import ExecutionRun

# 执行租约默认时长（秒）与续租默认时长：心跳按同一口径续期。
DEFAULT_LEASE_SECONDS = 120

# 等待类 Run 状态：进入即释放执行租约（LeaseRelease）——执行器不再占住
# 单活执行位；恢复/重试必须经 execution.reacquire 重新授权认领。
RELEASE_LEASE_STATES = frozenset({"waiting_budget", "waiting_resource", "waiting_external",
                                  "waiting_authorization", "waiting_acceptance"})


def _validate(session, run, validate_context, *, action):
    if validate_context is None:
        raise ValueError("authorization_adapter_required")
    context = json.loads(run.context_json)
    if sha256_json(context) != run.context_hash:
        raise ValueError("context_changed")
    if validate_context(session, context, action) is not True:
        raise ValueError("authorization_invalid")
    task = session.get(Task, run.task_id)
    if task is None or task.revision != run.task_revision or task.status in {"done", "cancelled", "paused"}:
        raise ValueError("work_revision_changed")
    return task


def request_run(session, *, task_id, context, actor_id, output_root, idempotency_key, validate_context):
    task = session.get(Task, task_id)
    if task is None or task.workflow_kind != "delegated_delivery":
        raise ValueError("delegated_task_required")
    if not task.goal.strip() or not task.owner or not json.loads(task.acceptance_json):
        raise ValueError("incomplete_work_contract")
    previous = session.scalar(select(ExecutionRun).where(
        ExecutionRun.tenant == task.tenant, ExecutionRun.idempotency_key == idempotency_key))
    if previous is not None:
        if previous.task_id != task_id or previous.task_revision != task.revision or previous.context_hash != sha256_json(context):
            raise ValueError("run_intent_conflict")
        # R02：幂等恢复（暂停/排队 Run 重启）同样重验授权与操作者绑定，
        # "Run 已存在"不得成为绕过授权重验的通道。
        _validate(session, previous, validate_context, action="work.execute")
        return previous
    run = ExecutionRun(id=uuid4().hex, task_id=task_id, tenant=task.tenant,
        task_revision=task.revision, idempotency_key=idempotency_key, actor_id=actor_id,
        context_json=json.dumps(context, sort_keys=True), context_hash=sha256_json(context),
        output_root=str(Path(output_root).resolve()), state="queued")
    _validate(session, run, validate_context, action="work.execute")
    session.add(run)
    session.flush()
    return run


def claim(session, run_id, *, worker, validate_context, now=None,
          lease_seconds=DEFAULT_LEASE_SECONDS):
    now = now or utcnow()
    run = session.get(ExecutionRun, run_id)
    if run is None or run.state != "queued":
        return None
    _validate(session, run, validate_context, action="work.execute")
    # Single active worker initially. The exclusion is part of the write CAS.
    busy = exists(select(ExecutionRun.id).where(
        ExecutionRun.id != run_id, ExecutionRun.state.in_(["claimed", "running"]),
        ExecutionRun.lease_until > now))
    result = session.execute(update(ExecutionRun).where(
        ExecutionRun.id == run_id, ExecutionRun.state == "queued",
        ExecutionRun.generation == run.generation, ~busy).values(
        state="claimed", generation=run.generation + 1, lease_owner=worker,
        lease_until=now + timedelta(seconds=lease_seconds)))
    session.flush()
    if result.rowcount != 1:
        return None
    session.refresh(run)
    return run


def transition(session, run_id, *, worker, generation, state, validate_context, checkpoint=None, now=None):
    now = now or utcnow()
    run = session.get(ExecutionRun, run_id)
    if run is None:
        raise ValueError("run_not_found")
    task = _validate(session, run, validate_context, action="work.execute")
    # 等待是延期（Later）不是失败：waiting_* 出口回到 running（预算/资源恢复续跑）。
    waiting = {"waiting_budget", "waiting_resource", "waiting_external", "waiting_authorization"}
    allowed = {"claimed": {"running", "paused"} | waiting,
               "running": {"waiting_acceptance", "failed", "paused"} | waiting}
    for wstate in waiting:
        allowed[wstate] = {"running", "paused"}
    if state not in allowed.get(run.state, set()):
        raise ValueError("invalid_run_transition")
    values = {"state": state, "attempts": run.attempts + (1 if state == "running" else 0),
              "checkpoint_json": json.dumps(checkpoint if checkpoint is not None
                                            else json.loads(run.checkpoint_json))}
    if state in RELEASE_LEASE_STATES:
        # R05（LeaseRelease）：进入等待类状态即释放执行租约——执行期已结束，
        # 固定租约不再占住单活执行位；恢复/重试经 reacquire 重新授权认领。
        # 释放与状态转换同一条条件 UPDATE，崩溃不会留下"等待却占租约"的中间态。
        values["lease_owner"] = ""
        values["lease_until"] = None
    result = session.execute(update(ExecutionRun).where(
        ExecutionRun.id == run_id, ExecutionRun.lease_owner == worker,
        ExecutionRun.generation == generation, ExecutionRun.lease_until > now,
        ExecutionRun.state == run.state).values(**values))
    if result.rowcount != 1:
        raise ValueError("stale_run_lease")
    task.status = state
    session.flush()
    session.refresh(run)
    return run


def renew_lease(session, run_id, *, worker, generation, now=None,
                lease_seconds=DEFAULT_LEASE_SECONDS):
    """R05：Run 执行租约续期（独立条件 UPDATE，stale 即拒）。

    - 条件绑定 worker/generation/state：只有仍持有执行租约（claimed/running）
      的同一 worker 按同一 generation 才能续期；被接管/被恢复即拒。
    - "已过期租约不续"指所有权已被收走的租约：recover_expired/takeover 之后
      generation 递增、owner 清空、状态 waiting_recovery，条件更新必然不命中，
      必须走对账恢复，不得复活已回收的租约。
    - 时钟越过旧到期点但所有权未转移（无他人能认领非 queued 状态的 Run）时，
      同 worker/generation 的续期仍然有效——这是心跳在长执行中追平时钟的依据。
    - 新到期点 = now + lease_seconds，由调用方的时钟显式给定（控制时钟可测）。
    失败抛 stale_run_lease；心跳线程捕获记录，不抛入主流程。
    """
    if int(lease_seconds) <= 0:
        raise ValueError("invalid_lease_seconds")
    now = now or utcnow()
    result = session.execute(update(ExecutionRun).where(
        ExecutionRun.id == run_id, ExecutionRun.lease_owner == worker,
        ExecutionRun.generation == generation,
        ExecutionRun.state.in_(["claimed", "running"])).values(
        lease_until=now + timedelta(seconds=int(lease_seconds))))
    session.flush()
    if result.rowcount != 1:
        raise ValueError("stale_run_lease")
    run = session.get(ExecutionRun, run_id)
    session.refresh(run)
    return run


def reacquire(session, run_id, *, worker, validate_context, now=None,
              lease_seconds=DEFAULT_LEASE_SECONDS):
    """R05：等待类 Run 恢复执行前的重新授权认领（租约已在进入等待时释放）。

    - 重新验证授权与工作合同（_validate），"等待"不得成为绕过授权的通道；
    - CAS：仍处于等待类状态且租约已释放/已过期才可重取，他人持有有效租约即拒；
    - generation 递增：恢复后的执行是新的一次尝试（产物按新 generation 登记），
      状态回到 claimed，随后经 execution.transition 正常推进。
    """
    now = now or utcnow()
    run = session.get(ExecutionRun, run_id)
    if run is None:
        raise ValueError("run_not_found")
    if run.state not in RELEASE_LEASE_STATES:
        raise ValueError("run_not_reclaimable")
    _validate(session, run, validate_context, action="work.execute")
    result = session.execute(update(ExecutionRun).where(
        ExecutionRun.id == run_id, ExecutionRun.state.in_(sorted(RELEASE_LEASE_STATES)),
        ExecutionRun.generation == run.generation,
        or_(ExecutionRun.lease_owner == "", ExecutionRun.lease_until.is_(None),
            ExecutionRun.lease_until <= now)).values(
        state="claimed", generation=run.generation + 1, lease_owner=worker,
        lease_until=now + timedelta(seconds=int(lease_seconds))))
    session.flush()
    if result.rowcount != 1:
        raise ValueError("stale_run_lease")
    session.refresh(run)
    return run


def abort(session, run_id, *, worker, generation, state="failed", error="", now=None):
    """工作合同已消失（任务取消/暂停/完成或授权失效）时的受控终止（N02）。

    状态机 transition 的失败回写需要授权重验与任务状态同步；任务已被取消/
    暂停时这两步必然拒绝（work_revision_changed / authorization_invalid），
    Run 若不做受控终止就会永远悬挂在 running。abort 用与 claim 同强度的
    fencing CAS（worker+generation+claimed/running）直接落到终态：
    - state="cancelled"：任务已取消（取消方负责任务侧状态）；
    - state="failed"：其他合同失效（授权撤销等）。
    不重验授权不是绕过——工作合同已不存在，租约 fencing 就是唯一裁决。
    """
    if state not in {"failed", "cancelled"}:
        raise ValueError("invalid_abort_state")
    now = now or utcnow()
    result = session.execute(update(ExecutionRun).where(
        ExecutionRun.id == run_id, ExecutionRun.lease_owner == worker,
        ExecutionRun.generation == generation,
        ExecutionRun.state.in_(["claimed", "running"]),
        ExecutionRun.lease_until > now).values(
        state=state, lease_owner="", lease_until=None,
        error=str(error or "")[:500]))
    session.flush()
    if result.rowcount != 1:
        raise ValueError("stale_run_lease")
    return state


def recover_expired(session, *, now=None):
    now = now or utcnow()
    recovered = []
    # R05：等待类 Run 若仍持有已过期租约（崩溃于释放前/旧数据），同样进入
    # 对账恢复路径；已释放租约的等待 Run 安全停放，由唤醒/恢复机制续跑。
    stale = and_(ExecutionRun.lease_until.is_not(None),
                 ExecutionRun.lease_until <= now,
                 ExecutionRun.state.in_(["claimed", "running", *RELEASE_LEASE_STATES]))
    for run in session.scalars(select(ExecutionRun).where(stale)):
        # No blind re-execution of possibly completed external tools.
        run.state = "waiting_recovery"
        run.generation += 1
        run.lease_owner = ""
        run.lease_until = None
        run.error = "worker_lease_expired: reconcile checkpoint and side effects before resume"
        recovered.append(run.id)
    session.flush()
    return recovered


def resume(session, run_id, *, reconcile, disposition, note="", now=None):
    """waiting_recovery 的显式恢复：先对账 checkpoint 与已发生副作用再决定去向。

    - reconcile(session, run) -> bool：副作用对账回调；返回 False 视为对账未通过。
    - disposition：queued（可重新认领）或 failed（对账失败/副作用不可恢复）。
    版本绑定：task revision 或 context 变化时拒绝恢复，要求按新版本重新起 Run。
    """
    if disposition not in {"queued", "failed"}:
        raise ValueError("invalid_resume_disposition")
    if reconcile is None:
        raise ValueError("reconcile_callback_required")
    run = session.get(ExecutionRun, run_id)
    if run is None:
        raise ValueError("run_not_found")
    if run.state != "waiting_recovery":
        raise ValueError("run_not_awaiting_recovery")
    task = session.get(Task, run.task_id)
    if task is None or task.revision != run.task_revision or task.status in {"done", "cancelled"}:
        raise ValueError("work_revision_changed")
    context = json.loads(run.context_json)
    if sha256_json(context) != run.context_hash:
        raise ValueError("context_changed")
    if not reconcile(session, run):
        disposition = "failed"
        note = (note + " | reconciliation_failed").strip(" |")
    run.state = disposition
    run.error = "" if disposition == "queued" else (note or "reconciliation_failed")
    if disposition == "queued":
        run.attempts = run.attempts  # 恢复不是新失败，不累计 attempts
    # R06：恢复统一 Task/Run 语义——Task 停在 running 而 Run 已退出执行时，
    # 经唯一状态机走恢复转换（running→queued / running→failed）并留 TaskEvent，
    # 后续 claim→transition_to(running) 不会再撞 invalid_task_transition。
    if task.status == "running":
        service.transition_to(session, task.id,
                              to_state="queued" if disposition == "queued" else "failed",
                              actor="recovery",
                              reason=("run 恢复对账通过，重新排队待认领"
                                      if disposition == "queued"
                                      else f"run 恢复对账未通过：{note or 'reconciliation_failed'}"))
    session.flush()
    return run
