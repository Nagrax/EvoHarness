"""DE-38 前置：组织授权链、委派上限、生命周期级联与版本 CAS。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.clock import utcnow
from evoharness.models import Base
from evoharness.organization import service as org
from evoharness.organization.models import Assignment, Membership, OrganizationEvent
from evoharness.organization.service import AuthenticatedPrincipal, OrganizationError


OWNER = AuthenticatedPrincipal(principal_id="founder", tenant_id="tn", app_id="app",
                               authenticated=True,
                               authorities=frozenset({"organization.bootstrap", "organization.manage",
                                                      "work.execute", "knowledge.read"}))


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


def _principal(pid, grants=()):
    return AuthenticatedPrincipal(principal_id=pid, tenant_id="tn", app_id="app",
                                  authenticated=True, authorities=frozenset(grants))


@pytest.fixture()
def org_setup(session):
    org.bootstrap_organization(session, OWNER, org_id="org", title="EvoHarness",
                                membership_id="m0", budget_limit=100)
    org.create_unit(session, OWNER, org_id="org", unit_id="dev", title="研发")
    org.create_space(session, OWNER, org_id="org", space_id="support", title="支持")
    org.bind_space(session, OWNER, org_id="org", unit_id="dev", space_id="support")
    org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="swe",
                        title="工程师", mission="修复缺陷", actions={"work.execute"}, budget_limit=10)
    org.add_member(session, OWNER, org_id="org", membership_id="m1", principal_id="alice",
                   tenant_id="tn", app_id="app", grants={"work.execute"}, principal_kind="ai_service")
    org.create_employee(session, OWNER, org_id="org", employee_id="emp1", membership_id="m1",
                        display_name="数字员工")
    org.assign(session, OWNER, org_id="org", assignment_id="a1", position_id="swe",
               employee_id="emp1", space_ids={"support"}, actions={"work.execute"}, budget_limit=5)
    return org


def test_bootstrap_rejects_unauthorized_and_duplicate(session):
    with pytest.raises(OrganizationError, match="organization_bootstrap_not_granted"):
        org.bootstrap_organization(session, _principal("stranger"),
                                   org_id="o2", title="x", membership_id="m")
    org.bootstrap_organization(session, OWNER, org_id="o2", title="y", membership_id="m9")
    with pytest.raises(OrganizationError, match="organization_exists"):
        org.bootstrap_organization(session, OWNER, org_id="o2", title="z", membership_id="m10")


def test_grants_cannot_exceed_delegable_ceiling(session, org_setup):
    # 管理者没有可委派的 knowledge.read 之外的越权动作；授予超出自身可委派集合的动作被拒
    with pytest.raises(OrganizationError, match="grant_exceeds_authorized_ceiling"):
        org.add_member(session, OWNER, org_id="org", membership_id="mx", principal_id="mallory",
                       tenant_id="tn", app_id="app", grants={"org.admin"})
    # 岗位动作上限也不能超过管理者可委派集合
    with pytest.raises(OrganizationError, match="grant_exceeds_authorized_ceiling"):
        org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="p2",
                            title="越权岗", mission="", actions={"org.admin"})
    # 预算超组织上限被拒
    with pytest.raises(OrganizationError, match="budget_exceeds_authorized_ceiling"):
        org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="p3",
                            title="贵岗", mission="", actions=set(), budget_limit=999)


def test_resolve_context_intersects_grants_and_budget(session, org_setup):
    actor = _principal("alice")
    context = org.resolve_context(session, actor, org_id="org", space_id="support", assignment_id="a1")
    assert context.allowed_actions == frozenset({"work.execute"})
    # 预算取组织、岗位、任职三者最小值
    assert context.values["budget_limit"] == 5
    # 授权交集校验：未授予的动作被拒
    with pytest.raises(OrganizationError, match="action_not_granted"):
        org.validate_context(session, context, "knowledge.publish")
    fresh = org.validate_context(session, context, "work.execute")
    assert fresh.snapshot() == context.snapshot()


def test_resolve_context_rejects_foreign_actor_and_stale_revision(session, org_setup):
    with pytest.raises(OrganizationError, match="membership_inactive"):
        org.resolve_context(session, _principal("bob"), org_id="org", space_id="support",
                            assignment_id="a1")
    with pytest.raises(OrganizationError, match="stale_assignment_revision"):
        org.resolve_context(session, _principal("alice"), org_id="org", space_id="support",
                            assignment_id="a1", expected_revision=99)


def test_snapshot_validation_detects_tampering(session, org_setup):
    context = org.resolve_context(session, _principal("alice"), org_id="org",
                                   space_id="support", assignment_id="a1")
    snapshot = context.snapshot()
    # 主体不符
    with pytest.raises(OrganizationError, match="context_principal_mismatch"):
        org.validate_snapshot(session, snapshot, _principal("eve"), "work.execute")
    # 篡改后哈希不匹配
    forged = {**snapshot, "allowed_actions": ["work.execute", "org.admin"]}
    with pytest.raises(OrganizationError, match="context_snapshot_hash_mismatch"):
        org.validate_snapshot(session, forged, _principal("alice"), "org.admin")


def test_membership_revocation_cascades_and_never_revives_old_assignments(session, org_setup):
    context = org.resolve_context(session, _principal("alice"), org_id="org",
                                  space_id="support", assignment_id="a1")
    member = session.get(Membership, "m1")
    org.transition(session, OWNER, org_id="org", kind="membership", target_id="m1",
                   status="revoked", expected_revision=member.revision)
    assignment = session.get(Assignment, "a1")
    assert assignment.status == "ended"
    # 撤权后授权上下文立即失效
    with pytest.raises(OrganizationError, match="membership_inactive"):
        org.validate_context(session, context, "work.execute")
    # 重新授予成员资格后，旧的任职不会复活，需要新的明确任职
    org.renew_membership(session, OWNER, org_id="org", membership_id="m1",
                         grants={"work.execute"}, expected_revision=member.revision)
    assert session.get(Assignment, "a1").status == "ended"


def test_unit_pause_blocks_context_and_lifecycle_events_pending(session, org_setup):
    context = org.resolve_context(session, _principal("alice"), org_id="org",
                                  space_id="support", assignment_id="a1")
    applied = []
    org.set_enabled(session, OWNER, org_id="org", kind="unit", target_id="dev", enabled=False,
                    expected_revision=1, lifecycle_effects=lambda s, e: applied.append(e.action))
    assert applied == ["unit.enabled_changed"]
    with pytest.raises(OrganizationError, match="organization_unit_inactive"):
        org.validate_context(session, context, "work.execute")


def test_stale_revision_cas_and_unit_cycle(session, org_setup):
    org.create_unit(session, OWNER, org_id="org", unit_id="qa", title="测试", parent_id="dev")
    unit = session.scalar(select(__import__("evoharness.organization.models", fromlist=["OrganizationUnit"]).OrganizationUnit).where(
        __import__("evoharness.organization.models", fromlist=["OrganizationUnit"]).OrganizationUnit.unit_id == "qa"))
    with pytest.raises(OrganizationError, match="stale_revision"):
        org.move_unit(session, OWNER, org_id="org", unit_id="qa", parent_id=None, expected_revision=99)
    # 单元成环被拒绝：dev -> qa 会形成环
    dev_unit = session.scalar(select(__import__("evoharness.organization.models", fromlist=["OrganizationUnit"]).OrganizationUnit).where(
        __import__("evoharness.organization.models", fromlist=["OrganizationUnit"]).OrganizationUnit.unit_id == "dev"))
    with pytest.raises(OrganizationError, match="organization_unit_cycle"):
        org.move_unit(session, OWNER, org_id="org", unit_id="dev", parent_id="qa",
                      expected_revision=dev_unit.revision)


def test_position_state_and_event_log(session, org_setup):
    assert org.position_state(session, org_id="org", position_id="swe") == "active"
    # 任职过期后岗位显示空缺
    assignment = session.get(Assignment, "a1")
    assignment.valid_until = utcnow() - timedelta(days=1)
    session.flush()
    assert org.position_state(session, org_id="org", position_id="swe") == "vacant"
    actions = [e.action for e in session.scalars(select(OrganizationEvent))]
    assert "organization.created" in actions
    assert "membership.created" in actions
    assert "assignment.created" in actions


def test_channel_binding_requires_explicit_grant(session, org_setup):
    # 绑定渠道需要组织管理者；陌生主体不能替组织绑渠道
    with pytest.raises(OrganizationError, match="membership_inactive"):
        org.bind_channel(session, _principal("eve"), org_id="org", space_id="support",
                         tenant_id="tn", app_id="app", channel_id="group-1")
    # 同一 (tenant, app, channel, thread_scope) 不能重复绑定
    org.bind_channel(session, OWNER, org_id="org", space_id="support",
                     tenant_id="tn", app_id="app", channel_id="group-1",
                     read_grant=True, write_grant=False)
    with pytest.raises(OrganizationError, match="channel_binding_exists"):
        org.bind_channel(session, OWNER, org_id="org", space_id="support",
                         tenant_id="tn", app_id="app", channel_id="group-1")
    # 缺身份键拒绝
    with pytest.raises(OrganizationError, match="channel_identity_required"):
        org.bind_channel(session, OWNER, org_id="org", space_id="support",
                         tenant_id="", app_id="app", channel_id="g")


def test_channel_resolution_never_guesses_space(session, org_setup):
    # 未绑定渠道显式失败，不落默认空间
    with pytest.raises(OrganizationError, match="channel_unbound"):
        org.resolve_space(session, tenant_id="tn", app_id="app", channel_id="group-9")
    org.create_space(session, OWNER, org_id="org", space_id="private", title="私有")
    org.bind_channel(session, OWNER, org_id="org", space_id="support",
                     tenant_id="tn", app_id="app", channel_id="group-1", write_grant=True)
    # 绑定入口本身拒绝同范围重复绑定（歧义在写入时拦截）
    with pytest.raises(OrganizationError, match="channel_binding_exists"):
        org.bind_channel(session, OWNER, org_id="org", space_id="private",
                         tenant_id="tn", app_id="app", channel_id="group-1")
    # 数据库唯一约束兜底在独立 legacy 表测试中验证（避免污染本事务）
    # 话题级精确绑定优先于渠道级；话题授权独立，不继承渠道级 write 授权
    org.bind_channel(session, OWNER, org_id="org", space_id="support",
                     tenant_id="tn", app_id="app", channel_id="group-1", thread_scope="topic-a")
    resolved = org.resolve_space(session, tenant_id="tn", app_id="app",
                                 channel_id="group-1", thread_scope="topic-a")
    assert resolved["space_id"] == "support" and resolved["write_grant"] is False
    # 只有话题绑定时，不带话题的解析不回退到更宽的绑定
    org.bind_channel(session, OWNER, org_id="org", space_id="private",
                     tenant_id="tn", app_id="app", channel_id="group-3", thread_scope="only-topic")
    with pytest.raises(OrganizationError, match="channel_unbound"):
        org.resolve_space(session, tenant_id="tn", app_id="app", channel_id="group-3")
    # 渠道级正常解析：读取与写入授权按绑定记录返回
    resolved = org.resolve_space(session, tenant_id="tn", app_id="app", channel_id="group-1")
    assert resolved["space_id"] == "support" and resolved["write_grant"] is True
    assert resolved["read_grant"] is True


def test_resolve_space_detects_ambiguity_when_unique_guard_absent():
    """无唯一约束的旧表部署：解析层仍必须检测歧义，不猜测空间。"""
    import sqlalchemy as sa
    from evoharness.organization import service as org_service
    from evoharness.organization.models import Organization, BusinessSpace
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=[Organization.__table__, BusinessSpace.__table__])
    # 早期人工建表缺唯一约束；create_all 不重建已存在表，保留现状
    legacy = sa.Table("channel_bindings", sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.String(64)), sa.Column("space_id", sa.String(64)),
        sa.Column("tenant_id", sa.String(64)), sa.Column("app_id", sa.String(64)),
        sa.Column("channel_id", sa.String(64)), sa.Column("thread_scope", sa.String(64)),
        sa.Column("read_grant", sa.Boolean), sa.Column("write_grant", sa.Boolean),
        sa.Column("enabled", sa.Boolean), sa.Column("revision", sa.Integer),
        sa.Column("created_at", sa.DateTime))
    legacy.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        s.add(Organization(org_id="org", title="t", owner_principal_id="p"))
        s.add(BusinessSpace(space_id="s1", org_id="org", title="a"))
        s.add(BusinessSpace(space_id="s2", org_id="org", title="b"))
        s.execute(legacy.insert().values([
            {"org_id": "org", "space_id": "s1", "tenant_id": "tn", "app_id": "app",
             "channel_id": "g", "thread_scope": "", "read_grant": True, "write_grant": False,
             "enabled": True, "revision": 1, "created_at": utcnow()},
            {"org_id": "org", "space_id": "s2", "tenant_id": "tn", "app_id": "app",
             "channel_id": "g", "thread_scope": "", "read_grant": True, "write_grant": False,
             "enabled": True, "revision": 1, "created_at": utcnow()},
        ]))
        with pytest.raises(OrganizationError, match="channel_binding_ambiguous"):
            org_service.resolve_space(s, tenant_id="tn", app_id="app", channel_id="g")
    engine.dispose()


def test_channel_grants_are_separate_and_space_pause_blocks(session, org_setup):
    org.bind_channel(session, OWNER, org_id="org", space_id="support",
                     tenant_id="tn", app_id="app", channel_id="group-2",
                     read_grant=True, write_grant=False)
    resolved = org.resolve_space(session, tenant_id="tn", app_id="app", channel_id="group-2")
    assert resolved["read_grant"] is True and resolved["write_grant"] is False
    # 空间停用后解析失败：绑定存在也不放行
    org.set_enabled(session, OWNER, org_id="org", kind="space", target_id="support",
                    enabled=False, expected_revision=1)
    with pytest.raises(OrganizationError, match="space_paused_or_missing"):
        org.resolve_space(session, tenant_id="tn", app_id="app", channel_id="group-2")
