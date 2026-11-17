"""DE-42 回归：岗位能力证据与组织知识学习闭环。

覆盖 tasks.json DE-42 的 3 条验收场景 + 学习闭环只读边界（全部离线：
内存 SQLite、fake 授权端口、tmp_path 沙箱，无网络、无外部服务）：

场景 1：一个岗位验证的能力迁到另一个范围需重新核条件和回归，不因 healthy
        自动跨范围授权；没有证据的能力显式标待验证（不猜测）。
场景 2：调度器不把候选（未认证证据）或无证据能力当合格接单资格
        （qualified 拒绝带原因）；投影更新不覆盖已采用的独立 SOP。
场景 3：岗位更换仍可读取合法组织经验，个人私聊/凭据不被沉淀成全组织资产；
        跨单元复用取来源授权交集（不扩大受众）。
场景 4：评分器/组织政策/能力证据行对优化路径只读——改写尝试被明确拒绝并写
        permission-denied 审计事件。

接线回归：agents/online_skill_evolution.py（在线反馈归因）、agents/skill_tiers.py
（晋级 evidence 归因，仍只走原 champion 门，不建第二链）、agents/memory.py
（记忆归因 frontmatter，个人记忆不带 org-* 键）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from agents import memory as memory_mod
from agents import online_skill_evolution as ose
from agents import skill_tiers
from agents.skills import create_skill, reset_skill_cache
from evoharness.governance import budget as budget_svc
from evoharness.knowledge import service as knowledge
from evoharness.models import Base, Task, TaskStatus
from evoharness.organization import capabilities as caps
from evoharness.organization import service as org
from evoharness.organization.capabilities import CapabilityEvidence
from evoharness.organization.models import Assignment, Membership, OrganizationEvent, Position
from evoharness.organization.service import AuthenticatedPrincipal, OrganizationError
from evoharness.work import scheduling
from evoharness.work import service as work_service

OWNER = AuthenticatedPrincipal(principal_id="founder", tenant_id="tn", app_id="app",
                               authenticated=True,
                               authorities=frozenset({"organization.bootstrap", "organization.manage",
                                                      "work.execute", "knowledge.read"}))


def _principal(pid: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(principal_id=pid, tenant_id="tn", app_id="app",
                                  authenticated=True, authorities=frozenset())


def _org_validator(principal: AuthenticatedPrincipal):
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
    """单组织两单元：swe（dev/support，声明能力 code.fix）与 qa_eng（qa/verify+support，
    声明能力 test.verify）。mallory 只有角色名没有管理授权。"""
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
    org.create_position(session, OWNER, org_id="org", unit_id="qa", position_id="qa_eng",
                        title="测试工程师", mission="验证交付", actions={"work.execute"},
                        budget_limit=8, capability_refs=["test.verify"])
    org.add_member(session, OWNER, org_id="org", membership_id="m1", principal_id="alice",
                   tenant_id="tn", app_id="app", grants={"work.execute", "knowledge.read"},
                   principal_kind="ai_service")
    org.add_member(session, OWNER, org_id="org", membership_id="m2", principal_id="bob",
                   tenant_id="tn", app_id="app", grants={"work.execute", "knowledge.read"},
                   principal_kind="human")
    org.add_member(session, OWNER, org_id="org", membership_id="m3", principal_id="mallory",
                   tenant_id="tn", app_id="app", grants={"knowledge.read"},
                   roles=["org-admin", "主管"], delegable_actions={"knowledge.read"})
    org.create_employee(session, OWNER, org_id="org", employee_id="emp1",
                        membership_id="m1", display_name="数字员工A")
    org.create_employee(session, OWNER, org_id="org", employee_id="emp2",
                        membership_id="m2", display_name="员工B")
    org.create_employee(session, OWNER, org_id="org", employee_id="emp3",
                        membership_id="m3", display_name="观察员")
    org.assign(session, OWNER, org_id="org", assignment_id="a1", position_id="swe",
               employee_id="emp1", space_ids={"support"}, actions={"work.execute"}, budget_limit=5)
    org.assign(session, OWNER, org_id="org", assignment_id="a3", position_id="qa_eng",
               employee_id="emp2", space_ids={"verify"}, actions={"work.execute"}, budget_limit=8)
    budget_svc.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=100000)
    budget_svc.set_limit(session, scope_kind="space", scope_id="support", limit_tokens=50000)
    budget_svc.set_limit(session, scope_kind="space", scope_id="verify", limit_tokens=50000)
    return SimpleNamespace(alice=_principal("alice"), bob=_principal("bob"),
                           mallory=_principal("mallory"))


def _context_factory(principals: dict):
    def factory(session, task, executor):
        member = session.get(Membership, executor["membership_id"])
        principal = principals[member.principal_id]
        context = org.resolve_context(session, principal, org_id="org",
                                      space_id=task.space_id,
                                      assignment_id=executor["assignment_id"]).values
        return context, _org_validator(principal)

    return factory


def _delegated_task(session, *, task_key: str, space_id: str = "support",
                    capability: str = "code.fix", position: str = "swe") -> Task:
    acceptance = json.dumps([{"criterion": "回归通过", "capability": capability}])
    task = Task(task_key=task_key, tenant="tn", owner="founder", chat_id="chat",
                title=capability[:20], action=capability, status=TaskStatus.OPEN,
                workflow_kind="delegated_delivery", goal=f"用 {capability} 交付",
                acceptance_json=acceptance, org_id="org", space_id=space_id,
                assigned_position=position)
    session.add(task)
    session.flush()
    work_service.transition_to(session, task.id, to_state="queued", actor="founder")
    session.flush()
    return task


def _verified_evidence(session, *, position_id: str = "swe", capability: str = "code.fix",
                       space_id: str = "support", kind: str = caps.EVIDENCE_KIND_SKILL,
                       skill_name: str = "fix-login", objective: str = "",
                       task_id: int = 0, regression: str = "eval-report-1"):
    ev = caps.record_evidence(session, OWNER, org_id="org", position_id=position_id,
                              capability=capability, scope_space_ids={space_id}, kind=kind,
                              skill_name=skill_name, artifact_hash="a" * 64,
                              eval_protocol="eval/v1", tool_env="dev-env",
                              source_objective_id=objective, source_task_id=task_id)
    caps.verify_evidence(session, OWNER, org_id="org", evidence_id=ev.evidence_id,
                         regression_refs=[regression])
    return ev


# ── 场景 1：跨范围迁移需重新核条件与回归；无证据能力显式待验证 ────────────────


def test_capability_scope_migration_requires_reverification(session, org_setup):
    # 没有证据的能力：显式待验证，不给合格资格（不猜测）
    status = caps.capability_status(session, org_id="org", position_id="swe",
                                    capability="code.fix", space_id="support")
    assert status == {"qualified": False, "state": "pending_verification",
                      "reason": "evidence_missing", "evidence_id": ""}
    # 未在岗位声明的能力：capability_not_declared
    undeclared = caps.capability_status(session, org_id="org", position_id="swe",
                                        capability="quantum.leap", space_id="support")
    assert undeclared["qualified"] is False and undeclared["reason"] == "capability_not_declared"
    # 证据是确定性管理对象：非管理者不能登记/核验
    with pytest.raises(OrganizationError, match="organization_management_not_granted"):
        caps.record_evidence(session, org_setup.mallory, org_id="org", position_id="swe",
                             capability="code.fix", scope_space_ids={"support"})
    ev = caps.record_evidence(session, OWNER, org_id="org", position_id="swe",
                              capability="code.fix", scope_space_ids={"support"},
                              skill_name="fix-login", artifact_hash="a" * 64,
                              eval_protocol="eval/v1", tool_env="dev-env",
                              source_objective_id="obj-1", source_task_id=0)
    assert ev.state == "pending" and ev.source_objective_id == "obj-1"  # 目标归因随证据落库
    # 未核验证据（候选形态）不是合格资格
    pending = caps.capability_status(session, org_id="org", position_id="swe",
                                     capability="code.fix", space_id="support")
    assert pending["qualified"] is False and pending["reason"] == "evidence_pending_verification"
    # 未声明范围/重复范围被拒绝
    with pytest.raises(caps.CapabilityError, match="evidence_scope_required"):
        caps.record_evidence(session, OWNER, org_id="org", position_id="swe",
                             capability="code.fix", scope_space_ids=set())
    with pytest.raises(caps.CapabilityError, match="evidence_duplicate_scope"):
        caps.record_evidence(session, OWNER, org_id="org", position_id="swe",
                             capability="code.fix", scope_space_ids={"support"},
                             artifact_hash="a" * 64, eval_protocol="eval/v1", tool_env="dev-env")
    # 绑定不全不能核验（缺 tool_env）
    partial = caps.record_evidence(session, OWNER, org_id="org", position_id="qa_eng",
                                   capability="test.verify", scope_space_ids={"verify"},
                                   artifact_hash="b" * 64, eval_protocol="eval/v1", tool_env="")
    with pytest.raises(caps.CapabilityError, match="evidence_binding_incomplete"):
        caps.verify_evidence(session, OWNER, org_id="org", evidence_id=partial.evidence_id,
                             regression_refs=["rr-1"])
    # 核验必须带真实回归证据
    with pytest.raises(caps.CapabilityError, match="evidence_regression_required"):
        caps.verify_evidence(session, OWNER, org_id="org", evidence_id=ev.evidence_id)
    caps.verify_evidence(session, OWNER, org_id="org", evidence_id=ev.evidence_id,
                         regression_refs=["eval-report-1"])
    assert caps.capability_status(session, org_id="org", position_id="swe",
                                  capability="code.fix", space_id="support")["qualified"] is True

    # 跨范围迁移：扩到 verify → 生成 pending 派生证据；原范围 verified 不自动授权新范围
    derived = caps.extend_scope(session, OWNER, org_id="org", evidence_id=ev.evidence_id,
                                extra_space_ids={"verify"})
    assert derived.state == "pending" and derived.derived_from == ev.evidence_id
    assert derived.scope() == ["verify"]
    migrated = caps.capability_status(session, org_id="org", position_id="swe",
                                      capability="code.fix", space_id="verify")
    assert migrated["qualified"] is False and migrated["reason"] == "evidence_pending_verification"
    # 原范围不受影响
    original = caps.capability_status(session, org_id="org", position_id="swe",
                                      capability="code.fix", space_id="support")
    assert original["qualified"] is True and original["evidence_id"] == ev.evidence_id
    # 新范围必须重新核条件并回归才取得资格
    with pytest.raises(caps.CapabilityError, match="evidence_regression_required"):
        caps.verify_evidence(session, OWNER, org_id="org", evidence_id=derived.evidence_id)
    caps.verify_evidence(session, OWNER, org_id="org", evidence_id=derived.evidence_id,
                         regression_refs=["eval-report-verify-1"])
    assert caps.capability_status(session, org_id="org", position_id="swe",
                                  capability="code.fix", space_id="verify")["qualified"] is True
    # 撤回立即失去资格；原因必填；历史行保留
    with pytest.raises(caps.CapabilityError, match="revoke_reason_required"):
        caps.revoke_evidence(session, OWNER, org_id="org", evidence_id=ev.evidence_id, reason="")
    caps.revoke_evidence(session, OWNER, org_id="org", evidence_id=ev.evidence_id,
                         reason="制品已废弃")
    assert session.get(CapabilityEvidence, ev.evidence_id).state == "revoked"
    revoked = caps.capability_status(session, org_id="org", position_id="swe",
                                     capability="code.fix", space_id="support")
    assert revoked["qualified"] is False and revoked["reason"] == "evidence_revoked"
    # 概览：无证据能力显式列待验证
    overview = caps.position_capability_overview(session, org_id="org", position_id="qa_eng")
    assert overview["capabilities"][0]["capability"] == "test.verify"
    assert overview["capabilities"][0]["pending_verification"] is True
    # 核验/登记只写 capability.* 组织事件，不写任何晋级链状态
    actions = [event.action for event in session.scalars(select(OrganizationEvent))]
    assert "capability.evidence_verified" in actions and "capability.evidence_revoked" in actions


# ── 场景 2：调度器不把候选/无证据能力当合格资格；投影不覆盖已采用独立 SOP ──────


def test_scheduler_rejects_candidates_and_evidence_less_capabilities(session, org_setup, monkeypatch):
    monkeypatch.setenv(caps.GATE_ENV, "1")
    principals = {"alice": org_setup.alice, "bob": org_setup.bob}
    task = _delegated_task(session, task_key="k-gate", capability="code.fix")
    # 无证据能力：调度器不给合格资格（证据缺失 → 不合格）
    assert scheduling.qualified_assignments(session, task) == []
    report = scheduling.schedule_pending(session, org_id="org",
                                         context_factory=_context_factory(principals))
    wait = next((e for e in report["waiting"] if e["task_key"] == "k-gate"), None)
    assert wait is not None and wait["reason"] == "no_qualified_position"
    # 等待负责人澄清后重新排队（与 DE-40 同一入口）
    work_service.resolve_clarification(session, task.id, actor="founder", answer_message_id="m-1")
    work_service.transition_to(session, task.id, to_state="ready", actor="founder")
    work_service.transition_to(session, task.id, to_state="queued", actor="founder")

    # 搜索池/未认证候选形态：登记了 pending 证据仍不当合格资格
    candidate = caps.record_evidence(session, OWNER, org_id="org", position_id="swe",
                                     capability="code.fix", scope_space_ids={"support"},
                                     skill_name="candidate-sop", artifact_hash="d" * 64,
                                     eval_protocol="eval/v1", tool_env="dev-env",
                                     source_objective_id="obj-1", source_task_id=task.id)
    assert scheduling.qualified_assignments(session, task) == []
    status = caps.capability_status(session, org_id="org", position_id="swe",
                                    capability="code.fix", space_id="support")
    assert status["reason"] == "evidence_pending_verification"
    report2 = scheduling.schedule_pending(session, org_id="org",
                                          context_factory=_context_factory(principals))
    wait2 = next((e for e in report2["waiting"] if e["task_key"] == "k-gate"), None)
    assert wait2 is not None and wait2["reason"] == "no_qualified_position"

    # 组织核条件+回归核验后：同一证据成为合格资格，调度放行（不新建资格链）
    work_service.resolve_clarification(session, task.id, actor="founder", answer_message_id="m-2")
    work_service.transition_to(session, task.id, to_state="ready", actor="founder")
    work_service.transition_to(session, task.id, to_state="queued", actor="founder")
    caps.verify_evidence(session, OWNER, org_id="org", evidence_id=candidate.evidence_id,
                         regression_refs=["eval-report-1"])
    qualified = scheduling.qualified_assignments(session, task)
    assert [q["assignment_id"] for q in qualified] == ["a1"]
    report3 = scheduling.schedule_pending(session, org_id="org",
                                          context_factory=_context_factory(principals))
    entry = next((e for e in report3["scheduled"] if e["task_key"] == "k-gate"), None)
    assert entry is not None and entry["executor_assignment"] == "a1"

    # 范围不符也不合格：任务在 verify 空间，swe 证据只覆盖 support（且 dev 单元未绑 verify）
    other = _delegated_task(session, task_key="k-gate-verify", space_id="verify", capability="code.fix")
    assert scheduling.qualified_assignments(session, other) == []
    assert caps.capability_status(session, org_id="org", position_id="swe",
                                  capability="code.fix", space_id="verify")["qualified"] is False


def test_gate_default_off_keeps_existing_qualified_behavior(session, org_setup, monkeypatch):
    # 默认 off：DE-40 既有行为不变（岗位声明能力即覆盖），开关按组织灰度启用
    monkeypatch.delenv(caps.GATE_ENV, raising=False)
    assert caps.evidence_gate_enabled() is False
    task = _delegated_task(session, task_key="k-legacy", capability="code.fix")
    assert [q["assignment_id"] for q in scheduling.qualified_assignments(session, task)] == ["a1"]


def test_projection_update_never_overwrites_adopted_sop(session, org_setup):
    # 存量未登记文件：保持原投影写入者行为（unmanaged 门在原链）
    assert caps.projection_update_guard(session, org_id="org", skill_name="legacy-sop") == {
        "allowed": True, "reason": "unmanaged_legacy", "evidence_id": ""}
    # 管理者登记已采用的独立 SOP 证据并核验（制品 hash 绑定）
    adopted = caps.record_evidence(session, OWNER, org_id="org", position_id="swe",
                                   capability="code.fix", scope_space_ids={"support"},
                                   kind=caps.EVIDENCE_KIND_SOP, skill_name="deploy-runbook",
                                   artifact_hash="c" * 64, eval_protocol="sop-accept/v1",
                                   tool_env="ops-env")
    caps.verify_evidence(session, OWNER, org_id="org", evidence_id=adopted.evidence_id,
                         regression_refs=["sop-acceptance-1"])
    # 重建式投影（producer-kind=projection / managed-by=experience.skill_sync）：允许
    for kwargs in ({"producer_kind": "projection"}, {"managed_by": "experience.skill_sync"}):
        guard = caps.projection_update_guard(session, org_id="org", skill_name="whatever", **kwargs)
        assert guard["allowed"] is True and guard["reason"] == "projection_source_owned_rebuild"
    # 已采用的独立 SOP：按名称或制品 hash 命中 → 拒绝并给原因与证据引用
    by_name = caps.projection_update_guard(session, org_id="org", skill_name="deploy-runbook")
    assert by_name["allowed"] is False and by_name["reason"] == "adopted_sop_source_owned"
    assert by_name["evidence_id"] == adopted.evidence_id
    by_hash = caps.projection_update_guard(session, org_id="org", skill_name="other",
                                           artifact_hash="c" * 64)
    assert by_hash["allowed"] is False and by_hash["reason"] == "adopted_sop_source_owned"
    # 拒绝是纯判定：证据行未被改写（正文走责任人原写入链）
    assert session.get(CapabilityEvidence, adopted.evidence_id).state == "verified"


# ── 场景 3：岗位更换仍可读合法组织经验；个人资产不进组织；跨单元取授权交集 ─────


def test_position_knowledge_view_survives_tenure_change_without_personal_assets(session, org_setup):
    knowledge.record_claim(session, org_id="org", space_id="support", owner_scope="public",
                           subject="发布流程", predicate="步骤", value="构建→回归→发布",
                           source_refs=["org-runbook"])
    # alice 在同一主题上有个人私聊沉淀（个人来源，不是组织资产）
    knowledge.record_claim(session, org_id="org", space_id="support", owner_scope="alice",
                           subject="发布流程", predicate="个人备注", value="Alice 的个人操作偏好",
                           source_refs=["alice-private-note"])
    knowledge.record_claim(session, org_id="org", space_id="support", owner_scope="alice",
                           subject="发布凭据", predicate="存放位置", value="Alice 个人令牌保险库",
                           source_refs=["alice-private-note"])
    knowledge.record_claim(session, org_id="org", space_id="verify", owner_scope="public",
                           subject="发布流程", predicate="步骤", value="构建→回归→发布→观察",
                           source_refs=["verify-runbook"])
    # 在任主体：组织经验 + 本人私有可见；verify 空间不进 dev 岗位视图
    view_alice = caps.position_knowledge_view(session, org_id="org", position_id="swe",
                                              principal_id="alice", subject="发布流程")
    assert view_alice["spaces"] == ["support"]
    owners = {(c["space_id"], c["owner_scope"]) for c in view_alice["claims"]}
    assert ("support", "public") in owners and ("support", "alice") in owners
    assert all(c["space_id"] == "support" for c in view_alice["claims"])
    # 岗位更换：alice 离任，bob 接任同一岗位
    assignment = session.get(Assignment, "a1")
    org.transition(session, OWNER, org_id="org", kind="assignment", target_id="a1",
                   status="ended", expected_revision=assignment.revision)
    org.assign(session, OWNER, org_id="org", assignment_id="a2b", position_id="swe",
               employee_id="emp2", space_ids={"support"}, actions={"work.execute"}, budget_limit=5)
    view_bob = caps.position_knowledge_view(session, org_id="org", position_id="swe",
                                            principal_id="bob", subject="发布流程")
    pairs = {(c["owner_scope"], c["value"]) for c in view_bob["claims"]}
    assert ("public", "构建→回归→发布") in pairs  # 合法组织经验随岗位可读
    assert all(c["owner_scope"] in {"", "public", "bob"} for c in view_bob["claims"])
    assert all(c["owner_scope"] != "alice" for c in view_bob["claims"])
    # 个人私聊/凭据不被沉淀成全组织资产：接任者读不到前任私有凭据
    cred = caps.position_knowledge_view(session, org_id="org", position_id="swe",
                                        principal_id="bob", subject="发布凭据")
    assert cred["claims"] == []


def test_cross_unit_reuse_takes_source_authorization_intersection(session, org_setup):
    knowledge.record_claim(session, org_id="org", space_id="verify", owner_scope="public",
                           subject="压测口径", predicate="阈值", value="P95<800ms",
                           source_refs=["qa-doc"])
    knowledge.record_claim(session, org_id="org", space_id="support", owner_scope="public",
                           subject="压测口径", predicate="阈值", value="支持空间口径",
                           source_refs=["support-doc"])
    # dev 单元只绑定 support：来源授权（support+verify）∩ 目标读边界（support）
    reuse_dev = caps.cross_unit_reuse_view(session, org_id="org", target_position_id="swe",
                                           subject="压测口径")
    assert reuse_dev["source_spaces"] == ["support", "verify"]
    assert reuse_dev["target_spaces"] == ["support"]
    assert reuse_dev["authorized_spaces"] == ["support"]
    assert reuse_dev["excluded_spaces"] == ["verify"]  # 不扩大受众：verify 来源不进 dev
    assert [c["space_id"] for c in reuse_dev["claims"]] == ["support"]
    # qa 单元绑定 verify+support：交集为两者
    reuse_qa = caps.cross_unit_reuse_view(session, org_id="org", target_position_id="qa_eng",
                                          subject="压测口径")
    assert reuse_qa["authorized_spaces"] == ["support", "verify"]
    assert {c["space_id"] for c in reuse_qa["claims"]} == {"support", "verify"}
    # 个人来源不进入跨单元复用（只复用组织来源）
    knowledge.record_claim(session, org_id="org", space_id="verify", owner_scope="bob",
                           subject="压测口径", predicate="备注", value="bob 私有备注",
                           source_refs=["bob-private"])
    reuse_qa2 = caps.cross_unit_reuse_view(session, org_id="org", target_position_id="swe",
                                           subject="压测口径")
    assert all(c["owner_scope"] in {"", "public"} for c in reuse_qa2["claims"])


# ── 场景 4：评分器/组织政策/能力证据对优化路径只读，拒绝写 permission-denied 审计 ─


def test_optimizer_cannot_rewrite_scorer_or_policy(session, org_setup):
    readonly_kinds = ("scorer", "org_policy", "eval_protocol", "capability_evidence",
                      "position_authorization", "business_rule")
    for kind in readonly_kinds:
        with pytest.raises(caps.CapabilityError, match="optimization_target_read_only"):
            caps.request_optimizer_write(session, org_id="org", actor="optimizer:search",
                                         target_kind=kind, target_id="target-1",
                                         intent="改评分阈值/政策给自己开门")
    # 失败尝试逐条落 permission-denied 审计事件（不静默）
    events = list(session.scalars(select(OrganizationEvent).where(
        OrganizationEvent.action == "capability.optimizer_write_denied")))
    assert len(events) == len(readonly_kinds)
    for event in events:
        detail = json.loads(event.detail_json)
        assert detail["outcome"] == "permission_denied" and detail["target_kind"] in readonly_kinds
    # 非只读目标返回 allowed：继续走原服务（skill_evolution/knowledge.service），
    # 不在本模块开第二写入面
    assert caps.request_optimizer_write(session, org_id="org", actor="optimizer",
                                        target_kind="skill_content")["allowed"] is True
    assert caps.request_optimizer_write(session, org_id="org", actor="optimizer",
                                        target_kind="knowledge_candidate")["allowed"] is True


def test_attribution_helpers_contract(session):
    # 归因维度契约：空值剔除；attach 不覆盖已有键；与 normalize 同键
    assert caps.capability_attribution(position_id="swe", objective_id="obj-1") == {
        "position_id": "swe", "objective_id": "obj-1"}
    assert caps.normalize_attribution({"position_id": "swe", "task_id": 7}) == {"position_id": "swe", "task_id": 7}
    assert caps.normalize_attribution("not-a-dict") == {}
    payload = caps.attach_attribution({}, {"position_id": "swe"})
    assert payload["capability_attribution"] == {"position_id": "swe"}
    assert caps.attach_attribution({"capability_attribution": {"position_id": "x"}}, {"position_id": "swe"}) == {
        "capability_attribution": {"position_id": "x"}}
    assert caps.attach_attribution({}, None) == {}


# ── agents 三文件最小接线回归 ─────────────────────────────────────────────────


def _extract_then_discard():
    calls = {"n": 0}

    async def side_query(system: str, user: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"skills": [{
                "name": "doc-update", "description": "文档更新顺序",
                "instructions": "先分析现有文档结构再改写", "evidence": "用户明确要求"}]},
                ensure_ascii=False)
        return json.dumps({"action": "discard", "reason": "duplicate"}, ensure_ascii=False)

    return side_query


def test_online_ingest_carries_capability_attribution(tmp_path, monkeypatch):
    # 在线反馈回原沉淀链，归因（岗位/目标引用）随 result/provenance 落原链路留痕
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    messages = [{"role": "user", "content": "以后文档更新都要先分析结构再改写"},
                {"role": "assistant", "content": "明白"}]
    attribution = {"position_id": "swe", "objective_id": "obj-1", "task_id": 7, "evidence_id": "ev-1"}
    result = asyncio.run(ose.online_ingest(messages=messages, side_query=_extract_then_discard(),
                                           attribution=attribution))
    assert result["action"] == "discard"
    assert result["capability_attribution"] == attribution
    # provenance 行（原链路留痕）携带归因
    provenance = tmp_path / ".evoharness" / "skill-evolution" / "online_provenance.jsonl"
    rows = [json.loads(line) for line in provenance.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[-1]["result"]["capability_attribution"] == attribution
    # 无归因调用零注入：result 不出现归因键（旧调用逐字节兼容）
    result2 = asyncio.run(ose.online_ingest(messages=messages, side_query=_extract_then_discard()))
    assert "capability_attribution" not in result2
    # 键契约与 evoharness.organization.capabilities 归一化一致
    assert ose._attach_attribution({}, attribution)["capability_attribution"] == caps.normalize_attribution(attribution)


def test_promotion_evidence_carries_capability_attribution(tmp_path, monkeypatch):
    # 晋级仍只走原评测晋级链（healthy + champion 门），归因只是新增维度
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    reset_skill_cache()
    skill_tiers.reset_tier_cache()
    try:
        create_skill(name="cap-skill", description="d", instructions="i")
        report = {
            "generated_at": "2026-11-14T00:00:00Z", "thresholds": {},
            "skills": [{
                "skill": "cap-skill", "status": "healthy", "reasons": [],
                "retrieved": 5, "relevant": 3, "used": 2, "used_rate": 0.4,
                "relevance_rate": 0.6,
                "replay": {"count": 3, "promotion_test": 1},
                "eval": {"pass_rate": 0.9, "hard_failures": 0},
                "capability_attribution": {"position_id": "swe", "objective_id": "obj-1",
                                           "evidence_id": "ev-1"},
                "artifacts": {"promotion": {"promoted": True, "status": "active_champion"}},
            }],
        }
        out = skill_tiers.apply_eval_promotions(report)
        assert out["promoted"] and skill_tiers.get_tier("cap-skill") == "staging"
        audit_path = skill_tiers.tier_audit_path()
        rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        tier_rows = [row for row in rows if row.get("event") == "tier_change"]
        assert tier_rows[-1]["trigger"] == "auto_evidence"
        assert tier_rows[-1]["evidence"]["capability_attribution"] == {
            "position_id": "swe", "objective_id": "obj-1", "evidence_id": "ev-1"}
        # 未携带归因的报告：审计行不出现该键（旧报告逐字节兼容）
        create_skill(name="cap-skill-2", description="d", instructions="i")
        report2 = json.loads(json.dumps(report))
        report2["skills"][0]["skill"] = "cap-skill-2"
        report2["skills"][0].pop("capability_attribution")
        out2 = skill_tiers.apply_eval_promotions(report2)
        assert out2["promoted"]
        rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        tier_rows = [row for row in rows if row.get("event") == "tier_change"]
        assert "capability_attribution" not in tier_rows[-1]["evidence"]
        # 归因不是第二晋级链：非 healthy 不晋升，与归因无关
        create_skill(name="cap-skill-3", description="d", instructions="i")
        report3 = json.loads(json.dumps(report))
        report3["skills"][0]["skill"] = "cap-skill-3"
        report3["skills"][0]["status"] = "watch"
        out3 = skill_tiers.apply_eval_promotions(report3)
        assert out3["promoted"] == [] and skill_tiers.get_tier("cap-skill-3") == "experimental"
        assert skill_tiers.capability_attribution(position_id="swe", objective_id="obj-1") == {
            "position_id": "swe", "objective_id": "obj-1"}
    finally:
        reset_skill_cache()
        skill_tiers.reset_tier_cache()


def test_memory_attribution_frontmatter(tmp_path, monkeypatch):
    # 组织事件归因写 frontmatter；个人记忆不带 org-* 键（不被沉淀成组织资产）
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    filename = memory_mod.save_memory("发布顺序", "岗位沉淀", "feedback", "先分析再改写",
                                      attribution={"position_id": "swe", "objective_id": "obj-9",
                                                   "evidence_id": "ev-1"})
    text = (memory_mod.get_memory_dir() / filename).read_text(encoding="utf-8")
    assert "org-position-id: swe" in text
    assert "org-objective-id: obj-9" in text
    assert "org-evidence-id: ev-1" in text
    # 无归因的个人记忆：frontmatter 无任何 org-* 键
    personal = memory_mod.save_memory("个人偏好", "d", "user", "内容")
    personal_text = (memory_mod.get_memory_dir() / personal).read_text(encoding="utf-8")
    assert "org-" not in personal_text
    # 索引与召回读取不受新增键影响
    entries = {entry.name: entry for entry in memory_mod.list_memories()}
    assert {"发布顺序", "个人偏好"} <= set(entries)
