"""DE-40 回归：岗位队列、资源租约、优先级中断与确定性调度。

覆盖 tasks.json DE-40 的验收场景 1 与 3（全部离线：内存 SQLite、fake 授权端口、
固定时钟推进，无网络、无外部服务）：

场景 1a：双岗位争同一资源不并发覆盖（资源租约 + CAS version 生效）；
场景 1b：高优先级接手保存旧 Run 检查点且不重复扣费/执行（可恢复边界暂停）；
场景 3a：没有合格岗位时等待负责人/澄清，不无限增殖 Agent；
场景 3b：崩溃后原派工与在途副作用可对账（原 Run 幂等复用，租约到期不死锁）。

另覆盖：预算等待不计失败（DE-15 接线）、组队决定执行人（DE-38 接线）、
协调提案复核入队不另起调度（DE-21 接线）、agents 工作执行引擎接线。
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from evoharness.clock import utcnow
from evoharness.governance import budget as budget_svc
from evoharness.models import Base, Task, TaskEvent, TaskStatus
from evoharness.organization import service as org
from evoharness.organization.models import Membership, Position
from evoharness.organization.service import AuthenticatedPrincipal
from evoharness.work import execution, scheduling, service, wakeups
from evoharness.work.models import ExecutionRun

OWNER = AuthenticatedPrincipal(principal_id="founder", tenant_id="tn", app_id="app",
                               authenticated=True,
                               authorities=frozenset({"organization.bootstrap",
                                                      "organization.manage",
                                                      "work.execute", "knowledge.read"}))


def _principal(pid: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(principal_id=pid, tenant_id="tn", app_id="app",
                                  authenticated=True, authorities=frozenset())


def _org_validator(principal: AuthenticatedPrincipal):
    def validate(session, context, action):
        try:
            org.validate_context(session, org.ResolvedContext(context, principal), action)
            return True
        except org.OrganizationError:
            return False

    return validate


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def org_setup(session):
    """单组织两单元：swe（dev/support，能力 code.fix）与 qa_eng（qa/verify，能力 test.verify）。"""
    org.bootstrap_organization(session, OWNER, org_id="org", title="EvoHarness",
                               membership_id="m0", budget_limit=100)
    org.create_unit(session, OWNER, org_id="org", unit_id="dev", title="研发")
    org.create_unit(session, OWNER, org_id="org", unit_id="qa", title="质量")
    org.create_space(session, OWNER, org_id="org", space_id="support", title="支持")
    org.create_space(session, OWNER, org_id="org", space_id="verify", title="验收")
    org.bind_space(session, OWNER, org_id="org", unit_id="dev", space_id="support")
    org.bind_space(session, OWNER, org_id="org", unit_id="qa", space_id="verify")
    org.bind_space(session, OWNER, org_id="org", unit_id="qa", space_id="support")
    org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="swe",
                        title="工程师", mission="修复缺陷", actions={"work.execute"},
                        budget_limit=10, capability_refs=["code.fix"])
    org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="reviewer",
                        title="审阅岗", mission="审阅补丁", actions={"knowledge.read"},
                        capability_refs=["code.review"], budget_limit=5)
    org.create_position(session, OWNER, org_id="org", unit_id="qa", position_id="qa_eng",
                        title="测试工程师", mission="验证交付", actions={"work.execute"},
                        capability_refs=["test.verify"], budget_limit=8)
    org.add_member(session, OWNER, org_id="org", membership_id="m1", principal_id="alice",
                   tenant_id="tn", app_id="app", grants={"work.execute", "knowledge.read"},
                   principal_kind="ai_service")
    org.add_member(session, OWNER, org_id="org", membership_id="m2", principal_id="bob",
                   tenant_id="tn", app_id="app", grants={"work.execute", "knowledge.read"},
                   principal_kind="human")
    org.create_employee(session, OWNER, org_id="org", employee_id="emp1",
                        membership_id="m1", display_name="数字员工A")
    org.create_employee(session, OWNER, org_id="org", employee_id="emp2",
                        membership_id="m2", display_name="员工B")
    org.assign(session, OWNER, org_id="org", assignment_id="a1", position_id="swe",
               employee_id="emp1", space_ids={"support"}, actions={"work.execute"},
               budget_limit=5)
    org.assign(session, OWNER, org_id="org", assignment_id="a2", position_id="reviewer",
               employee_id="emp1", space_ids={"support"}, actions={"knowledge.read"})
    org.assign(session, OWNER, org_id="org", assignment_id="a3", position_id="qa_eng",
               employee_id="emp2", space_ids={"verify"}, actions={"work.execute"},
               budget_limit=8)
    budget_svc.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=100000)
    budget_svc.set_limit(session, scope_kind="space", scope_id="support", limit_tokens=50000)
    budget_svc.set_limit(session, scope_kind="space", scope_id="verify", limit_tokens=50000)
    return {"alice": _principal("alice"), "bob": _principal("bob")}


def _context_factory(principals: dict):
    """调度授权装配：按任职主体解析组织授权快照与适配器（生产装配同构）。"""

    def factory(session, task, executor):
        member = session.get(Membership, executor["membership_id"])
        principal = principals[member.principal_id]
        context = org.resolve_context(session, principal, org_id="org",
                                      space_id=task.space_id,
                                      assignment_id=executor["assignment_id"]).values
        return context, _org_validator(principal)

    return factory


def _delegated_task(session, *, task_key: str, space_id: str = "support",
                    position: str = "swe", acceptance: str = '[{"criterion": "回归通过"}]',
                    due_at=None, goal="修复登录缺陷") -> Task:
    task = Task(task_key=task_key, tenant="tn", owner="founder", chat_id="chat",
                title=goal[:20], action=goal, status=TaskStatus.OPEN,
                workflow_kind="delegated_delivery", goal=goal, acceptance_json=acceptance,
                org_id="org", space_id=space_id, assigned_position=position, due_at=due_at)
    session.add(task)
    session.flush()
    service.transition_to(session, task.id, to_state="queued", actor="founder")
    session.flush()
    return task


def _scheduled(report, task_key):
    return next((e for e in report["scheduled"] if e["task_key"] == task_key), None)


def _waiting(report, task_key):
    return next((e for e in report["waiting"] if e["task_key"] == task_key), None)


# ── 场景 1a：双岗位争同一资源不并发覆盖（租约 + CAS） ─────────────────────────


def test_two_positions_contend_same_resource_without_overwrite(session, org_setup):
    acceptance = json.dumps([{"criterion": "回归通过",
                              "resource": {"kind": "code_branch", "key": "repo#main"}}])
    task_a = _delegated_task(session, task_key="k-res-a", acceptance=acceptance)
    task_b = _delegated_task(session, task_key="k-res-b", acceptance=acceptance)

    report = scheduling.schedule_pending(session, org_id="org",
                                         context_factory=_context_factory(org_setup))
    assert _scheduled(report, "k-res-a") is not None
    # 第二个岗位争同一资源：等待，不并发覆盖
    conflict = _waiting(report, "k-res-b")
    assert conflict is not None and conflict["reason"] == "resource_leased"
    lease = session.scalar(select(scheduling.ResourceLease))
    assert lease.holder_id == str(task_a.id) and lease.status == "held"

    # CAS 内容写：持有者可推进版本；落后版本/非持有者一律拒绝
    new_version = scheduling.lease_write(session, lease, holder_id=str(task_a.id),
                                         expected_version=0, writer="a1")
    assert new_version == 1
    with pytest.raises(scheduling.SchedulingError, match="lease_version_conflict"):
        scheduling.lease_write(session, lease, holder_id=str(task_a.id),
                               expected_version=0)  # 落后版本
    with pytest.raises(scheduling.SchedulingError, match="lease_version_conflict"):
        scheduling.lease_write(session, lease, holder_id=str(task_b.id),
                               expected_version=1)  # 非持有者

    # 任务 a 结束（取消）并释放资源后下一轮：b 拿到租约（仍走同一 Run 服务，
    # 不另建调度状态；CAS 版本跨持有者延续，不清零）
    service.transition_to(session, task_a.id, to_state="cancelled", actor="founder")
    scheduling.release_lease(session, lease.lease_id, holder_id=str(task_a.id))
    report2 = scheduling.schedule_pending(session, org_id="org",
                                          context_factory=_context_factory(org_setup))
    assert _scheduled(report2, "k-res-b") is not None
    lease = session.scalar(select(scheduling.ResourceLease))
    assert lease.holder_id == str(task_b.id) and lease.version == 1  # 版本延续，未清零


def test_acquire_lease_conflict_and_expiry(session, org_setup):
    now = utcnow()
    first = scheduling.acquire_lease(session, org_id="org", resource_kind="document",
                                     resource_key="doc-1", holder_id="task-1",
                                     ttl_seconds=60, now=now)
    assert first is not None and first.status == "held"
    assert scheduling.acquire_lease(session, org_id="org", resource_kind="document",
                                    resource_key="doc-1", holder_id="task-2",
                                    ttl_seconds=60, now=now) is None
    # 过期租约原子接管，崩溃/超时不死锁
    expired = scheduling.acquire_lease(session, org_id="org", resource_kind="document",
                                       resource_key="doc-1", holder_id="task-2",
                                       ttl_seconds=600,
                                       now=now + timedelta(seconds=120))
    assert expired is not None and expired.holder_id == "task-2"
    assert expired.holder_epoch == 2  # 接管计数，历史可对账
    with pytest.raises(scheduling.SchedulingError, match="lease_holder_mismatch"):
        scheduling.release_lease(session, expired.lease_id, holder_id="task-1")


# ── 场景 1b：高优先级接手保存旧 Run 检查点且不重复扣费/执行 ───────────────────


def test_priority_pause_saves_checkpoint_and_settles_once(session, org_setup):
    task_low = _delegated_task(session, task_key="k-low")
    report = scheduling.schedule_pending(session, org_id="org",
                                         context_factory=_context_factory(org_setup),
                                         max_concurrency=1, estimated_tokens=100)
    low = _scheduled(report, "k-low")
    run = session.get(ExecutionRun, low["run_id"])

    claimed = execution.claim(session, run.id, worker="w1",
                              validate_context=_org_validator(org_setup["alice"]))
    service.transition_to(session, task_low.id, to_state="running", actor="w1")
    execution.transition(session, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_org_validator(org_setup["alice"]),
                         checkpoint={"done": ["复现缺陷"], "next": "写补丁"})
    assert run.attempts == 1

    # 高优先级工作到达：容量满 → 等待（不算失败）
    task_high = _delegated_task(session, task_key="k-high",
                                due_at=utcnow() + timedelta(days=1))
    report_wait = scheduling.schedule_pending(session, org_id="org",
                                              context_factory=_context_factory(org_setup),
                                              max_concurrency=1, estimated_tokens=100)
    assert _waiting(report_wait, "k-high")["reason"] == "no_capacity"

    # 高优先级接手：只在可恢复边界（持有租约的 worker 交出检查点）暂停
    scheduling.pause_run_for_priority(
        session, run.id, worker="w1", generation=claimed.generation,
        validate_context=_org_validator(org_setup["alice"]),
        checkpoint={"done": ["复现缺陷"], "next": "写补丁"},
        reservation_id=low["reservation_id"], used_tokens=300,
        reason="priority: k-high")
    assert run.state == "paused" and task_low.status == "paused"
    assert json.loads(run.checkpoint_json)["done"] == ["复现缺陷"]

    # 不重复扣费：真实用量 300 只结算一次；重复结算被 DE-15 拒绝
    summary = budget_svc.usage_summary(session, org_id="org")
    assert summary["used_total"] == 300
    with pytest.raises(budget_svc.BudgetError, match="reservation_already_closed"):
        budget_svc.settle(session, low["reservation_id"], used_tokens=300)

    # 容量腾出后：调度恢复被让路的 Run（回队列，不是新失败），
    # 高优先级任务先被派工
    report2 = scheduling.schedule_pending(session, org_id="org",
                                          context_factory=_context_factory(org_setup),
                                          max_concurrency=1, estimated_tokens=100)
    assert {"run_id": run.id, "task_key": "k-low"} in report2["resumed"]
    assert run.state == "queued" and task_low.status == "queued"
    assert run.attempts == 1  # 恢复不是新执行尝试
    assert report2["scheduled"][0]["task_key"] == "k-high"
    resumed = _scheduled(report2, "k-low")
    assert resumed is not None and resumed["run_id"] == run.id
    runs = list(session.scalars(select(ExecutionRun).where(
        ExecutionRun.task_id == task_low.id)))
    assert [r.id for r in runs] == [run.id]  # 不重复建 Run
    # 续跑是新工作新预留；旧调用已结算用量不重复入账
    summary2 = budget_svc.usage_summary(session, org_id="org")
    assert summary2["used_total"] == 300
    assert summary2["reserved_open"] == 200  # 高优先级 100 + 低优先级续跑 100

    # 高优先级 Run 占住执行位，低优先级 Run 保留检查点等待（不重复执行）
    high_run = session.get(ExecutionRun, report2["scheduled"][0]["run_id"])
    execution.claim(session, high_run.id, worker="w2",
                    validate_context=_org_validator(org_setup["alice"]))
    execution.transition(session, high_run.id, worker="w2",
                         generation=high_run.generation, state="running",
                         validate_context=_org_validator(org_setup["alice"]))
    assert json.loads(run.checkpoint_json)["done"] == ["复现缺陷"]

    # 高优先级完成后，低优先级从同一检查点续跑（不重做已完成步骤）
    execution.transition(session, high_run.id, worker="w2",
                         generation=high_run.generation, state="waiting_acceptance",
                         validate_context=_org_validator(org_setup["alice"]))
    re_claimed = execution.claim(session, run.id, worker="w3",
                                 validate_context=_org_validator(org_setup["alice"]))
    assert re_claimed is not None
    execution.transition(session, run.id, worker="w3", generation=re_claimed.generation,
                         state="running", validate_context=_org_validator(org_setup["alice"]))
    assert run.attempts == 2  # 第二次真实执行，而非恢复重复计数
    assert json.loads(run.checkpoint_json)["done"] == ["复现缺陷"]


# ── 等待不计失败：预算耗尽是等待，不是执行失败 ────────────────────────────────


def test_budget_wait_does_not_count_failure(session, org_setup):
    budget_svc.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=50)
    task = _delegated_task(session, task_key="k-budget")
    report = scheduling.schedule_pending(session, org_id="org",
                                         context_factory=_context_factory(org_setup),
                                         estimated_tokens=1000)
    wait = _waiting(report, "k-budget")
    assert wait is not None and wait["reason"] == "budget_exhausted"
    # 等待语义：任务保持排队，无 Run、无失败、无预留泄漏
    assert session.get(Task, task.id).status == "queued"
    assert session.scalar(select(func.count()).select_from(ExecutionRun).where(
        ExecutionRun.task_id == task.id)) == 0
    assert budget_svc.usage_summary(session, org_id="org")["reserved_open"] == 0
    # 预算恢复后同一调度即放行，不补记任何失败
    budget_svc.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=100000)
    report2 = scheduling.schedule_pending(session, org_id="org",
                                          context_factory=_context_factory(org_setup),
                                          estimated_tokens=1000)
    assert _scheduled(report2, "k-budget") is not None


# ── 场景 3a：无合格岗位等待负责人/澄清，不增殖 Agent ──────────────────────────


def test_no_qualified_position_waits_for_owner_not_new_agents(session, org_setup):
    acceptance = json.dumps([{"criterion": "回归通过", "capability": "quantum.leap"}])
    task = _delegated_task(session, task_key="k-nopos", acceptance=acceptance)
    tasks_before = session.scalar(select(func.count()).select_from(Task))
    runs_before = session.scalar(select(func.count()).select_from(ExecutionRun))

    report = scheduling.schedule_pending(session, org_id="org",
                                         context_factory=_context_factory(org_setup))
    wait = _waiting(report, "k-nopos")
    assert wait is not None and wait["reason"] == "no_qualified_position"
    # 转澄清等待负责人：带具体原因；不是失败态，也不是新任务/新 Run/新 Agent
    refreshed = session.get(Task, task.id)
    assert refreshed.status == TaskStatus.NEEDS_CONFIRMATION
    assert "no_qualified_position" in (refreshed.waiting_reason or "")
    assert "quantum.leap" in (refreshed.waiting_reason or "")
    assert session.scalar(select(func.count()).select_from(Task)) == tasks_before
    assert session.scalar(select(func.count()).select_from(ExecutionRun)) == runs_before
    events = list(session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)))
    assert any(e.event_type == "clarification_requested" for e in events)
    # 重复调度轮询不刷屏：等待态不再是候选，澄清事件只有一条
    report2 = scheduling.schedule_pending(session, org_id="org",
                                          context_factory=_context_factory(org_setup))
    assert _waiting(report2, "k-nopos") is None
    events2 = list(session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)))
    assert sum(1 for e in events2 if e.event_type == "clarification_requested") == 1

    # 负责人澄清 + 岗位取得能力后：同一任务恢复调度（无增殖产物）
    service.resolve_clarification(session, task.id, actor="founder", answer_message_id="m-1")
    position = session.get(Position, "swe")
    org.update_position_contract(session, OWNER, org_id="org", position_id="swe",
                                 expected_revision=position.revision,
                                 capability_refs=["code.fix", "quantum.leap"])
    service.transition_to(session, task.id, to_state="ready", actor="founder")
    service.transition_to(session, task.id, to_state="queued", actor="founder")
    report3 = scheduling.schedule_pending(session, org_id="org",
                                          context_factory=_context_factory(org_setup))
    assert _scheduled(report3, "k-nopos") is not None
    assert session.scalar(select(func.count()).select_from(Task)) == tasks_before


# ── 场景 3b：崩溃后原派工与在途副作用可对账 ──────────────────────────────────


def test_crash_reconciles_original_dispatch_and_side_effects(session, org_setup):
    task = _delegated_task(session, task_key="k-crash")
    report = scheduling.schedule_pending(session, org_id="org",
                                         context_factory=_context_factory(org_setup))
    entry = _scheduled(report, "k-crash")
    run = session.get(ExecutionRun, entry["run_id"])
    original_context = json.loads(run.context_json)
    assert original_context["assignment_id"] == "a1"

    # 执行器认领后崩溃：租约过期 → waiting_recovery，未知回执不自动写成功
    execution.claim(session, run.id, worker="w1",
                    validate_context=_org_validator(org_setup["alice"]),
                    now=utcnow() - timedelta(seconds=300), lease_seconds=120)
    recovered = execution.recover_expired(session, now=utcnow())
    assert run.id in recovered and run.state == "waiting_recovery"
    assert "reconcile" in run.error
    # 派工事实保留：原 Run、原授权上下文
    assert json.loads(run.context_json) == original_context

    # 调度器不绕过对账：waiting_recovery 期间不重复派工
    scheduling.schedule_pending(session, org_id="org",
                                context_factory=_context_factory(org_setup))
    assert session.scalar(select(func.count()).select_from(ExecutionRun).where(
        ExecutionRun.task_id == task.id)) == 1

    # 对账通过：原 Run 回到队列，原派工续用（同一 Run id，不重复执行）
    execution.resume(session, run.id, reconcile=lambda s, r: True,
                     disposition="queued", note="side effects verified")
    assert run.state == "queued" and run.attempts == 0
    report3 = scheduling.schedule_pending(session, org_id="org",
                                          context_factory=_context_factory(org_setup))
    assert _scheduled(report3, "k-crash")["run_id"] == run.id
    claimed = execution.claim(session, run.id, worker="w2",
                              validate_context=_org_validator(org_setup["alice"]))
    assert claimed is not None  # 同一 Run 可继续执行

    # 对账失败：显式 failed 留真失败，不伪装成功（再次崩溃后对账不过）
    execution.recover_expired(session, now=utcnow() + timedelta(seconds=600))
    assert run.state == "waiting_recovery"
    execution.resume(session, run.id, reconcile=lambda s, r: False,
                     disposition="queued", note="unmatched receipt")
    assert run.state == "failed" and "reconciliation_failed" in run.error

    # 在途副作用（资源租约）同样可对账：到期释放，不阻塞后续派工
    lease = scheduling.acquire_lease(session, org_id="org", resource_kind="code_branch",
                                     resource_key="repo#crash", holder_id=str(task.id),
                                     ttl_seconds=30, now=utcnow())
    assert lease is not None
    expired = scheduling.expire_leases(session, now=utcnow() + timedelta(seconds=60))
    assert lease.lease_id in expired
    freed = scheduling.acquire_lease(session, org_id="org", resource_kind="code_branch",
                                     resource_key="repo#crash", holder_id="task-other",
                                     ttl_seconds=600, now=utcnow() + timedelta(seconds=60))
    assert freed is not None and freed.holder_id == "task-other"


# ── 组队决定执行人（DE-38 接线）与协调提案入队（DE-21 接线） ──────────────────


def _proposal_validator(principal: AuthenticatedPrincipal, assignment_id: str):
    """提案复核适配器：独立解析主体在任授权（不依赖 Run 上下文快照）。"""

    def validate(session, context, action):
        org.resolve_context(session, principal, org_id="org", space_id="support",
                            assignment_id=assignment_id)
        return action == "work.execute"

    return validate


def test_crew_picks_executor_and_proposal_run_enters_same_queue(session, org_setup):
    task = _delegated_task(session, task_key="k-crew")
    # 第二个合格任职（bob 也在 swe/support 在任）：无组队时按确定性序取 a1
    org.assign(session, OWNER, org_id="org", assignment_id="a4", position_id="swe",
               employee_id="emp2", space_ids={"support"}, actions={"work.execute"},
               budget_limit=5)
    report = scheduling.schedule_pending(session, org_id="org",
                                         context_factory=_context_factory(org_setup))
    assert _scheduled(report, "k-crew")["executor_assignment"] == "a1"

    # 组队（DE-38）：执行人取组队 executor 名单；审阅者随 Run 上下文传递
    org.form_task_crew(session, OWNER, org_id="org", task_id=task.id, crew=[
        {"role": "accountable", "assignment_id": "a1"},
        {"role": "executor", "assignment_id": "a4"},
        {"role": "reviewer", "assignment_id": "a2"},
    ])
    existing = session.scalar(select(ExecutionRun).where(ExecutionRun.task_id == task.id))
    report2 = scheduling.schedule_pending(session, org_id="org",
                                          context_factory=_context_factory(org_setup))
    entry = _scheduled(report2, "k-crew")
    assert entry["run_id"] == existing.id  # 已派工不重复建 Run
    assert entry["reviewers"] == ["a2"]

    # 协调提案（DE-21 复核）→ request_run 进入同一执行队列，不另起调度状态
    task2 = _delegated_task(session, task_key="k-proposal")
    outcome = wakeups.coordinate(
        session, task2.id, actor="alice", kind="request_run",
        based_on_revision=task2.revision, payload={"trigger": "coordination"},
        proposal_id="prop-sched-1",
        validate_context=_proposal_validator(org_setup["alice"], "a1"))
    assert outcome["executed"] is True
    proposal_run_id = outcome["runId"]
    report3 = scheduling.schedule_pending(session, org_id="org",
                                          context_factory=_context_factory(org_setup))
    scheduled = _scheduled(report3, "k-proposal")
    assert scheduled is not None and scheduled["run_id"] == proposal_run_id
    # 全程只有一条 Run：提案派工与调度派工共用同一执行队列
    assert session.scalar(select(func.count()).select_from(ExecutionRun).where(
        ExecutionRun.task_id == task2.id)) == 1


# ── agents 侧最小接线：工作执行引擎类型 ──────────────────────────────────────


def test_work_executor_agent_wiring():
    import agents.agent as agent_mod
    import agents.subagent as subagent_mod
    from evoharness.work.executor import CODING_TOOLS

    config = agent_mod.resolve_sub_agent_config(subagent_mod.WORK_EXECUTOR_AGENT_TYPE)
    names = {t["name"] for t in config["tools"]}
    assert names == set(CODING_TOOLS)          # 与 Run 受限执行器同一白名单
    assert "agent" not in names                # 不递归创建子代理
    # 既有代理类型行为不变
    explore = agent_mod.resolve_sub_agent_config("explore")
    assert {t["name"] for t in explore["tools"]} == set(subagent_mod.READ_ONLY_TOOLS)
    general = agent_mod.resolve_sub_agent_config("general")
    assert "agent" not in {t["name"] for t in general["tools"]}
