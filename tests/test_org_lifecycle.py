"""DE-38 回归：组织、业务单元、岗位与任职生命周期。

覆盖 tasks.json DE-38 的 4 条验收场景（全部离线：内存 SQLite、无网络、无外部服务）：
1. 两个单元至少三类岗位与人/AI 任职可配置；改岗位执行模型不丢目标和历史；
2. 离任/撤权后旧 Run 不能新增动作（取消传播 + 在途回执对账），接任者拿不到
   个人 token 或私有笔记，合法组织产物可接手；
3. 管理员/主管角色名不自动获得以别人身份发送/跨 scope 读取的权利；
4. 同一任务按能力在岗位间合法转交（能力不匹配被拒绝并给原因），多任职与
   临时组队合规可用；不要求机械流水线。

接线说明：work.execution 的 validate_context 适配为组织授权
（org.validate_context），授权上下文来自 resolve_context 的 values 快照。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.clock import utcnow
from evoharness.governance import authorization as gov
from evoharness.ids import sha256_json
from evoharness.knowledge import service as knowledge
from evoharness.models import Base, Task, TaskEvent, TaskStatus
from evoharness.organization import service as org
from evoharness.organization.models import (Assignment, EmployeeProfile, Membership,
                                            OrganizationEvent, Position, TaskCrewMember)
from evoharness.organization.service import AuthenticatedPrincipal, OrganizationError
from evoharness.work import execution, supplements
from evoharness.work.models import Artifact, ExecutionRun


OWNER = AuthenticatedPrincipal(principal_id="founder", tenant_id="tn", app_id="app",
                               authenticated=True,
                               authorities=frozenset({"organization.bootstrap", "organization.manage",
                                                      "work.execute", "knowledge.read"}))


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
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def org_setup(session):
    """两个单元、四类岗位、人/AI 服务主体任职与指派的完整组织。

    - 单元 dev（绑定 support 空间）；单元 qa（绑定 verify 与 support 空间）；
    - 岗位：swe（code.fix）/ reviewer（code.review）/ qa_eng（test.verify）/ docs（write.docs）；
    - 成员：alice（ai_service）、bob（human）、mallory（human，角色名 org-admin/主管）。
    """
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
                        title="工程师", mission="修复缺陷", actions={"work.execute"}, budget_limit=10,
                        inputs=["缺陷单"], outputs=["补丁"], acceptance=["回归通过"],
                        capability_refs=["code.fix"])
    org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="reviewer",
                        title="审阅岗", mission="审阅补丁", actions={"knowledge.read"},
                        capability_refs=["code.review"], budget_limit=5)
    org.create_position(session, OWNER, org_id="org", unit_id="qa", position_id="qa_eng",
                        title="测试工程师", mission="验证交付", actions={"work.execute"},
                        capability_refs=["test.verify"], budget_limit=8)
    org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="docs",
                        title="文档岗", mission="沉淀文档", actions={"knowledge.read"},
                        capability_refs=["write.docs"], budget_limit=2)
    org.add_member(session, OWNER, org_id="org", membership_id="m1", principal_id="alice",
                   tenant_id="tn", app_id="app", grants={"work.execute", "knowledge.read"},
                   principal_kind="ai_service")
    org.add_member(session, OWNER, org_id="org", membership_id="m2", principal_id="bob",
                   tenant_id="tn", app_id="app", grants={"work.execute", "knowledge.read"},
                   principal_kind="human")
    org.add_member(session, OWNER, org_id="org", membership_id="m3", principal_id="mallory",
                   tenant_id="tn", app_id="app", grants={"knowledge.read"},
                   roles=["org-admin", "主管"], delegable_actions={"knowledge.read"})
    org.create_employee(session, OWNER, org_id="org", employee_id="emp1", membership_id="m1",
                        display_name="数字员工A", runtime_binding="runner-alice-token")
    org.create_employee(session, OWNER, org_id="org", employee_id="emp2", membership_id="m2",
                        display_name="员工B", runtime_binding="runner-bob")
    org.create_employee(session, OWNER, org_id="org", employee_id="emp3", membership_id="m3",
                        display_name="观察员")
    org.assign(session, OWNER, org_id="org", assignment_id="a1", position_id="swe",
               employee_id="emp1", space_ids={"support"}, actions={"work.execute"}, budget_limit=5)
    org.assign(session, OWNER, org_id="org", assignment_id="a2", position_id="reviewer",
               employee_id="emp1", space_ids={"support"}, actions={"knowledge.read"})
    org.assign(session, OWNER, org_id="org", assignment_id="a3", position_id="qa_eng",
               employee_id="emp2", space_ids={"verify"}, actions={"work.execute"}, budget_limit=8)
    org.assign(session, OWNER, org_id="org", assignment_id="a6", position_id="reviewer",
               employee_id="emp3", space_ids={"support"}, actions={"knowledge.read"})
    return SimpleNamespace(
        alice=_principal("alice"), bob=_principal("bob"), mallory=_principal("mallory"))


def _delegated_task(session, *, task_key: str, space_id: str = "support",
                    position: str = "swe", goal: str = "修复登录缺陷") -> Task:
    task = Task(task_key=task_key, tenant="tn", owner="founder", chat_id="chat", thread_id="",
                title=goal[:20], action=goal, status=TaskStatus.OPEN,
                workflow_kind="delegated_delivery", goal=goal, acceptance_json='["回归通过"]',
                org_id="org", space_id=space_id, assigned_position=position)
    session.add(task)
    session.flush()
    session.add(TaskEvent(task_id=task.id, event_type="created", actor="system", detail="组织任务建立"))
    session.flush()
    return task


def _start_run(session, task: Task, principal, assignment_id: str, space_id: str,
               idempotency_key: str):
    context = org.resolve_context(session, principal, org_id="org", space_id=space_id,
                                  assignment_id=assignment_id).values
    run = execution.request_run(session, task_id=task.id, context=context,
                                actor_id=principal.principal_id, output_root="out",
                                idempotency_key=idempotency_key,
                                validate_context=_org_validator(principal))
    return run, context


# ── 场景 1：两单元/三类岗位/人 AI 任职可配置；改岗位执行模型不丢目标和历史 ──


def test_two_units_three_positions_and_tenure_replacement_preserve_history(session, org_setup):
    # 两个单元、四类岗位（覆盖三类以上）、AI 与人类任职都已配置生效
    assert session.get(Position, "swe").unit_id == "dev"
    assert session.get(Position, "qa_eng").unit_id == "qa"
    assert session.get(Membership, "m1").principal_kind == "ai_service"
    assert session.get(Membership, "m2").principal_kind == "human"
    assert org.position_state(session, org_id="org", position_id="swe") == "active"

    task = _delegated_task(session, task_key="k-s1", position="swe")
    before = (task.id, task.goal, task.acceptance_json, task.revision, task.assigned_position)
    events_before = [e.id for e in session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task.id))]

    position = session.get(Position, "swe")
    org.update_position_contract(session, OWNER, org_id="org", position_id="swe",
                                 expected_revision=position.revision,
                                 mission="修复并自验缺陷",
                                 capability_refs=["code.fix", "code.review"],
                                 acceptance=["回归通过", "审阅通过"])
    # 改岗位执行模型：Task 稳定 ID、目标、验收、历史事件全部不变
    task = session.get(Task, task.id)
    assert (task.id, task.goal, task.acceptance_json, task.revision, task.assigned_position) == before
    assert [e.id for e in session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task.id))] == events_before
    assert json.loads(session.get(Position, "swe").capability_refs_json) == ["code.fix", "code.review"]
    # 契约更新是版本化事件
    actions = [e.action for e in session.scalars(select(OrganizationEvent))]
    assert "position.contract_updated" in actions

    # 任职替换：原任职退出（保留历史行），接任者按同一岗位新任职
    assignment = session.get(Assignment, "a1")
    org.transition(session, OWNER, org_id="org", kind="assignment", target_id="a1",
                   status="ended", expected_revision=assignment.revision)
    assert session.get(Assignment, "a1").status == "ended"  # 历史保留，不物理删除
    assert org.position_state(session, org_id="org", position_id="swe") == "vacant"
    org.assign(session, OWNER, org_id="org", assignment_id="a7", position_id="swe",
               employee_id="emp2", space_ids={"support"}, actions={"work.execute"}, budget_limit=5)
    assert org.position_state(session, org_id="org", position_id="swe") == "active"
    task = session.get(Task, task.id)
    assert (task.id, task.goal, task.acceptance_json, task.revision, task.assigned_position) == before
    assert [e.id for e in session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task.id))] == events_before
    # 接任者立即取得该岗位在该空间的有效授权上下文
    context = org.resolve_context(session, org_setup.bob, org_id="org", space_id="support",
                                  assignment_id="a7")
    assert context.allowed_actions == frozenset({"work.execute"})


def test_position_contract_update_is_versioned_and_scoped(session, org_setup):
    position = session.get(Position, "swe")
    with pytest.raises(OrganizationError, match="stale_revision"):
        org.update_position_contract(session, OWNER, org_id="org", position_id="swe",
                                     expected_revision=position.revision + 9, mission="x")
    with pytest.raises(OrganizationError, match="organization_management_not_granted"):
        org.update_position_contract(session, org_setup.mallory, org_id="org", position_id="swe",
                                     expected_revision=position.revision, mission="x")
    # 授权上限不能扩到管理者可委派集合之外
    with pytest.raises(OrganizationError, match="grant_exceeds_authorized_ceiling"):
        org.update_position_contract(session, OWNER, org_id="org", position_id="swe",
                                     expected_revision=position.revision,
                                     action_ceiling={"send_as_user"})
    updated = org.update_position_contract(session, OWNER, org_id="org", position_id="swe",
                                           expected_revision=position.revision,
                                           action_ceiling={"work.execute"})
    assert json.loads(updated.action_ceiling_json) == ["work.execute"]


# ── 场景 2：离任/撤权围栏在途 Run；个人边界与组织产物分别继承 ────────────────


def test_offboard_fences_runs_propagates_cancellation_and_reconciles_receipts(session, org_setup):
    task = _delegated_task(session, task_key="k-s2")
    run1, _ = _start_run(session, task, org_setup.alice, "a1", "support", "intent-1")
    claimed = execution.claim(session, run1.id, worker="w-alice",
                              validate_context=_org_validator(org_setup.alice))
    execution.transition(session, claimed.id, worker="w-alice", generation=claimed.generation,
                         state="running", validate_context=_org_validator(org_setup.alice),
                         checkpoint={"done": ["复现"], "next": ["补丁"]})
    run2, _ = _start_run(session, task, org_setup.alice, "a1", "support", "intent-2")

    # 未生效委派：明天才开始的指派
    org.assign(session, OWNER, org_id="org", assignment_id="a-future", position_id="reviewer",
               employee_id="emp1", space_ids={"support"}, actions={"knowledge.read"},
               valid_from=utcnow() + timedelta(days=1))

    member = session.get(Membership, "m1")
    _, event = org.transition(session, OWNER, org_id="org", kind="membership", target_id="m1",
                              status="revoked", expected_revision=member.revision,
                              lifecycle_effects=org.default_offboard_effects)
    # 离任即终止全部任职（含未生效委派）
    assert session.get(Assignment, "a1").status == "ended"
    assert session.get(Assignment, "a2").status == "ended"
    assert session.get(Assignment, "a-future").status == "ended"

    # 在途 Run 不能新增动作：运行中转状态被拒；排队 Run 认领被拒
    with pytest.raises(ValueError, match="authorization_invalid"):
        execution.transition(session, claimed.id, worker="w-alice", generation=claimed.generation,
                             state="waiting_external",
                             validate_context=_org_validator(org_setup.alice))
    with pytest.raises(ValueError, match="authorization_invalid"):
        execution.claim(session, run2.id, worker="w-alice",
                        validate_context=_org_validator(org_setup.alice))

    # 取消传播：任务补充队列出现取消（执行器在安全边界停止）
    pending = supplements.pending(session, task.id)
    cancellations = [row for row in pending if row.event_kind == "cancellation"]
    assert {json.loads(row.payload_json)["run_id"] for row in cancellations} == {run1.id, run2.id}
    assert supplements.has_pending_cancellation(session, task.id)
    detail = json.loads(event.detail_json)
    assert set(detail["fenced_run_ids"]) == {run1.id, run2.id}

    # 在途回执对账：running 有对账单且保持 open；queued 无副作用只取消
    receipts = org.open_receipts(session, org_id="org")
    assert [r.target_id for r in receipts] == [run1.id]
    assert receipts[0].status == "open"
    assert json.loads(receipts[0].detail_json)["state"] == "running"
    # 未知回执不自动写成功：对账必须显式且带证据
    with pytest.raises(OrganizationError, match="receipt_reconcile_note_required"):
        org.resolve_tenure_receipt(session, OWNER, org_id="org",
                                   receipt_id=receipts[0].receipt_id, outcome="reconciled")
    resolved = org.resolve_tenure_receipt(session, OWNER, org_id="org",
                                          receipt_id=receipts[0].receipt_id,
                                          outcome="reconciled", note="补丁已入库，检查点对账一致")
    assert resolved.status == "reconciled"

    # 重新授予后再离任：失败对账也如实保留
    stale_run = session.get(ExecutionRun, run1.id)
    stale_run.lease_until = utcnow() - timedelta(seconds=1)  # 旧租约到期，不占用单活认领
    session.flush()
    org.renew_membership(session, OWNER, org_id="org", membership_id="m1",
                         grants={"work.execute"}, expected_revision=member.revision)
    org.assign(session, OWNER, org_id="org", assignment_id="a4", position_id="swe",
               employee_id="emp1", space_ids={"support"}, actions={"work.execute"}, budget_limit=5)
    run3, _ = _start_run(session, task, org_setup.alice, "a4", "support", "intent-3")
    claimed3 = execution.claim(session, run3.id, worker="w-alice",
                               validate_context=_org_validator(org_setup.alice))
    member = session.get(Membership, "m1")
    org.transition(session, OWNER, org_id="org", kind="membership", target_id="m1",
                   status="revoked", expected_revision=member.revision,
                   lifecycle_effects=org.default_offboard_effects)
    receipts = {r.target_id: r for r in org.open_receipts(session, org_id="org")}
    assert set(receipts) == {run3.id}
    failed = org.resolve_tenure_receipt(session, OWNER, org_id="org",
                                        receipt_id=receipts[run3.id].receipt_id, outcome="failed")
    assert failed.status == "failed"
    assert org.open_receipts(session, org_id="org") == []


def test_successor_gets_no_personal_token_or_private_notes_but_org_artifacts(
        session, org_setup, tmp_path):
    # 前任私有笔记与组织资料分别登记（DE-16 owner_scope 分层）
    knowledge.record_claim(session, org_id="org", space_id="support", owner_scope="alice",
                           subject="部署凭据", predicate="存放位置",
                           value="Alice 个人令牌保险库", source_refs=["alice-private-note"])
    knowledge.record_claim(session, org_id="org", space_id="support", owner_scope="public",
                           subject="发布流程", predicate="步骤",
                           value="构建→回归→发布", source_refs=["org-runbook"])

    task = _delegated_task(session, task_key="k-s2b")
    run1, _ = _start_run(session, task, org_setup.alice, "a1", "support", "intent-1")
    claimed = execution.claim(session, run1.id, worker="w-alice",
                              validate_context=_org_validator(org_setup.alice))
    execution.transition(session, claimed.id, worker="w-alice", generation=claimed.generation,
                         state="running", validate_context=_org_validator(org_setup.alice))
    # 前任的个人发送批准（原治理服务，绑定 grantee）
    gov.approve(session, "founder", org_id="org", target_type="message", target_id="msg-1",
                target_revision=1, payload_hash=sha256_json({"text": "发布通知"}),
                actions={"send_as_user"}, grantee_id="alice")
    # 合法组织产物落在组织输出根，登记为稳定制品
    artifact_path = tmp_path / "org-artifact.md"
    content = "组织产物：发布复盘报告（岗位可继承）"
    artifact_path.write_text(content, encoding="utf-8")
    artifact = Artifact(id="art-1", run_id=run1.id, generation=1, task_revision=task.revision,
                        kind="file", path=str(artifact_path),
                        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(), scope_hash="org")
    session.add(artifact)
    session.flush()

    # 离任前任
    member = session.get(Membership, "m1")
    org.transition(session, OWNER, org_id="org", kind="membership", target_id="m1",
                   status="revoked", expected_revision=member.revision,
                   lifecycle_effects=org.default_offboard_effects)

    # 接任者：新任职（新 EmployeeProfile，独立 runtime 绑定），拿不到个人 token
    org.assign(session, OWNER, org_id="org", assignment_id="a5", position_id="swe",
               employee_id="emp2", space_ids={"support"}, actions={"work.execute"}, budget_limit=5)
    context = org.resolve_context(session, org_setup.bob, org_id="org", space_id="support",
                                  assignment_id="a5")
    body = json.dumps(context.values, ensure_ascii=False)
    assert "alice" not in body and "runner-alice-token" not in body
    # 前任的员工档案原样保留（退出不抹历史），但绑定不迁移给接任者
    assert session.get(EmployeeProfile, "emp1").runtime_binding == "runner-alice-token"
    assert session.get(EmployeeProfile, "emp2").runtime_binding == "runner-bob"
    assert session.scalar(select(Assignment).where(Assignment.assignment_id == "a1")).status == "ended"
    # 旧 Run 认领/续跑被围栏拒绝
    with pytest.raises(ValueError, match="authorization_invalid"):
        execution.transition(session, claimed.id, worker="w-alice", generation=claimed.generation,
                             state="waiting_external",
                             validate_context=_org_validator(org_setup.alice))
    # 接任者拿不到前任私有笔记（owner_scope 裁剪），组织资料可见
    hits = knowledge.search(session, space_id="support", subject="部署凭据",
                            owner_scope="bob")
    assert hits["claims"] == []
    assert knowledge.search(session, space_id="support", subject="部署凭据",
                            owner_scope="bob", include_private=True)["claims"] == []
    org_hits = knowledge.search(session, space_id="support", subject="发布流程",
                                owner_scope="bob")
    assert [c["value"] for c in org_hits["claims"]] == ["构建→回归→发布"]
    # 以别人身份发送：前任的批准不随岗位转移（grantee 绑定）
    with pytest.raises(gov.AuthorizationError, match="decision_grantee_mismatch"):
        gov.validate(session, target_type="message", target_id="msg-1", target_revision=1,
                     payload_hash=sha256_json({"text": "发布通知"}),
                     action="send_as_user", grantee_id="bob")
    # 合法组织产物按稳定 ID 可接手
    inherited = session.get(Artifact, "art-1")
    assert inherited is not None and Path(inherited.path).read_text(encoding="utf-8") == content
    assert inherited.sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
    # 接任者可合法执行：新 Run 以自己的授权上下文建立
    run2, _ = _start_run(session, task, org_setup.bob, "a5", "support", "intent-bob")
    assert run2.state == "queued"


# ── 场景 3：角色名不自动产生以别人身份发送/跨 scope 读取/管理权 ────────────────


def test_role_names_never_grant_management_or_send_or_cross_scope(session, org_setup):
    # 角色名为展示元数据：org-admin/主管不能管理组织
    for call in (
        lambda: org.add_member(session, org_setup.mallory, org_id="org", membership_id="mx",
                               principal_id="eve", tenant_id="tn", app_id="app",
                               grants={"knowledge.read"}),
        lambda: org.transition(session, org_setup.mallory, org_id="org", kind="position",
                               target_id="swe", status="paused",
                               expected_revision=session.get(Position, "swe").revision),
        lambda: org.bind_channel(session, org_setup.mallory, org_id="org", space_id="support",
                                 tenant_id="tn", app_id="app", channel_id="g1"),
        lambda: org.form_task_crew(session, org_setup.mallory, org_id="org", task_id=1,
                                   crew=[{"assignment_id": "a1", "role": "accountable"}]),
    ):
        with pytest.raises(OrganizationError, match="organization_management_not_granted"):
            call()
    # 管理者自己也不能配置"以别人身份发送"的岗位或授权（不在其可委派上限内）
    with pytest.raises(OrganizationError, match="grant_exceeds_authorized_ceiling"):
        org.add_member(session, OWNER, org_id="org", membership_id="mx", principal_id="eve",
                       tenant_id="tn", app_id="app", grants={"send_as_user"})
    with pytest.raises(OrganizationError, match="grant_exceeds_authorized_ceiling"):
        org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="p-spoof",
                            title="代发岗", mission="", actions={"send_as_user"})

    # 跨 scope 读取：mallory 的任职只在 support 空间
    context = org.resolve_context(session, org_setup.mallory, org_id="org", space_id="support",
                                  assignment_id="a6")
    assert context.allowed_actions == frozenset({"knowledge.read"})
    body = json.dumps(context.values)
    assert "org-admin" not in body and "主管" not in body  # 角色名不进入授权上下文
    with pytest.raises(OrganizationError, match="action_not_granted"):
        org.validate_context(session, context, "work.execute")
    with pytest.raises(OrganizationError, match="position_or_scope_inactive"):
        org.resolve_context(session, org_setup.mallory, org_id="org", space_id="verify",
                            assignment_id="a6")
    # 私有笔记按 owner_scope 裁剪，角色名不给读取权
    knowledge.record_claim(session, org_id="org", space_id="support", owner_scope="alice",
                           subject="故障复盘", predicate="根因",
                           value="配置漂移", source_refs=["alice-private"])
    assert knowledge.search(session, space_id="support", subject="故障复盘",
                            owner_scope="mallory")["claims"] == []
    # 以别人身份发送：无批准 → 拒绝；别人的批准 → grantee 不匹配
    with pytest.raises(gov.AuthorizationError, match="no_active_decision"):
        gov.validate(session, target_type="message", target_id="msg-other", target_revision=1,
                     payload_hash=sha256_json({"text": "x"}), action="send_as_user",
                     grantee_id="mallory")
    gov.approve(session, "founder", org_id="org", target_type="message", target_id="msg-3",
                target_revision=1, payload_hash=sha256_json({"text": "y"}),
                actions={"send_as_user"}, grantee_id="alice")
    with pytest.raises(gov.AuthorizationError, match="decision_grantee_mismatch"):
        gov.validate(session, target_type="message", target_id="msg-3", target_revision=1,
                     payload_hash=sha256_json({"text": "y"}), action="send_as_user",
                     grantee_id="mallory")


# ── 场景 4：按能力转交带交接回执；多任职与临时组队合规可用 ────────────────────


def test_capability_handoff_with_receipt_moves_task_without_loss(session, org_setup):
    task = _delegated_task(session, task_key="k-s4")
    run1, _ = _start_run(session, task, org_setup.alice, "a1", "support", "intent-1")
    claimed = execution.claim(session, run1.id, worker="w1",
                              validate_context=_org_validator(org_setup.alice))
    execution.transition(session, claimed.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_org_validator(org_setup.alice))
    before = (task.goal, task.acceptance_json, task.revision)

    # 能力不匹配的转交被拒绝并给原因：docs 岗位没有 code.fix
    with pytest.raises(OrganizationError, match="handoff_capability_gap"):
        org.propose_handoff(session, OWNER, org_id="org", task_id=task.id,
                            from_assignment_id="a1", to_position_id="docs",
                            requested_capability="code.fix", acceptance_revision=task.revision)
    # 合同版本过期拒绝
    with pytest.raises(OrganizationError, match="handoff_stale_revision"):
        org.propose_handoff(session, OWNER, org_id="org", task_id=task.id,
                            from_assignment_id="a1", to_position_id="qa_eng",
                            requested_capability="test.verify", acceptance_revision=task.revision + 1)
    # 目标空间未绑定拒绝（qa_eng 所在 qa 单元未绑定前不可承接该空间任务）
    # qa↔support 已绑定，此处直接合法提案
    handoff = org.propose_handoff(session, OWNER, org_id="org", task_id=task.id,
                                  from_assignment_id="a1", to_position_id="qa_eng",
                                  requested_capability="test.verify", acceptance_revision=task.revision,
                                  artifacts=[{"artifact_id": "art-1", "sha256": "a" * 64}],
                                  unfinished=["复测登录分支"], source_space_ids=["support"])
    assert handoff.status == "proposed"
    with pytest.raises(OrganizationError, match="handoff_already_proposed"):
        org.propose_handoff(session, OWNER, org_id="org", task_id=task.id,
                            from_assignment_id="a1", to_position_id="qa_eng",
                            requested_capability="test.verify", acceptance_revision=task.revision)
    # 拒绝必须给具体缺口原因；接收方本人可拒绝
    with pytest.raises(OrganizationError, match="handoff_rejection_reason_required"):
        org.decide_handoff(session, org_setup.bob, org_id="org", handoff_id=handoff.handoff_id,
                           decision="reject")
    rejected = org.decide_handoff(session, org_setup.bob, org_id="org",
                                  handoff_id=handoff.handoff_id, decision="reject",
                                  reason="本迭代排期已满，缺测试环境窗口")
    assert rejected.status == "rejected" and "排期" in rejected.decision_reason
    # 拒绝后可重新提案并接受（bob 在 qa_eng 有覆盖 support 的任职）
    org.assign(session, OWNER, org_id="org", assignment_id="a5", position_id="qa_eng",
               employee_id="emp2", space_ids={"support"}, actions={"work.execute"}, budget_limit=8)
    handoff2 = org.propose_handoff(session, OWNER, org_id="org", task_id=task.id,
                                   from_assignment_id="a1", to_position_id="qa_eng",
                                   requested_capability="test.verify", acceptance_revision=task.revision,
                                   artifacts=[{"artifact_id": "art-1", "sha256": "a" * 64}],
                                   unfinished=["复测登录分支"], source_space_ids=["support"])
    with pytest.raises(OrganizationError, match="handoff_receiver_not_authorized"):
        org.decide_handoff(session, org_setup.mallory, org_id="org",
                           handoff_id=handoff2.handoff_id, decision="accept")
    accepted = org.decide_handoff(session, org_setup.bob, org_id="org",
                                  handoff_id=handoff2.handoff_id, decision="accept")
    assert accepted.status == "accepted"
    # 责任岗位切换，目标与历史不丢
    task = session.get(Task, task.id)
    assert task.assigned_position == "qa_eng"
    assert (task.goal, task.acceptance_json, task.revision) == before
    events = list(session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)))
    handoff_events = [e for e in events if e.event_type == "position_handoff"]
    assert len(handoff_events) == 1  # 直接转交，不经过任何机械流水线中间岗
    receipt = json.loads(handoff_events[0].evidence_message_ids)
    assert receipt["unfinished"] == ["复测登录分支"]
    # 旧岗位在途 Run：取消传播 + 对账单
    pending = supplements.pending(session, task.id)
    assert any(row.event_kind == "cancellation" and
               json.loads(row.payload_json)["run_id"] == run1.id for row in pending)
    receipts = org.open_receipts(session, org_id="org")
    assert [r.target_id for r in receipts] == [run1.id]
    # 已决转交单不能再次裁决
    with pytest.raises(OrganizationError, match="handoff_not_pending"):
        org.decide_handoff(session, org_setup.bob, org_id="org",
                           handoff_id=handoff2.handoff_id, decision="accept")


def test_multi_tenure_and_ad_hoc_crew_are_legal(session, org_setup):
    # 多任职：alice 同时持有 swe 与 reviewer 两条有效任职
    ctx_swe = org.resolve_context(session, org_setup.alice, org_id="org", space_id="support",
                                  assignment_id="a1")
    ctx_rev = org.resolve_context(session, org_setup.alice, org_id="org", space_id="support",
                                  assignment_id="a2")
    assert ctx_swe.allowed_actions == frozenset({"work.execute"})
    assert ctx_rev.allowed_actions == frozenset({"knowledge.read"})

    task = _delegated_task(session, task_key="k-s4b", position="swe")
    # 临时组队：accountable 必须恰一个且与任务责任岗位一致；执行/审阅分离
    with pytest.raises(OrganizationError, match="crew_review_separation_required"):
        org.form_task_crew(session, OWNER, org_id="org", task_id=task.id,
                           crew=[{"assignment_id": "a1", "role": "accountable"},
                                 {"assignment_id": "a1", "role": "executor"},
                                 {"assignment_id": "a1", "role": "reviewer"}])
    with pytest.raises(OrganizationError, match="crew_accountable_position_mismatch"):
        # bob 在 qa_eng 另有覆盖 support 的任职：岗位与任务责任岗位不一致，不能当 accountable
        org.assign(session, OWNER, org_id="org", assignment_id="a5", position_id="qa_eng",
                   employee_id="emp2", space_ids={"support"}, actions={"work.execute"}, budget_limit=8)
        org.form_task_crew(session, OWNER, org_id="org", task_id=task.id,
                           crew=[{"assignment_id": "a5", "role": "accountable"},
                                 {"assignment_id": "a1", "role": "executor"}])
    crew = org.form_task_crew(session, OWNER, org_id="org", task_id=task.id,
                              crew=[{"assignment_id": "a1", "role": "accountable"},
                                    {"assignment_id": "a1", "role": "executor"},
                                    {"assignment_id": "a2", "role": "reviewer"}])
    assert [row.role for row in crew] == ["accountable", "executor", "reviewer"]
    disbanded = org.disband_task_crew(session, OWNER, org_id="org", task_id=task.id)
    assert all(row.status == "ended" for row in disbanded)
    assert session.scalars(select(TaskCrewMember).where(
        TaskCrewMember.task_id == task.id, TaskCrewMember.status == "active")).all() == []


# ── 生命周期补齐：暂停撤销未生效委派并冻结在途任职 ────────────────────────────


def test_position_pause_revokes_pending_delegations_and_fences_claims(session, org_setup):
    # 暂停前捕获有效授权上下文（稍后证明旧上下文不能再新起 Run）
    stale_ctx = org.resolve_context(session, org_setup.alice, org_id="org", space_id="support",
                                    assignment_id="a1").values
    org.assign(session, OWNER, org_id="org", assignment_id="a-pending", position_id="swe",
               employee_id="emp2", space_ids={"support"}, actions={"work.execute"}, budget_limit=5,
               valid_from=utcnow() + timedelta(days=1))
    position = session.get(Position, "swe")
    org.transition(session, OWNER, org_id="org", kind="position", target_id="swe",
                   status="paused", expected_revision=position.revision,
                   lifecycle_effects=org.default_offboard_effects)
    # 暂停即撤销未生效委派
    assert session.get(Assignment, "a-pending").status == "ended"
    # 在途任职保留行但被冻结：授权上下文立即失效，新认领停止
    assert session.get(Assignment, "a1").status == "active"
    with pytest.raises(OrganizationError, match="position_or_scope_inactive"):
        org.resolve_context(session, org_setup.alice, org_id="org", space_id="support",
                            assignment_id="a1")
    task = _delegated_task(session, task_key="k-pause")
    with pytest.raises(ValueError, match="authorization_invalid"):
        execution.request_run(session, task_id=task.id, context=stale_ctx,
                              actor_id="alice", output_root="out", idempotency_key="intent-paused",
                              validate_context=_org_validator(org_setup.alice))
    # 岗位空缺语义：暂停不是空缺
    assert org.position_state(session, org_id="org", position_id="swe") == "paused"
    # 恢复激活后原任职继续有效（未终止），可重新建立授权上下文
    position = session.get(Position, "swe")
    org.transition(session, OWNER, org_id="org", kind="position", target_id="swe",
                   status="active", expected_revision=position.revision)
    context = org.resolve_context(session, org_setup.alice, org_id="org", space_id="support",
                                  assignment_id="a1")
    assert context.allowed_actions == frozenset({"work.execute"})
