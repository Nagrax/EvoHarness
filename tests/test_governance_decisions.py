"""DE-41 回归：组织决策、审阅责任与统一接管。

覆盖 tasks.json DE-41 的 3 条验收场景（全部离线：文件 SQLite、无网络、无真实模型）：
1. 执行者不能用自造审阅身份批准自己越界；HTTP 正文 actor 或群聊指令不能增加权限；
2. 目标/授权/正文变化后旧批准无效；撤权和暂停及时传到工具/发送层并保留真实未知回执；
3. 有权用户可从根入口接管和恢复；系统不要求为每个无副作用步骤重新确认；
   预算熔断与暂停共用治理状态与 Run 状态机。

接线说明：agents.tools.check_permission 经 decisions.tool_gate 在 bypass/档位检查
之前重验组织授权交集；feishu outbox 适配器 _precheck 经 decisions.check_send_allowed
在发送前重验（发送层接线另见 feishu-daily-agent/tests/test_outbox_governance_gate.py）。
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

from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.governance import authorization, budget, decisions
from evoharness.governance.authorization import AuthorizationError
from evoharness.governance.budget import BudgetError
from evoharness.governance.decisions import GovernanceError
from evoharness.governance.models import Decision
from evoharness.models import Base, OutboxStatus, Task, TaskStatus
from evoharness.organization import service as org
from evoharness.organization.models import Assignment, Position, TenureReceipt
from evoharness.organization.service import AuthenticatedPrincipal, OrganizationError
from evoharness.outbox import enqueue
from evoharness.work import acceptance, execution, service as work_service, supplements
from evoharness.work.acceptance import AcceptanceError
from evoharness.work.models import ExecutionRun

OWNER = AuthenticatedPrincipal(principal_id="founder", tenant_id="tn", app_id="app",
                               authenticated=True,
                               authorities=frozenset({"organization.bootstrap", "organization.manage",
                                                      "work.execute", "work.review", "knowledge.read"}))


def _principal(pid: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(principal_id=pid, tenant_id="tn", app_id="app",
                                  authenticated=True, authorities=frozenset())


def _org_validator(principal: AuthenticatedPrincipal):
    """work.execution 的授权适配器：组织授权上下文失效即拒绝。"""

    def validate(session, context, action):
        try:
            org.validate_context(session, org.ResolvedContext(context, principal), action)
            return True
        except OrganizationError:
            return False

    return validate


@pytest.fixture()
def env(tmp_path):
    """组织 + 委派任务 + 已被 worker 认领进入 running 的执行 Run（DE-11 租约）。"""
    db_path = tmp_path / "gov-decisions.db"
    engine = create_engine("sqlite:///" + db_path.as_posix(), future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        org.bootstrap_organization(session, OWNER, org_id="org", title="EvoHarness",
                                   membership_id="m0", budget_limit=100)
        org.create_unit(session, OWNER, org_id="org", unit_id="dev", title="研发")
        org.create_space(session, OWNER, org_id="org", space_id="support", title="支持")
        org.bind_space(session, OWNER, org_id="org", unit_id="dev", space_id="support")
        org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="swe",
                            title="工程师", mission="修复缺陷", actions={"work.execute"},
                            budget_limit=10, capability_refs=["code.fix"])
        org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="auditor",
                            title="审阅岗", mission="独立审阅", actions={"work.review"},
                            budget_limit=5, capability_refs=["code.review"])
        org.add_member(session, OWNER, org_id="org", membership_id="m1", principal_id="alice",
                       tenant_id="tn", app_id="app", grants={"work.execute"},
                       principal_kind="ai_service")
        org.add_member(session, OWNER, org_id="org", membership_id="m2", principal_id="bob",
                       tenant_id="tn", app_id="app", grants={"work.execute", "work.review"},
                       principal_kind="human")
        # mallory 只有观察授权，但角色名挂着 org-admin/主管（角色名不等于权限）
        org.add_member(session, OWNER, org_id="org", membership_id="m3", principal_id="mallory",
                       tenant_id="tn", app_id="app", grants={"knowledge.read"},
                       roles=["org-admin", "主管"], delegable_actions={"knowledge.read"})
        org.create_employee(session, OWNER, org_id="org", employee_id="emp1", membership_id="m1",
                            display_name="数字员工A")
        org.create_employee(session, OWNER, org_id="org", employee_id="emp2", membership_id="m2",
                            display_name="审阅员B")
        org.create_employee(session, OWNER, org_id="org", employee_id="emp3", membership_id="m3",
                            display_name="观察员")
        org.assign(session, OWNER, org_id="org", assignment_id="a1", position_id="swe",
                   employee_id="emp1", space_ids={"support"}, actions={"work.execute"},
                   budget_limit=5)
        org.assign(session, OWNER, org_id="org", assignment_id="a2", position_id="auditor",
                   employee_id="emp2", space_ids={"support"}, actions={"work.review"})
        task = Task(task_key="de41-fix-login", tenant="tn", owner="founder", chat_id="chat",
                    thread_id="", title="修复登录缺陷", action="fix", assignee_id="alice",
                    status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                    goal="修复登录缺陷并补回归", acceptance_json='["回归通过"]',
                    org_id="org", space_id="support", assigned_position="swe")
        session.add(task)
        session.flush()
        context = org.resolve_context(session, _principal("alice"), org_id="org",
                                      space_id="support", assignment_id="a1").values
        alice = _principal("alice")
        run = execution.request_run(session, task_id=task.id, context=context, actor_id="alice",
                                    output_root="out", idempotency_key="run-de41",
                                    validate_context=_org_validator(alice))
        claimed = execution.claim(session, run.id, worker="worker-a",
                                  validate_context=_org_validator(alice))
        assert claimed is not None
        running = execution.transition(session, run.id, worker="worker-a", generation=1,
                                       state="running", validate_context=_org_validator(alice))
        assert running.state == "running"
        task_id, run_id = task.id, run.id
    # setup 事务先提交，测试块各自开独立事务（工具/发送层闸门用独立连接重验）
    yield SimpleNamespace(factory=factory,
                          db_url="sqlite:///" + db_path.as_posix(),
                          owner=OWNER, alice=_principal("alice"), bob=_principal("bob"),
                          mallory=_principal("mallory"),
                          task_id=task_id, run_id=run_id)
    engine.dispose()


# ── 场景 1：执行者不能用自造审阅身份批准自己越界；正文 actor / 群聊指令不能提权 ──


def test_executor_cannot_mint_reviewer_identity_to_approve_own_overreach(env):
    with env.factory.begin() as session:
        task = session.get(Task, env.task_id)
        run = session.get(ExecutionRun, env.run_id)
        # 自造审阅身份（HTTP 正文/群聊指令声明"首席审阅官"）：与可信会话主体不一致 → 拒绝
        with pytest.raises(GovernanceError, match="actor_claim_mismatch"):
            decisions.record_review_decision(session, org_id="org", reviewer=env.alice,
                                             task=task, run=run, verdict="pass",
                                             claimed_reviewer="首席审阅官")
        # 执行者本人冒充审阅者 → 拒绝（同一执行 Run 的自评分不冒充独立验收）
        with pytest.raises(GovernanceError, match="reviewer_must_differ_from_executor"):
            decisions.record_review_decision(session, org_id="org", reviewer=env.alice,
                                             task=task, run=run, verdict="pass")
        # 角色名（org-admin/主管）不构成审阅资格：无审阅任职与授权交集 → 拒绝
        with pytest.raises(GovernanceError, match="reviewer_not_authorized"):
            decisions.record_review_decision(session, org_id="org", reviewer=env.mallory,
                                             task=task, run=run, verdict="pass")
        with pytest.raises(GovernanceError, match="invalid_verdict"):
            decisions.record_review_decision(session, org_id="org", reviewer=env.bob,
                                             task=task, run=run, verdict="self-ok")
        # 持审阅任职（a2）的真实主体 → 审阅责任绑定成功，绑定当前 task revision
        decision = decisions.record_review_decision(session, org_id="org", reviewer=env.bob,
                                                    task=task, run=run, verdict="pass",
                                                    notes="独立复核通过")
        assert decision.target_revision == task.revision
        assert decision.grantee_id == "a2"
        assert json.loads(decision.actions_json) == ["work.review"]
        # 决定可重验：同一载荷 + 同一版本 + 审阅动作（R02：重验携带批准绑定的 grantee）
        payload = {"verdict": "pass", "run_id": run.id, "notes": "独立复核通过",
                   "artifact_hashes": []}
        ok = decisions.verify_decision(session, kind="review_responsibility",
                                       target_type="work_item", target_id=str(task.id),
                                       target_revision=task.revision, payload=payload,
                                       action="work.review", grantee_id="a2")
        assert ok.decision_id == decision.decision_id
        # DE-13 独立验收同样拒绝执行者自审（治理层与工作层口径一致）
        with pytest.raises(AcceptanceError, match="reviewer_must_differ_from_executor"):
            acceptance.review(session, task.id, run_id=run.id, reviewer="alice", verdict="pass")


def test_body_actor_and_group_command_cannot_escalate(env):
    with env.factory.begin() as session:
        assignment = session.get(Assignment, "a1")
        # HTTP 正文 actor 声明"ceo"≠会话绑定主体 → 拒绝
        with pytest.raises(GovernanceError, match="actor_claim_mismatch"):
            decisions.bind_decision(session, org_id="org", principal=env.mallory,
                                    kind="position_delegation", target_type="position_assignment",
                                    target_id="a1", target_revision=assignment.revision,
                                    payload={"assignment": "a1", "grant": "send_as_user"},
                                    actions={"send_as_user"}, claimed_actor="ceo")
        # 群聊指令把自己说成有委派权：主体真实但无管理者授权 → 拒绝（角色名不提权）
        with pytest.raises(GovernanceError, match="delegation_not_authorized"):
            decisions.bind_decision(session, org_id="org", principal=env.mallory,
                                    kind="position_delegation", target_type="position_assignment",
                                    target_id="a1", target_revision=assignment.revision,
                                    payload={"assignment": "a1"},
                                    actions={"send_as_user"})
        # 费用归属同样只认管理者
        with pytest.raises(GovernanceError, match="delegation_not_authorized"):
            decisions.bind_decision(session, org_id="org", principal=env.mallory,
                                    kind="cost_attribution", target_type="position",
                                    target_id="swe", target_revision=1,
                                    payload={"owner": "mallory"}, actions={"cost.attribute"})
        # 目标验收决定：非管理者且非任务 owner → 拒绝
        with pytest.raises(GovernanceError, match="acceptance_decision_not_authorized"):
            decisions.bind_decision(session, org_id="org", principal=env.mallory,
                                    kind="objective_acceptance", target_type="work_item",
                                    target_id=str(env.task_id), target_revision=1,
                                    payload={"goal": "x"}, actions={"objective.accept"})
        # 未认证的"主体"根本进不了决定通道
        with pytest.raises(GovernanceError, match="trusted_principal_required"):
            decisions.bind_decision(session, org_id="org",
                                    principal={"principal_id": "ghost"}, kind="position_delegation",
                                    target_type="position_assignment", target_id="a1",
                                    target_revision=assignment.revision,
                                    payload={"assignment": "a1"}, actions={"work.execute"})
        # 管理者（会话绑定主体）可绑定委派；决定的操作者是真实主体
        decision = decisions.bind_decision(session, org_id="org", principal=env.owner,
                                           kind="position_delegation",
                                           target_type="position_assignment", target_id="a1",
                                           target_revision=assignment.revision,
                                           payload={"assignment": "a1",
                                                    "actions": ["work.execute"]},
                                           actions={"work.execute"}, grantee_id="a1")
        assert decision.actor_id == "founder"
        # grantee 绑定：被授权主体之外的复用被拒（DE-12 语义复用）
        with pytest.raises(AuthorizationError, match="decision_grantee_mismatch"):
            decisions.verify_decision(session, kind="position_delegation",
                                      target_type="position_assignment", target_id="a1",
                                      target_revision=assignment.revision,
                                      payload={"assignment": "a1", "actions": ["work.execute"]},
                                      action="work.execute", grantee_id="a2")


# ── 场景 2：目标/授权/正文变化后旧批准无效；撤权与暂停传到发送层并保留真实回执 ──


def test_revision_payload_and_revoke_kill_old_approval_and_reach_send_layer(env):
    with env.factory.begin() as session:
        task = session.get(Task, env.task_id)
        payload = {"goal": task.goal}
        decision = decisions.bind_decision(session, org_id="org", principal=env.owner,
                                           kind="objective_acceptance",
                                           target_type="work_item", target_id=str(task.id),
                                           target_revision=task.revision, payload=payload,
                                           actions={"objective.accept"}, grantee_id="a2")
        ok = decisions.verify_decision(session, kind="objective_acceptance",
                                       target_type="work_item", target_id=str(task.id),
                                       target_revision=task.revision, payload=payload,
                                       action="objective.accept", grantee_id="a2")
        assert ok.decision_id == decision.decision_id
        # 目标语义变更（真实入口 revise_contract：revision+1）→ 旧批准失效
        bound_revision = decision.target_revision
        revised = work_service.revise_contract(session, task.id, actor="founder",
                                               goal="修复注册链路缺陷",
                                               expected_revision=bound_revision,
                                               reason="范围变更")
        assert revised.revision == bound_revision + 1
        # 目标已前进到新版本：旧批准（绑定 revision=bound_revision）对当前版本失效
        with pytest.raises(AuthorizationError, match="decision_target_revision_changed"):
            decisions.verify_decision(session, kind="objective_acceptance",
                                      target_type="work_item", target_id=str(task.id),
                                      target_revision=revised.revision, payload=payload,
                                      action="objective.accept", grantee_id="a2")
        # 正文变化 → 旧批准失效
        with pytest.raises(AuthorizationError, match="decision_payload_changed"):
            decisions.verify_decision(session, kind="objective_acceptance",
                                      target_type="work_item", target_id=str(task.id),
                                      target_revision=decision.target_revision,
                                      payload={"goal": "被篡改的目标"},
                                      action="objective.accept", grantee_id="a2")
        # 发送意图三种真实状态：排队 / 在途发送 / 未知回执
        pending = enqueue(session, intent="reply|d9|1", tenant="tn", kind="reply",
                          target_type="message", target_id="d9",
                          payload={"body": "答案", "chat": "c1"})
        sending = enqueue(session, intent="reply|d9|2", tenant="tn", kind="reply",
                          target_type="message", target_id="d9",
                          payload={"body": "答案", "chat": "c1"})
        sending.status = OutboxStatus.SENDING
        unknown = enqueue(session, intent="reply|d9|3", tenant="tn", kind="reply",
                          target_type="message", target_id="d9",
                          payload={"body": "答案", "chat": "c1"})
        unknown.status = OutboxStatus.DELIVERY_UNKNOWN
        session.flush()
        # 批准发送该草稿（发送层治理绑定）
        authorization.approve(session, "founder", org_id="org", target_type="draft",
                              target_id="d9", target_revision=1,
                              payload_hash="hash-d9", actions={"send_as_bot"}, grantee_id="a1")
        # 撤回：排队意图取消；在途 SENDING 行不回写成功，由发送层重验拦截；
        # 未知回执保持 unknown，不被改写为任何成功态
        cancelled = authorization.revoke(session, "founder", target_type="draft",
                                         target_id="d9", org_id="org")
        session.refresh(pending)
        assert pending.status == OutboxStatus.CANCELLED
        assert "decision_revoked" in pending.last_error
        assert cancelled == [pending.intent_key]
        ok_sending, reason_sending = decisions.check_send_allowed(session, sending)
        assert (ok_sending, reason_sending) == (False, "decision_revoked")
        ok_unknown, reason_unknown = decisions.check_send_allowed(session, unknown)
        assert (ok_unknown, reason_unknown) == (True, "")
        session.refresh(unknown)
        assert unknown.status == OutboxStatus.DELIVERY_UNKNOWN
        session.refresh(sending)
        assert sending.status == OutboxStatus.SENDING
        # 撤回治理绑定的目标验收决定后，重验失败（无任何活跃决定）
        authorization.revoke(session, "founder", target_type="work_item",
                             target_id=str(task.id), org_id="org")
        with pytest.raises(AuthorizationError, match="no_active_decision"):
            decisions.verify_decision(session, kind="objective_acceptance",
                                      target_type="work_item", target_id=str(task.id),
                                      target_revision=decision.target_revision, payload=payload,
                                      action="objective.accept", grantee_id="a2")


def test_pause_propagates_to_tool_and_send_layers(env):
    from agents import tools
    with env.factory.begin() as session:
        ctx = org.resolve_context(session, env.alice, org_id="org", space_id="support",
                                  assignment_id="a1")
        snapshot = ctx.snapshot()
    gate = decisions.tool_gate(env.factory, env.alice, snapshot)
    tools.set_organization_governance(gate)
    try:
        # 暂停前：bypass 档位下有副作用工具放行（授权交集有效）
        allowed = tools.check_permission("write_file",
                                         {"file_path": "note.txt", "content": "x"},
                                         mode="bypassPermissions")
        assert allowed["action"] == "allow"
        # 暂停组织（真实暂停入口）
        with env.factory.begin() as session:
            summary = decisions.pause_scope(session, env.owner, org_id="org",
                                            kind="organization", target_id="org",
                                            reason="紧急排查，暂停组织")
            assert summary["fenced_run_ids"] == [env.run_id]
            assert summary["receipt_ids"]
            # 在途 Run 围栏：状态不被盲改，取消走补充队列、副作用状态开对账单
            run = session.get(ExecutionRun, env.run_id)
            assert run.state == "running"
            assert supplements.has_pending_cancellation(session, env.task_id)
            receipts = org.open_receipts(session, org_id="org")
            assert [r.target_id for r in receipts] == [env.run_id]
        # 工具层即时拒绝：bypassPermissions / acceptEdits 都不能绕过授权交集
        denied = tools.check_permission("write_file",
                                        {"file_path": "note.txt", "content": "x"},
                                        mode="bypassPermissions")
        assert denied["action"] == "deny"
        assert "organization governance" in denied["message"]
        denied2 = tools.check_permission("edit_file",
                                         {"file_path": "note.txt", "content": "y"},
                                         mode="acceptEdits")
        assert denied2["action"] == "deny"
        # 读类未映射工具不受影响（无副作用步骤不重复确认）
        read = tools.check_permission("read_file", {"file_path": "note.txt"},
                                      mode="bypassPermissions")
        assert read["action"] == "allow"
        # 发送层即时拦截：有治理绑定的发送意图被暂停拦下
        with env.factory.begin() as session:
            authorization.approve(session, "founder", org_id="org", target_type="draft",
                                  target_id="d-pause", target_revision=1,
                                  payload_hash="hash-pause", actions={"send_as_bot"})
            row = enqueue(session, intent="reply|d-pause|1", tenant="tn", kind="reply",
                          target_type="message", target_id="d-pause",
                          payload={"body": "答案", "chat": "c1"})
            ok, reason = decisions.check_send_allowed(session, row)
            assert (ok, reason) == (False, "organization_paused")
        # 恢复（先核授权与目标版本）：组织重新可用
        with env.factory.begin() as session:
            resumed = decisions.resume_scope(session, env.owner, org_id="org",
                                             kind="organization", target_id="org")
            assert resumed["revision"] >= 2
            # 已围栏 Run 不自动复活：对账单仍开放，恢复后需显式对账
            assert resumed["open_receipt_ids"]
        # 上下文已因版本翻动失效：组合根重新绑定授权上下文后工具层恢复
        with env.factory.begin() as session:
            fresh = org.resolve_context(session, env.alice, org_id="org", space_id="support",
                                        assignment_id="a1").snapshot()
        tools.set_organization_governance(decisions.tool_gate(env.factory, env.alice, fresh))
        allowed_again = tools.check_permission("write_file",
                                               {"file_path": "note.txt", "content": "x"},
                                               mode="bypassPermissions")
        assert allowed_again["action"] == "allow"
    finally:
        tools.clear_organization_governance()
    # 闸门未注册时零行为变化（既有权限语义不变）
    untouched = tools.check_permission("write_file",
                                       {"file_path": "note.txt", "content": "x"},
                                       mode="bypassPermissions")
    assert untouched["action"] == "allow"


# ── 场景 3：根入口接管与恢复；无副作用步骤不重复确认；熔断与暂停共用状态 ────────


def test_authorized_user_takes_over_and_resumes_from_root_entry(env):
    with env.factory.begin() as session:
        # 无权用户（非管理者/非任务 owner/非 accountable 任职）不能接管
        with pytest.raises(GovernanceError, match="takeover_not_authorized"):
            decisions.takeover_run(session, env.mallory, org_id="org", run_id=env.run_id,
                                   reason="我想接管")
        # 有权用户（组织管理者）接管：围栏 + 清租约 + 对账单
        summary = decisions.takeover_run(session, env.owner, org_id="org", run_id=env.run_id,
                                         reason="执行异常，接管处理")
        assert summary["state"] == "waiting_recovery"
        assert summary["receipt_id"]
        assert summary["takeover_by"] == "founder"
    with env.factory.begin() as session:
        run = session.get(ExecutionRun, env.run_id)
        assert run.state == "waiting_recovery"
        assert run.lease_owner == "" and run.lease_until is None
        assert run.generation == 2
        assert supplements.has_pending_cancellation(session, env.task_id)
        assert [r.target_id for r in org.open_receipts(session, org_id="org")] == [env.run_id]
        # 恢复先核目标版本：任务 revision 未漂移 → 对账后回 queued（DE-11 语义）
        resumed = execution.resume(session, env.run_id, reconcile=lambda s, r: True,
                                   disposition="queued")
        assert resumed.state == "queued"
    # 根入口（DE-28 api 契约层）：任务级接管 = pause；恢复 = 转回 queued
    api = WorkApi(Application.open(env.db_url))
    paused = api.pause_work_item(env.task_id, actor="founder", reason="接管期间冻结")
    assert paused["ok"] and paused["data"]["state"] == "paused"
    with env.factory.begin() as session:
        assert session.get(Task, env.task_id).status == "paused"
        # 暂停的任务不能被继续认领（Run 租约校验拒绝）
        with pytest.raises(ValueError, match="work_revision_changed"):
            execution.claim(session, env.run_id, worker="worker-b",
                            validate_context=_org_validator(env.alice))
    requeued = api.transition_work_item(env.task_id, to_state="queued", actor="founder",
                                        reason="接管结束，恢复执行")
    assert requeued["ok"] and requeued["data"]["state"] == "queued"
    with env.factory.begin() as session:
        claimed = execution.claim(session, env.run_id, worker="worker-b",
                                  validate_context=_org_validator(env.alice))
        assert claimed is not None and claimed.state == "claimed"
    # 目标版本漂移后旧 Run 不能恢复：必须按新版本重新起 Run
    with env.factory.begin() as session:
        task = session.get(Task, env.task_id)
        work_service.revise_contract(session, task.id, actor="founder", goal="再次变更目标",
                                     expected_revision=task.revision, reason="范围二变")
    with env.factory.begin() as session:
        fenced = decisions.takeover_run(session, env.owner, org_id="org", run_id=env.run_id,
                                        reason="版本漂移，重新处理")
        assert fenced["state"] == "waiting_recovery"
        with pytest.raises(ValueError, match="work_revision_changed"):
            execution.resume(session, env.run_id, reconcile=lambda s, r: True,
                             disposition="queued")


def test_side_effect_free_steps_do_not_require_repeated_confirmation(env):
    with env.factory.begin() as session:
        ctx = org.resolve_context(session, env.alice, org_id="org", space_id="support",
                                  assignment_id="a1")
        snapshot = ctx.snapshot()
        before = session.scalar(select(func.count()).select_from(Decision))
        # 普通可验证工作：按已获授权自动推进，不逐个步骤要决定
        for _ in range(3):
            step = decisions.authorize_step(session, env.alice, snapshot,
                                            action="work.execute", impact="routine")
            assert step == {"impact": "routine", "mode": "auto",
                            "decision_required": False}
        # 高影响步骤：标记需要独立验收，但同样不凭自述改变状态
        high = decisions.authorize_step(session, env.alice, snapshot,
                                        action="work.execute", impact="high")
        assert high == {"impact": "high", "mode": "independent_review",
                        "decision_required": True}
        with pytest.raises(GovernanceError, match="invalid_impact_level"):
            decisions.authorize_step(session, env.alice, snapshot,
                                     action="work.execute", impact="critical")
        after = session.scalar(select(func.count()).select_from(Decision))
        assert after == before


def test_budget_breaker_and_pause_share_governance_and_run_state(env):
    from evoharness.governance.models import BudgetLimit
    with env.factory.begin() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=100)
    with env.factory.begin() as session:
        # 角色名不构成熔断权限
        with pytest.raises(GovernanceError, match="budget_breaker_not_authorized"):
            decisions.trip_budget_breaker(session, env.mallory, org_id="org",
                                          scope_kind="org", scope_id="org", reason="试试")
        # 管理员熔断：budget_limits 置零（同一治理状态，revision+1）
        summary = decisions.trip_budget_breaker(session, env.owner, org_id="org",
                                                scope_kind="org", scope_id="org",
                                                reason="成本熔断")
        assert summary["limit_tokens"] == 0 and summary["revision"] == 2
        # 预算预留被熔断拦下（等待语义）
        with pytest.raises(BudgetError, match="budget_exhausted"):
            budget.reserve(session, org_id="org", category="chat", tokens=10)
    # 执行器经同一 Run 状态机进入 waiting_budget（不新增平行状态）
    with env.factory.begin() as session:
        waiting = execution.transition(session, env.run_id, worker="worker-a", generation=1,
                                       state="waiting_budget",
                                       validate_context=_org_validator(env.alice))
        assert waiting.state == "waiting_budget"
        assert session.get(Task, env.task_id).status == "waiting_budget"
    # 恢复限额后预算恢复、Run 回到 running
    with env.factory.begin() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=100)
        budget.reserve(session, org_id="org", category="chat", tokens=10)
        # R05 语义适配（断言强度不减）：进入 waiting_budget 即释放执行租约，
        # 恢复执行必须先经 reacquire 重新授权认领（generation 递增），再回 running。
        reacquired = execution.reacquire(session, env.run_id, worker="worker-a",
                                         validate_context=_org_validator(env.alice))
        assert reacquired.generation == 2 and reacquired.lease_owner == "worker-a"
        resumed = execution.transition(session, env.run_id, worker="worker-a",
                                       generation=reacquired.generation,
                                       state="running",
                                       validate_context=_org_validator(env.alice))
        assert resumed.state == "running"
        assert session.get(Task, env.task_id).status == "running"
    # 岗位级暂停：与熔断共用治理状态（positions.status）与 Run 围栏（对账单）
    with env.factory.begin() as session:
        summary = decisions.pause_scope(session, env.owner, org_id="org", kind="position",
                                        target_id="swe", reason="岗位暂停")
        assert summary["fenced_run_ids"] == [env.run_id]
        assert session.get(Position, "swe").status == "paused"
        with pytest.raises(OrganizationError, match="position_or_scope_inactive"):
            org.resolve_context(session, env.alice, org_id="org", space_id="support",
                                assignment_id="a1")
    with env.factory.begin() as session:
        resumed = decisions.resume_scope(session, env.owner, org_id="org", kind="position",
                                         target_id="swe")
        assert session.get(Position, "swe").status == "active"
        # 恢复不自动闭合在途回执（对账单仍开放，等待显式对账）
        assert resumed["open_receipt_ids"]
        assert session.scalar(select(func.count()).select_from(TenureReceipt).where(
            TenureReceipt.status == "open")) >= 1
        assert session.scalar(select(func.count()).select_from(BudgetLimit)) == 1
