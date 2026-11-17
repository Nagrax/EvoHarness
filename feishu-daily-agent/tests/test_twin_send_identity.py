"""数字分身发送回归：以本人身份发出，且不越过授权边界。

核心事实（2026-11-11 实测确认）：
- `im:message.send_as_user` + `im:message` 可以让机器人**以本人身份**发消息，
  回执里 `sender_type=user`、`sender.id` 是本人 open_id；
- 但**用户身份发消息不支持 message_id**，只能发到 open_id/chat_id/email，
  即"分身能在会话里发言"，不能"引用回复某条消息"；
- 没授权的人不会被冒名：拿不到 token 就退回机器人身份。
"""
from __future__ import annotations

from app.core.database import session_scope
from app.domain import admission, outbox as outbox_module
from app.services import identity, reply_engine
from tests.helpers import FakeTransport, incoming

OWNER = "ou_lead"
CHAT = "oc_lead_p2p"


def _setup(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "feishu_owner_open_id", OWNER)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", CHAT)
    monkeypatch.setattr(settings, "feishu_allow_all_chats", True)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "")
    monkeypatch.setattr(settings, "deployment_mode", "single_approval")
    monkeypatch.setattr(settings, "shadow_mode", False)
    monkeypatch.setattr(settings, "auto_reply_enabled", False)


def _authorized_identity(session, settings, open_id: str = OWNER):
    return identity.save_authorization(
        session,
        tenant=settings.tenant_id,
        result={
            "accessToken": "u-test-token",
            "refreshToken": "",
            "expiresIn": 7200,
            "scope": "im:message im:message.send_as_user",
            "user": {"openId": open_id, "unionId": "", "name": "测试用户"},
        },
    )


def test_identity_is_encrypted_at_rest(settings, monkeypatch):
    """token 不能明文落库。"""
    _setup(settings, monkeypatch)
    with session_scope() as session:
        ident = _authorized_identity(session, settings)
        stored = ident.access_token_enc
    assert stored and "u-test-token" not in stored, "token 必须加密存储"


def test_usable_token_roundtrip(settings, monkeypatch):
    _setup(settings, monkeypatch)
    with session_scope() as session:
        _authorized_identity(session, settings)
        assert identity.usable_access_token(session, OWNER) == "u-test-token"


def test_unauthorized_user_has_no_token(settings, monkeypatch):
    """未授权的人绝不能拿到 token —— 否则等于被冒名发言。"""
    _setup(settings, monkeypatch)
    with session_scope() as session:
        assert identity.usable_access_token(session, "ou_stranger") is None


def test_revoked_authorization_stops_sending_as_user(settings, monkeypatch):
    _setup(settings, monkeypatch)
    with session_scope() as session:
        _authorized_identity(session, settings)
        assert identity.revoke(session, OWNER, reason="本人撤回")
        assert identity.usable_access_token(session, OWNER) is None


def test_expired_token_without_refresh_is_not_used(settings, monkeypatch):
    """过期且无 refresh_token 时不能硬用旧 token。"""
    _setup(settings, monkeypatch)
    with session_scope() as session:
        ident = _authorized_identity(session, settings)
        ident.expires_at = 0  # 已过期
        session.flush()
        assert identity.usable_access_token(session, OWNER) is None


def test_reply_uses_user_identity_when_authorized(settings, monkeypatch):
    """已授权：走用户身份发（FakeTransport 记录 as_user_sent）。"""
    _setup(settings, monkeypatch)
    transport = FakeTransport()
    with session_scope() as session:
        _authorized_identity(session, settings)
        source = admission.ensure_source(
            session, tenant=settings.tenant_id, owner=OWNER, chat_id=CHAT,
            source_type="p2p", reply_allowed=True,
        )
        reply_engine.upsert_chat(session, CHAT, "p2p", "t")
        draft = reply_engine.process_incoming(
            session,
            incoming("分身连通性检查：今天适合出门吗", "om_twin_1",
                     chat_id=CHAT, sender=OWNER),
            source=source,
        )
        assert draft is not None
        draft_id = draft.id

    from app.domain import review

    with session_scope() as session:
        review.approve_draft_direct(session, draft_id, actor=OWNER)

    from app.core.database import SessionLocal

    outbox_module.dispatch_outbox(SessionLocal, transport=transport, limit=5)
    assert transport.as_user_sent, "已授权时应以用户身份发送"
    assert transport.as_user_sent[0][0] == CHAT


def test_reply_falls_back_to_bot_when_not_authorized(settings, monkeypatch):
    """未授权：退回机器人身份，绝不冒名。"""
    _setup(settings, monkeypatch)
    transport = FakeTransport()
    with session_scope() as session:
        source = admission.ensure_source(
            session, tenant=settings.tenant_id, owner=OWNER, chat_id=CHAT,
            source_type="p2p", reply_allowed=True,
        )
        reply_engine.upsert_chat(session, CHAT, "p2p", "t")
        draft = reply_engine.process_incoming(
            session,
            incoming("分身连通性检查：今天适合出门吗", "om_twin_2",
                     chat_id=CHAT, sender=OWNER),
            source=source,
        )
        draft_id = draft.id

    from app.domain import review

    with session_scope() as session:
        review.approve_draft_direct(session, draft_id, actor=OWNER)

    from app.core.database import SessionLocal

    outbox_module.dispatch_outbox(SessionLocal, transport=transport, limit=5)
    assert not transport.as_user_sent, "未授权不能以用户身份发送"
    assert transport.replies, "应退回机器人身份照常发送"
