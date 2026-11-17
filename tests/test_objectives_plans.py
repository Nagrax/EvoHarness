"""DE-39 回归：目标、里程碑与跨单元工作计划。

覆盖 tasks.json DE-39 的 4 条验收场景（全部离线：内存 SQLite、无网络、无外部服务）：
1. 一个目标跨两个单元分解协作；循环依赖/无责任任务/验收歧义被明确阻止或转澄清；
2. 聊天完成或 Run 退出不能关闭上层目标；关键交付撤回后能追到受影响里程碑
   （受影响集合正确，无影响交付保留）；
3. 普通补充保留目标；范围变更增加 revision 且旧决定不再生效；不重复创建平行任务；
4. 目标冲突/资料缺口/临时协作生成可核验提案；角色名字不等于工作能力
   （无能力证据的岗位不能接目标任务）。

接线说明：目标语义变更与计划确认的权限来自既有决定（DE-12
governance.authorization）；里程碑证据回写只在 DE-13 验收服务 done 之后；
目标/计划是 Task 的引用层，不造第二任务库。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from evoharness.governance import authorization as gov
from evoharness.governance.authorization import AuthorizationError
from evoharness.ids import sha256_json
from evoharness.models import Base, Message, Task, TaskEvent, TaskStatus
from evoharness.organization import service as org
from evoharness.organization.models import Position
from evoharness.organization.service import AuthenticatedPrincipal
from evoharness.work import acceptance, execution, objectives, plans
from evoharness.work.objectives import Milestone, ObjectiveError
from evoharness.work.objectives import record_task_evidence
from evoharness.work.plans import PlanError
from evoharness.work.service import apply_completion_signal, append_note, revise_contract, transition_to
from evoharness.work.supplements import enqueue as enqueue_supplement, pending as pending_supplements


OWNER = AuthenticatedPrincipal(principal_id="founder", tenant_id="tn", app_id="app",
                               authenticated=True,
                               authorities=frozenset({"organization.bootstrap",
                                                      "organization.manage",
                                                      "work.execute", "knowledge.read"}))


def _principal(pid: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(principal_id=pid, tenant_id="tn", app_id="app",
                                  authenticated=True, authorities=frozenset())


@pytest.fixture()
def env(tmp_path):
    out_root = tmp_path / "run-out"
    out_root.mkdir()
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        org.bootstrap_organization(s, OWNER, org_id="org", title="EvoHarness",
                                   membership_id="m0", budget_limit=100)
        org.create_unit(s, OWNER, org_id="org", unit_id="dev", title="研发")
        org.create_unit(s, OWNER, org_id="org", unit_id="qa", title="质量")
        org.create_space(s, OWNER, org_id="org", space_id="support", title="支持")
        org.create_space(s, OWNER, org_id="org", space_id="verify", title="验收")
        org.bind_space(s, OWNER, org_id="org", unit_id="dev", space_id="support")
        org.bind_space(s, OWNER, org_id="org", unit_id="qa", space_id="verify")
        org.bind_space(s, OWNER, org_id="org", unit_id="qa", space_id="support")
        org.create_position(s, OWNER, org_id="org", unit_id="dev", position_id="swe",
                            title="工程师", mission="修复缺陷", actions={"work.execute"},
                            budget_limit=10, capability_refs=["code.fix"])
        org.create_position(s, OWNER, org_id="org", unit_id="qa", position_id="qa_eng",
                            title="测试工程师", mission="验证交付", actions={"work.execute"},
                            budget_limit=8, capability_refs=["test.verify"])
        # 角色名≠能力：有头衔、无任何能力证据的岗位
        org.create_position(s, OWNER, org_id="org", unit_id="dev", position_id="boss",
                            title="总工程师", mission="统筹", actions=set(),
                            capability_refs=[])
        org.add_member(s, OWNER, org_id="org", membership_id="m1", principal_id="alice",
                       tenant_id="tn", app_id="app", grants={"work.execute", "knowledge.read"},
                       principal_kind="ai_service")
        org.add_member(s, OWNER, org_id="org", membership_id="m2", principal_id="bob",
                       tenant_id="tn", app_id="app", grants={"work.execute", "knowledge.read"},
                       principal_kind="human")
        org.create_employee(s, OWNER, org_id="org", employee_id="emp1", membership_id="m1",
                            display_name="数字员工A")
        org.create_employee(s, OWNER, org_id="org", employee_id="emp2", membership_id="m2",
                            display_name="员工B")
        org.assign(s, OWNER, org_id="org", assignment_id="a1", position_id="swe",
                   employee_id="emp1", space_ids={"support"}, actions={"work.execute"},
                   budget_limit=5)
        org.assign(s, OWNER, org_id="org", assignment_id="a3", position_id="qa_eng",
                   employee_id="emp2", space_ids={"verify"}, actions={"work.execute"},
                   budget_limit=8)
        yield SimpleNamespace(s=s, out_root=out_root)
    engine.dispose()


def _make_task(s, key: str, *, owner="founder", goal="交付补丁与回归", acceptance=None,
               org_id="org", space_id="support", assignee="bob") -> Task:
    task = Task(task_key=key, tenant="t", owner=owner, chat_id="chat", title=key,
                action=key, status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                goal=goal,
                acceptance_json=json.dumps(acceptance if acceptance is not None
                                           else [{"kind": "code", "criteria": "回归通过"}],
                                           ensure_ascii=False),
                revision=1, org_id=org_id, space_id=space_id, assignee_id=assignee)
    s.add(task)
    s.flush()
    return task


def _make_objective(s, objective_id: str, *, title="组织目标",
                    acceptance=None, owner="founder", unit_id="", parent_id=""):
    acceptance = acceptance if acceptance is not None else [{"criteria": "上线且回归全绿"}]
    payload = objectives.creation_payload(title=title, acceptance=acceptance,
                                          owner=owner, unit_id=unit_id, parent_id=parent_id)
    gov.approve(s, "founder", org_id="org", target_type="objective",
                target_id=objective_id, target_revision=1,
                payload_hash=sha256_json(payload), actions={"objective.manage"})
    return objectives.create_objective(s, org_id="org", objective_id=objective_id,
                                       title=title, acceptance=acceptance, owner=owner,
                                       actor_id="founder", unit_id=unit_id,
                                       parent_id=parent_id)


def _confirm_plan(s, proposal_id: str, *, actor="founder"):
    payload = plans.confirmation_payload(s, proposal_id=proposal_id)
    decision = gov.approve(s, actor, org_id="org", target_type="work_plan",
                           target_id=proposal_id,
                           target_revision=payload["based_on_revision"],
                           payload_hash=sha256_json(payload), actions={"plan.confirm"})
    plan = plans.confirm_plan(s, proposal_id=proposal_id, actor_id=actor)
    return plan, decision


def _node(task, *, unit="dev", position="swe", capability="code.fix",
          depends=(), milestone=""):
    return {"task_id": task.id, "unit_id": unit, "position_id": position,
            "required_capability": capability, "depends_on": list(depends),
            "milestone_id": milestone}


def _run_to_done(s, task: Task, out_root, *, executor="bob", reviewer="founder"):
    """真实走 DE-11 Run → DE-13 验收的 done 唯一入口。"""
    allow = lambda *args, **kwargs: True  # noqa: E731  授权适配桩
    run = execution.request_run(s, task_id=task.id, context={"key": task.task_key},
                                actor_id=executor, output_root=str(out_root),
                                idempotency_key="i-%s" % task.task_key,
                                validate_context=allow)
    transition_to(s, task.id, to_state="queued", actor=executor)
    claimed = execution.claim(s, run.id, worker="w1", validate_context=allow)
    transition_to(s, task.id, to_state="running", actor=executor)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=allow)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="waiting_acceptance", validate_context=allow)
    acceptance.request_acceptance(s, task.id, run_id=run.id, requester=executor)
    for name in ("diff.patch", "test_report.txt"):
        path = out_root / ("%s-%s" % (task.id, name))
        path.write_text("data-%s" % task.id, encoding="utf-8")
        acceptance.register_artifact(s, run, kind=name.rsplit(".", 1)[0], path=str(path))
    done = acceptance.review(s, task.id, run_id=run.id, reviewer=reviewer,
                             verdict="pass", notes="回归与差异齐备")
    return run, done


# ── 场景 1：跨单元分解协作 + 明确阻止/转澄清 ─────────────────────────────────


def test_cross_unit_objective_decomposes_into_two_unit_plan(env):
    s = env.s
    _make_objective(s, "obj-relay", title="跨单元交付目标")
    t1 = _make_task(s, "k-dev", space_id="support", assignee="bob")
    t2 = _make_task(s, "k-qa", space_id="verify", assignee="alice")
    objectives.add_milestone(s, objective_id="obj-relay", milestone_id="m-dev",
                             title="研发侧交付", deliverable_condition="补丁与回归报告齐备",
                             actor_id="founder", covered_by_task_ids=[t1.id])
    objectives.add_milestone(s, objective_id="obj-relay", milestone_id="m-qa",
                             title="验收侧交付", deliverable_condition="验收报告通过",
                             actor_id="founder", covered_by_task_ids=[t2.id])
    proposal = plans.propose_plan(
        s, org_id="org", objective_id="obj-relay", title="两单元接力计划",
        nodes=[_node(t1, milestone="m-dev"),
               _node(t2, unit="qa", position="qa_eng", capability="test.verify",
                     depends=[t1.id], milestone="m-qa")],
        actor_id="alice", evidence_refs=["msg-plan-1"],
        expected_effect="研发交付后进入质量验收")
    assert proposal.status == "open"
    plan, decision = _confirm_plan(s, proposal.proposal_id)
    assert plan.status == "confirmed" and decision.decision_id
    overview = plans.plan_overview(s, plan_id=plan.plan_id)
    assert sorted(n["task_id"] for n in overview["nodes"]) == sorted([t1.id, t2.id])
    qa_node = next(n for n in overview["nodes"] if n["task_id"] == t2.id)
    assert qa_node["depends_on"] == [t1.id]
    assert qa_node["task_revision_recorded"] == t2.revision
    # WorkPlan 只引用原 Task：任务数没有增加（不造第二任务库）
    assert s.scalar(select(func.count(Task.id))) == 2
    # 计划确认不改变目标状态/版本；进度按里程碑证据汇报而非 done 百分比
    progress = objectives.objective_progress(s, objective_id="obj-relay")
    assert progress["status"] == "active" and progress["revision"] == 1
    assert progress["counts"] == {"total": 2, "evidenced": 0, "pending": 2,
                                  "invalidated": 0}
    assert progress["percent_complete"] is None
    assert progress["progress_model"] == "milestone_evidence"


def test_cycle_unowned_and_ambiguous_are_blocked_or_clarified(env):
    s = env.s
    _make_objective(s, "obj-block", title="阻止规则目标")
    t1 = _make_task(s, "k-t1")
    t2 = _make_task(s, "k-t2")
    cycle_nodes = [_node(t1, depends=[t2.id]), _node(t2, depends=[t1.id])]
    # 循环依赖明确阻止
    with pytest.raises(PlanError, match="plan_dependency_cycle"):
        plans.propose_plan(s, org_id="org", objective_id="obj-block", title="环",
                           nodes=cycle_nodes, actor_id="alice",
                           evidence_refs=["msg-b1"])
    # 转澄清：落库 needs_clarification 并记录违规清单，不能被确认
    clarified = plans.propose_plan(s, org_id="org", objective_id="obj-block", title="环-澄清",
                                   nodes=cycle_nodes, actor_id="alice",
                                   evidence_refs=["msg-b1"], proposal_id="pp-clarify",
                                   on_violation="clarify")
    assert clarified.status == "needs_clarification"
    assert any(v.startswith("plan_dependency_cycle") for v in clarified.violations())
    with pytest.raises(PlanError, match="proposal_needs_clarification"):
        plans.confirm_plan(s, proposal_id="pp-clarify", actor_id="founder")
    # 无责任任务（缺责任岗位）明确阻止
    unowned = _node(t1)
    unowned["position_id"] = ""
    with pytest.raises(PlanError, match="plan_node_unowned:%d" % t1.id):
        plans.propose_plan(s, org_id="org", objective_id="obj-block", title="无责任",
                           nodes=[unowned], actor_id="alice", evidence_refs=["msg-b2"])
    # 验收歧义（空验收合同的任务）明确阻止
    t_amb = _make_task(s, "k-amb", acceptance=[])
    with pytest.raises(PlanError, match="plan_acceptance_ambiguous:%d" % t_amb.id):
        plans.propose_plan(s, org_id="org", objective_id="obj-block", title="歧义",
                           nodes=[_node(t_amb)], actor_id="alice",
                           evidence_refs=["msg-b3"])
    # 没有目标证据的提案不能进
    with pytest.raises(PlanError, match="proposal_evidence_required"):
        plans.propose_plan(s, org_id="org", objective_id="obj-block", title="无证据",
                           nodes=[_node(t1)], actor_id="alice", evidence_refs=[])


# ── 场景 2：聊天/Run 退出不关目标；撤回可追且无影响交付保留 ──────────────────


def test_chat_or_run_exit_cannot_close_objective_but_evidence_returns(env):
    s, out_root = env.s, env.out_root
    _make_objective(s, "obj-done", title="验收回写目标")
    t1 = _make_task(s, "k-done", assignee="bob")
    t2 = _make_task(s, "k-other", assignee="bob")
    objectives.add_milestone(s, objective_id="obj-done", milestone_id="m1",
                             title="交付里程碑", deliverable_condition="补丁与回归报告",
                             actor_id="founder", covered_by_task_ids=[t1.id])
    objectives.add_milestone(s, objective_id="obj-done", milestone_id="m2",
                             title="另一任务里程碑", deliverable_condition="独立交付",
                             actor_id="founder", covered_by_task_ids=[t2.id])
    message = Message(message_id="m-chat-done", tenant="t", chat_id="chat", thread_id="",
                      sender_id="bob", sender_name="bob", content_text="已完成",
                      create_time=0)
    s.add(message)
    s.flush()
    # 聊天完成信号：委派任务只记 acceptance_requested，不 done，目标不动
    completed = apply_completion_signal(s, message)
    assert completed == []
    assert s.get(Task, t1.id).status != TaskStatus.DONE
    progress = objectives.objective_progress(s, objective_id="obj-done")
    assert progress["status"] == "active" and progress["revision"] == 1
    assert progress["counts"]["evidenced"] == 0
    # Run 退出（waiting_acceptance / 失败）同样不碰目标
    allow = lambda *args, **kwargs: True  # noqa: E731
    run = execution.request_run(s, task_id=t1.id, context={"k": "done"},
                                actor_id="bob", output_root=str(out_root),
                                idempotency_key="i-done", validate_context=allow)
    transition_to(s, t1.id, to_state="queued", actor="bob")
    claimed = execution.claim(s, run.id, worker="w1", validate_context=allow)
    transition_to(s, t1.id, to_state="running", actor="bob")
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=allow)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="waiting_acceptance", validate_context=allow)
    assert objectives.objective_progress(s, objective_id="obj-done")["status"] == "active"
    # 验收通过（done 唯一入口）→ 目标仍 active：验收只可能回写里程碑证据
    acceptance.request_acceptance(s, t1.id, run_id=run.id, requester="bob")
    for name in ("diff.patch", "test_report.txt"):
        path = out_root / name
        path.write_text("data", encoding="utf-8")
        acceptance.register_artifact(s, run, kind=name.rsplit(".", 1)[0], path=str(path))
    done = acceptance.review(s, t1.id, run_id=run.id, reviewer="founder",
                             verdict="pass", notes="证据齐备")
    assert done.status == TaskStatus.DONE
    progress = objectives.objective_progress(s, objective_id="obj-done")
    assert progress["status"] == "active" and progress["revision"] == 1
    assert progress["counts"] == {"total": 2, "evidenced": 0, "pending": 2,
                                  "invalidated": 0}
    # 证据回写只更新它覆盖的里程碑（m2 覆盖 t2，不被 t1 触碰）
    record_task_evidence(s, task_id=t1.id, actor_id="founder",
                         artifact_refs=["art-1"], note="验收通过回写")
    progress = objectives.objective_progress(s, objective_id="obj-done")
    assert progress["counts"] == {"total": 2, "evidenced": 1, "pending": 1,
                                  "invalidated": 0}
    m1 = s.get(Milestone, "m1")
    assert m1.status == "evidenced" and m1.evidence()["task_revision"] == t1.revision
    assert s.get(Milestone, "m2").status == "pending"
    # 目标达成必须显式：先撞里程碑门（m2 未回写证据），决定存在也进不去
    gov.approve(s, "founder", org_id="org", target_type="objective",
                target_id="obj-done", target_revision=1,
                payload_hash=sha256_json(objectives.achieve_payload(milestone_ids=["m1"])),
                actions={"objective.manage"})
    with pytest.raises(ObjectiveError, match="objective_milestones_not_evidenced"):
        objectives.mark_achieved(s, objective_id="obj-done", actor_id="founder",
                                 expected_revision=1)
    # m2 证据到位后：既有决定只覆盖 m1，载荷不符仍拒绝（决定门）
    _run_to_done(s, t2, out_root)
    record_task_evidence(s, task_id=t2.id, actor_id="founder")
    with pytest.raises(AuthorizationError, match="decision_payload_changed"):
        objectives.mark_achieved(s, objective_id="obj-done", actor_id="founder",
                                 expected_revision=1)
    payload = objectives.achieve_payload(milestone_ids=["m1", "m2"])
    gov.approve(s, "founder", org_id="org", target_type="objective",
                target_id="obj-done", target_revision=1,
                payload_hash=sha256_json(payload), actions={"objective.manage"})
    achieved = objectives.mark_achieved(s, objective_id="obj-done",
                                        actor_id="founder", expected_revision=1)
    assert achieved.status == "achieved"
    events = [e.event_kind for e in objectives.objective_events(s, objective_id="obj-done")]
    assert "objective_achieved" in events
    # 幂等回放：同一验收版本重复回写不翻版本
    before = s.get(Milestone, "m1").revision
    record_task_evidence(s, task_id=t1.id, actor_id="founder")
    assert s.get(Milestone, "m1").revision == before


def test_withdrawn_delivery_traces_affected_milestones_and_keeps_unaffected(env):
    s, out_root = env.s, env.out_root
    _make_objective(s, "obj-w", title="撤回追踪目标")
    t1 = _make_task(s, "k-w1", space_id="support")
    t2 = _make_task(s, "k-w2", space_id="verify", assignee="alice")
    t3 = _make_task(s, "k-w3", space_id="support")
    for milestone_id, task, condition in (("m1", t1, "补丁A"), ("m2", t2, "验收B"),
                                          ("m3", t3, "独立交付C")):
        objectives.add_milestone(s, objective_id="obj-w", milestone_id=milestone_id,
                                 title=milestone_id, deliverable_condition=condition,
                                 actor_id="founder", covered_by_task_ids=[task.id])
    proposal = plans.propose_plan(
        s, org_id="org", objective_id="obj-w", title="三任务计划",
        nodes=[_node(t1, milestone="m1"),
               _node(t2, unit="qa", position="qa_eng", capability="test.verify",
                     depends=[t1.id], milestone="m2"),
               _node(t3, milestone="m3")],
        actor_id="alice", evidence_refs=["msg-w"], expected_effect="t1 失效影响 t2")
    plan, _ = _confirm_plan(s, proposal.proposal_id)
    # t1 与无依赖的 t3 真实验收 done 并回写证据
    _run_to_done(s, t1, out_root)
    record_task_evidence(s, task_id=t1.id, actor_id="founder")
    _run_to_done(s, t3, out_root)
    record_task_evidence(s, task_id=t3.id, actor_id="founder")
    assert s.get(Milestone, "m1").status == "evidenced"
    assert s.get(Milestone, "m3").status == "evidenced"
    # 交付仍完好（done）时不能凭空传播撤回
    with pytest.raises(PlanError, match="delivery_not_withdrawn"):
        plans.mark_delivery_withdrawn(s, plan_id=plan.plan_id, task_id=t1.id,
                                      reason="误报", actor_id="founder")
    # 真实撤回：done→queued 需新失败证据（唯一重开入口）
    transition_to(s, t1.id, to_state="queued", actor="founder", reason="线上复现",
                  evidence_message_ids=["m-reopen"])
    result = plans.mark_delivery_withdrawn(s, plan_id=plan.plan_id, task_id=t1.id,
                                           reason="关键补丁撤回重做", actor_id="founder")
    # 受影响集合正确：m1 失效、下游 t2 受影响；无影响的 t3/m3 原样保留
    assert result["affected_milestones"] == ["m1"]
    assert result["affected_task_ids"] == sorted([t1.id, t2.id])
    assert result["unaffected_task_ids"] == [t3.id]
    m1 = s.get(Milestone, "m1")
    assert m1.status == "invalidated"
    assert m1.evidence()["task_id"] == t1.id  # 历史完成快照保留
    assert any(h["kind"] == "evidence_invalidated" for h in m1.history())
    assert s.get(Milestone, "m3").status == "evidenced"
    # 下游任务获得依赖失效事件，但任务状态不被计划擅改
    assert s.get(Task, t2.id).status != "cancelled"
    events = [e.event_type for e in s.scalars(
        select(TaskEvent).where(TaskEvent.task_id == t2.id))]
    assert "plan_dependency_affected" in events
    # 目标不因撤回被标完成或取消
    progress = objectives.objective_progress(s, objective_id="obj-w")
    assert progress["status"] == "active"
    assert progress["counts"]["invalidated"] == 1
    # 幂等：重复传播不翻版本、不重复事件
    m1_revision = m1.revision
    events_before = s.scalar(select(func.count(TaskEvent.id)).where(
        TaskEvent.task_id == t2.id))
    result_again = plans.mark_delivery_withdrawn(s, plan_id=plan.plan_id,
                                                 task_id=t1.id, reason="重复信号",
                                                 actor_id="founder")
    assert result_again["affected_task_ids"] == result["affected_task_ids"]
    assert s.get(Milestone, "m1").revision == m1_revision
    assert s.scalar(select(func.count(TaskEvent.id)).where(
        TaskEvent.task_id == t2.id)) == events_before


# ── 场景 3：普通补充保留目标；范围变更杀旧决定；不建平行任务 ─────────────────


def test_supplement_keeps_goal_scope_change_kills_old_decisions_no_parallel_tasks(env):
    s, out_root = env.s, env.out_root
    _make_objective(s, "obj-scope", title="口径稳定目标")
    t1 = _make_task(s, "k-s1")
    objectives.add_milestone(s, objective_id="obj-scope", milestone_id="m-s1",
                             title="交付", deliverable_condition="补丁与回归",
                             actor_id="founder", covered_by_task_ids=[t1.id])
    proposal = plans.propose_plan(s, org_id="org", objective_id="obj-scope",
                                  title="单任务计划", nodes=[_node(t1, milestone="m-s1")],
                                  actor_id="alice", evidence_refs=["msg-s"])
    plan, _ = _confirm_plan(s, proposal.proposal_id)
    objective_events_before = len(objectives.objective_events(s, objective_id="obj-scope"))
    task_count_before = s.scalar(select(func.count(Task.id)))
    # 普通补充（在途追加 + 追加说明）：目标与任务 revision 都不动
    row = enqueue_supplement(s, t1.id, event_id="evt-note-1", event_kind="note",
                             payload={"text": "补充日志路径"})
    duplicate = enqueue_supplement(s, t1.id, event_id="evt-note-1", event_kind="note")
    assert duplicate.id == row.id  # 同一事件幂等，不重复执行
    assert len(pending_supplements(s, t1.id)) == 1
    append_note(s, t1, message_id="m-note-1", note="补充日志路径", actor="bob")
    assert t1.revision == 1
    progress = objectives.objective_progress(s, objective_id="obj-scope")
    assert progress["revision"] == 1 and progress["status"] == "active"
    assert len(objectives.objective_events(s, objective_id="obj-scope")) == objective_events_before
    assert plans.plan_staleness(s, plan_id=plan.plan_id)["stale_task_ids"] == []
    assert s.scalar(select(func.count(Task.id))) == task_count_before
    # 范围变更：task revision+1，绑定旧版本的决定不再生效
    task_decision = gov.approve(s, "founder", org_id="org", target_type="task",
                                target_id=str(t1.id), target_revision=1,
                                payload_hash=sha256_json({"contract": "v1"}),
                                actions={"task.execute"})
    revise_contract(s, t1.id, actor="founder", goal="交付补丁与回归（新范围）",
                    expected_revision=1, reason="验收口径变化",
                    evidence_message_ids=["m-scope"])
    assert t1.revision == 2
    with pytest.raises(AuthorizationError, match="decision_target_revision_changed"):
        gov.validate(s, target_type="task", target_id=str(t1.id), target_revision=2,
                     payload_hash=sha256_json({"contract": "v1"}),
                     action="task.execute", now=task_decision.created_at)
    stale = plans.plan_staleness(s, plan_id=plan.plan_id)
    assert stale["stale_task_ids"] == [t1.id]
    # 目标范围变更：显式版本事件；旧决定（绑定旧版本）不再生效
    creation_decision_payload = objectives.creation_payload(
        title="口径稳定目标", acceptance=[{"criteria": "上线且回归全绿"}], owner="founder")
    revise_payload = objectives.revision_payload(
        changes={"acceptance": [{"criteria": "新口径回归全绿"}]}, reason="验收口径调整")
    gov.approve(s, "founder", org_id="org", target_type="objective",
                target_id="obj-scope", target_revision=1,
                payload_hash=sha256_json(revise_payload),
                actions={"objective.manage"})
    revised = objectives.revise_objective(
        s, objective_id="obj-scope", actor_id="founder", expected_revision=1,
        reason="验收口径调整", acceptance=[{"criteria": "新口径回归全绿"}],
        evidence_refs=["m-revise"])
    assert revised.revision == 2
    events = [e.event_kind for e in objectives.objective_events(s, objective_id="obj-scope")]
    assert "acceptance_revised" in events
    # 创建时绑定 revision 1 的旧决定无法再授权 revision 2 上的任何动作
    with pytest.raises(AuthorizationError, match="decision_target_revision_changed"):
        gov.validate(s, target_type="objective", target_id="obj-scope",
                     target_revision=2,
                     payload_hash=sha256_json(creation_decision_payload),
                     action="objective.manage")
    # 不重复创建平行任务：同任务不能进同目标的两张已确认计划
    second = plans.propose_plan(s, org_id="org", objective_id="obj-scope",
                                title="平行计划尝试",
                                nodes=[_node(t1, milestone="m-s1")],
                                actor_id="alice", evidence_refs=["msg-s2"],
                                proposal_id="pp-second")
    assert second.status == "open"
    with pytest.raises(PlanError, match="task_already_planned:%d" % t1.id):
        _confirm_plan(s, "pp-second")
    assert s.scalar(select(func.count(Task.id))) == task_count_before


# ── 场景 4：可核验提案；角色名不等于能力 ─────────────────────────────────────


def test_conflict_gap_collab_proposals_verifiable_and_role_not_capability(env):
    s = env.s
    _make_objective(s, "obj-a", title="目标A")
    _make_objective(s, "obj-b", title="目标B")
    conflict = plans.raise_proposal(
        s, org_id="org", objective_id="obj-a", kind="goal_conflict",
        payload={"conflicting_objective_ids": ["obj-a", "obj-b"],
                 "conflict": "同一资源两条目标都想占用"},
        actor_id="alice", evidence_refs=["msg-c1"],
        expected_effect="由责任人裁决优先级")
    verified = plans.verify_proposal(s, proposal_id=conflict.proposal_id)
    assert verified["verifiable"] is True and verified["problems"] == []
    with pytest.raises(PlanError, match="conflict_objective_missing:obj-x"):
        plans.raise_proposal(s, org_id="org", objective_id="obj-a", kind="goal_conflict",
                             payload={"conflicting_objective_ids": ["obj-a", "obj-x"]},
                             actor_id="alice", evidence_refs=["msg-c2"])
    with pytest.raises(PlanError, match="goal_conflict_needs_two_objectives"):
        plans.raise_proposal(s, org_id="org", objective_id="obj-a", kind="goal_conflict",
                             payload={"conflicting_objective_ids": ["obj-a"]},
                             actor_id="alice", evidence_refs=["msg-c3"])
    gap = plans.raise_proposal(s, org_id="org", objective_id="obj-a", kind="data_gap",
                               payload={"missing_sources": ["fs://spec-v2", "doc://sla"]},
                               actor_id="bob", evidence_refs=["msg-g1"],
                               expected_effect="资料补齐后再拆计划")
    assert plans.verify_proposal(s, proposal_id=gap.proposal_id)["verifiable"] is True
    with pytest.raises(PlanError, match="data_gap_sources_required"):
        plans.raise_proposal(s, org_id="org", objective_id="obj-a", kind="data_gap",
                             payload={"missing_sources": []}, actor_id="bob",
                             evidence_refs=["msg-g2"])
    collab = plans.raise_proposal(
        s, org_id="org", objective_id="obj-a", kind="temporary_collab",
        payload={"needed_capability": "code.fix", "suggested_position_id": "swe",
                 "until": "里程碑 m1"},
        actor_id="bob", evidence_refs=["msg-t1"], expected_effect="临时借调修复能力")
    assert plans.verify_proposal(s, proposal_id=collab.proposal_id)["verifiable"] is True
    # 角色名≠能力：无能力证据的岗位不能被建议接目标任务
    with pytest.raises(PlanError, match="position_capability_missing:boss"):
        plans.raise_proposal(s, org_id="org", objective_id="obj-a",
                             kind="temporary_collab",
                             payload={"needed_capability": "code.fix",
                                      "suggested_position_id": "boss"},
                             actor_id="bob", evidence_refs=["msg-t2"])
    t1 = _make_task(s, "k-cap")
    with pytest.raises(PlanError, match="position_capability_missing:boss"):
        plans.propose_plan(s, org_id="org", objective_id="obj-a", title="无能力岗位",
                           nodes=[_node(t1, position="boss")], actor_id="alice",
                           evidence_refs=["msg-t3"])
    # 目标 revision 前进后：提案过期如实报告（可核验但不再可执行）
    revise_payload = objectives.revision_payload(
        changes={"acceptance": [{"criteria": "新口径"}]}, reason="口径调整")
    gov.approve(s, "founder", org_id="org", target_type="objective",
                target_id="obj-a", target_revision=1,
                payload_hash=sha256_json(revise_payload),
                actions={"objective.manage"})
    objectives.revise_objective(s, objective_id="obj-a", actor_id="founder",
                                expected_revision=1, reason="口径调整",
                                acceptance=[{"criteria": "新口径"}])
    stale_verify = plans.verify_proposal(s, proposal_id=conflict.proposal_id)
    assert stale_verify["verifiable"] is False
    assert "proposal_stale_revision" in stale_verify["problems"]


def test_objective_hierarchy_and_scope_guards(env):
    s = env.s
    root = _make_objective(s, "obj-root", title="组织级目标")
    # 单元目标挂组织目标
    unit_goal = _make_objective(s, "obj-dev", title="研发单元目标",
                                unit_id="dev", parent_id="obj-root")
    assert unit_goal.parent_id == "obj-root"
    # 父目标不在本组织/单元不匹配都被拒绝
    with pytest.raises(ObjectiveError, match="objective_parent_not_in_organization"):
        payload = objectives.creation_payload(title="坏父", acceptance=[{"criteria": "c"}],
                                              owner="founder", unit_id="dev",
                                              parent_id="obj-nowhere")
        gov.approve(s, "founder", org_id="org", target_type="objective",
                    target_id="obj-bad", target_revision=1,
                    payload_hash=sha256_json(payload), actions={"objective.manage"})
        objectives.create_objective(s, org_id="org", objective_id="obj-bad", title="坏父",
                                    acceptance=[{"criteria": "c"}], owner="founder",
                                    actor_id="founder", unit_id="dev",
                                    parent_id="obj-nowhere")
    # 空验收目标拒绝（验收歧义在建目标时就被阻止）
    payload = objectives.creation_payload(title="无验收", acceptance=[], owner="founder")
    gov.approve(s, "founder", org_id="org", target_type="objective",
                target_id="obj-empty", target_revision=1,
                payload_hash=sha256_json(payload), actions={"objective.manage"})
    with pytest.raises(ObjectiveError, match="objective_acceptance_required"):
        objectives.create_objective(s, org_id="org", objective_id="obj-empty",
                                    title="无验收", acceptance=[], owner="founder",
                                    actor_id="founder")
    # 无既有决定不能建目标（DE-12：确认目标的权限来自决定）
    with pytest.raises(AuthorizationError):
        objectives.create_objective(s, org_id="org", objective_id="obj-nodecision",
                                    title="无决定", acceptance=[{"criteria": "c"}],
                                    owner="founder", actor_id="mallory")
    # 无决定不能改目标
    with pytest.raises(AuthorizationError):
        objectives.revise_objective(s, objective_id="obj-root", actor_id="founder",
                                    expected_revision=1, reason="未授权修改",
                                    title="越权改")
