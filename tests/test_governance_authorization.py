"""DE-12：统一决定验证、撤权联动与发送身份冻结。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.clock import utcnow
from evoharness.governance import authorization as auth
from evoharness.governance.authorization import AuthorizationError
from evoharness.ids import sha256_json
from evoharness.models import Base, Outbox, OutboxStatus
from evoharness.outbox import enqueue


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


def _approve(session, **overrides):
    payload = {"body": "答案正文", "chat": "c1"}
    values = dict(actor_id="owner-1", org_id="org", target_type="draft",
                  target_id="d1", target_revision=2,
                  payload_hash=sha256_json(payload),
                  actions={"send_as_user"}, grantee_id="emp1")
    values.update(overrides)
    return auth.approve(session, values.pop("actor_id"), **values), payload


def test_approve_supersedes_previous_decision(session):
    first, payload = _approve(session)
    second, _ = _approve(session, actions={"send_as_user", "share_knowledge"})
    session.refresh(first)
    assert first.superseded_by == second.decision_id
    # 旧批准不再可用；新批准的动作集合生效（R02：validate 必须携带批准绑定的 grantee）
    ok = auth.validate(session, target_type="draft", target_id="d1", target_revision=2,
                       payload_hash=sha256_json(payload), action="share_knowledge",
                       grantee_id="emp1")
    assert ok.decision_id == second.decision_id
    assert first.decision_id != second.decision_id


def test_independent_grantees_can_each_use_their_own_decision(session):
    first, payload = _approve(session, grantee_id="alice")
    second, _ = _approve(session, grantee_id="bob")
    for grantee, expected in (("bob", second), ("alice", first)):
        decision = auth.validate(
            session, org_id="org", target_type="draft", target_id="d1",
            target_revision=2, payload_hash=sha256_json(payload),
            action="send_as_user", grantee_id=grantee)
        assert decision.decision_id == expected.decision_id
    with pytest.raises(AuthorizationError, match="decision_grantee_mismatch"):
        auth.validate(session, org_id="org", target_type="draft", target_id="d1",
                      target_revision=2, payload_hash=sha256_json(payload),
                      action="send_as_user", grantee_id="mallory")


def test_grantee_validation_checks_matching_decision_payload(session):
    _approve(session, grantee_id="alice")
    _approve(session, grantee_id="bob", payload_hash="different-payload")
    with pytest.raises(AuthorizationError, match="decision_payload_changed"):
        auth.validate(session, org_id="org", target_type="draft", target_id="d1",
                      target_revision=2, payload_hash=sha256_json({"body": "答案正文", "chat": "c1"}),
                      action="send_as_user", grantee_id="bob")


def test_decision_is_expired_at_its_exact_deadline(session):
    deadline = utcnow()
    _, payload = _approve(session, expires_at=deadline)
    with pytest.raises(AuthorizationError, match="decision_expired"):
        auth.validate(session, target_type="draft", target_id="d1", target_revision=2,
                      payload_hash=sha256_json(payload), action="send_as_user",
                      grantee_id="emp1", now=deadline)


def test_validate_binds_revision_payload_action_expiry_grantee(session):
    _, payload = _approve(session, expires_at=utcnow() + timedelta(hours=1))
    base = dict(target_type="draft", target_id="d1")
    good_hash = sha256_json(payload)
    # 目标版本变化：旧批准无效
    with pytest.raises(AuthorizationError, match="decision_target_revision_changed"):
        auth.validate(session, **base, target_revision=3, payload_hash=good_hash,
                      action="send_as_user", grantee_id="emp1")
    # 正文变化：旧批准无效
    with pytest.raises(AuthorizationError, match="decision_payload_changed"):
        auth.validate(session, **base, target_revision=2,
                      payload_hash=sha256_json({"body": "被篡改", "chat": "c1"}),
                      action="send_as_user", grantee_id="emp1")
    # 批准的动作之外：知识共享不允许（复用许可与发送许可分别判断）
    with pytest.raises(AuthorizationError, match="action_not_in_decision"):
        auth.validate(session, **base, target_revision=2, payload_hash=good_hash,
                      action="share_knowledge", grantee_id="emp1")
    # 过期
    with pytest.raises(AuthorizationError, match="decision_expired"):
        auth.validate(session, **base, target_revision=2, payload_hash=good_hash,
                      action="send_as_user", grantee_id="emp1",
                      now=utcnow() + timedelta(hours=2))
    # 被授权主体之外
    with pytest.raises(AuthorizationError, match="decision_grantee_mismatch"):
        auth.validate(session, **base, target_revision=2, payload_hash=good_hash,
                      action="send_as_user", grantee_id="someone-else")
    # 无任何批准
    with pytest.raises(AuthorizationError, match="no_active_decision"):
        auth.validate(session, target_type="draft", target_id="missing",
                      target_revision=1, payload_hash="x", action="send_as_user")


def test_revoke_cancels_pending_intents_and_keeps_truthful_receipts(session):
    _, payload = _approve(session)
    pending = enqueue(session, intent="reply|d1|1", tenant="t", kind="reply",
                      target_type="message", target_id="d1",
                      payload={"body": "答案正文", "chat": "c1", "auto": False})
    sent = enqueue(session, intent="reply|d1|2", tenant="t", kind="reply",
                   target_type="message", target_id="d1",
                   payload={"body": "答案正文", "chat": "c1", "auto": False})
    sent.status = OutboxStatus.SENT
    session.flush()
    cancelled = auth.revoke(session, "owner-1", target_type="draft", target_id="d1", org_id="org")
    session.refresh(pending)
    session.refresh(sent)
    assert pending.status == OutboxStatus.CANCELLED
    assert "decision_revoked" in pending.last_error
    assert sent.status == OutboxStatus.SENT          # 已发送保留真实状态
    assert cancelled == [pending.intent_key]
    # 撤回后重验失败
    with pytest.raises(AuthorizationError, match="no_active_decision"):
        auth.validate(session, target_type="draft", target_id="d1", target_revision=2,
                      payload_hash=sha256_json(payload), action="send_as_user")


def test_send_identity_frozen_without_silent_fallback(session):
    _, payload = _approve(session)   # actions={send_as_user}, allow_bot_fallback=False
    # user 身份有效：按批准身份发送（R02：批准绑定 grantee，重验必须携带同一 grantee）
    decision, identity = auth.resolve_send_identity(
        session, target_type="draft", target_id="d1", target_revision=2,
        payload=payload, requested_identity="user", user_identity_valid=True,
        grantee_id="emp1")
    assert identity == "user"
    # user 身份失效且未显式允许 bot 备选：等待授权，不悄悄降级
    with pytest.raises(AuthorizationError, match="waiting_authorization"):
        auth.resolve_send_identity(
            session, target_type="draft", target_id="d1", target_revision=2,
            payload=payload, requested_identity="user", user_identity_valid=False,
            grantee_id="emp1")
    # 显式允许 bot 备选的批准载荷才可降级
    fallback, payload2 = _approve(session, target_id="d2",
                                  actions={"send_as_user", "send_as_bot"},
                                  allow_bot_fallback=True)
    decision2, identity2 = auth.resolve_send_identity(
        session, target_type="draft", target_id="d2", target_revision=2,
        payload=payload2, requested_identity="user", user_identity_valid=False,
        grantee_id="emp1")
    assert identity2 == "bot"
    # 请求未批准的身份（bot 不在动作内）直接拒绝
    with pytest.raises(AuthorizationError, match="action_not_in_decision"):
        auth.resolve_send_identity(
            session, target_type="draft", target_id="d1", target_revision=2,
            payload=payload, requested_identity="bot", user_identity_valid=True,
            grantee_id="emp1")


def test_decision_registered_by_product_migration(tmp_path):
    from sqlalchemy import inspect
    from evoharness.migrations import migrate
    engine = create_engine("sqlite:///" + str(tmp_path / "gov.db"))
    report = migrate(engine, backup_dir=tmp_path / "b")
    assert report["ok"]
    assert "governance_decisions" in set(inspect(engine).get_table_names())
    engine.dispose()
