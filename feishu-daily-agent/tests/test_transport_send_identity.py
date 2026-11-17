"""R09 传输契约与真实适配器接线：身份由上游冻结决定，传输层拒绝静默降级。

覆盖：
- 传输层防御纵深：send_identity="user" 且 token/chat 缺失 → 明确 permanent 错误，
  绝不静默走 bot（真实 FeishuTransport，假 _reply/_send_as_user，离线）；
- 解密失败 / refresh 失败：身份服务返回 None（token 不可用）→ 意图转
  waiting_authorization，传输不被调用；
- 撤权后发送被拒；
- token 恢复（重新授权）后按 next_at 重试成功；
- 适配器链路（app.domain.outbox.dispatch_outbox）端到端走同一冻结语义。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from evoharness.governance import authorization
from evoharness.models import OutboxStatus

from app.core.database import SessionLocal, session_scope
from app.domain import outbox as outbox_module
from app.feishu.transport import FeishuTransport, TransportResult
from app.models import Outbox as AppOutbox
from app.services import identity, oauth
from tests.helpers import FakeTransport

OWNER = "ou_freeze_owner"


@pytest.fixture(autouse=True)
def _single_approval(settings, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "single_approval")


def _approve(session, *, target_id, payload, actions=None,
             allow_bot_fallback=False, expires_at=None):
    from evoharness.ids import sha256_json

    return authorization.approve(
        session, OWNER, org_id=f"org-{uuid4().hex[:8]}", target_type="message",
        target_id=target_id, target_revision=1,
        payload_hash=sha256_json(payload), actions=actions or {"send_as_user"},
        grantee_id=OWNER, allow_bot_fallback=allow_bot_fallback,
        expires_at=expires_at)


def _enqueue(session, *, target_id, payload, intent):
    from evoharness.outbox import enqueue

    return enqueue(session, intent=intent, tenant="default", kind="reply",
                   target_type="message", target_id=target_id,
                   payload=payload, chat_id="oc_freeze_p2p")


PAYLOAD = {"text": "冻结契约回复", "twin_open_id": OWNER, "is_p2p": True}


# ── 传输层契约（真实 FeishuTransport，不打网络）──────────────────────────────


def test_transport_rejects_user_intent_without_token(monkeypatch):
    transport = FeishuTransport()

    def must_not_reply(*args, **kwargs):
        raise AssertionError("user 意图缺 token 时绝不能静默走 bot 回复路径")

    monkeypatch.setattr(transport, "_reply", must_not_reply)
    result = transport.reply_text("om_target", "正文", "idem-1",
                                  user_access_token="", user_chat_id="oc_1",
                                  send_identity="user")
    assert result.outcome == "permanent"
    assert result.error == "user_identity_token_unavailable"


def test_transport_rejects_user_intent_without_chat(monkeypatch):
    transport = FeishuTransport()

    def must_not_reply(*args, **kwargs):
        raise AssertionError("user 意图缺 chat_id 时绝不能静默走 bot 回复路径")

    monkeypatch.setattr(transport, "_reply", must_not_reply)
    result = transport.reply_text("om_target", "正文", "idem-2",
                                  user_access_token="u-token",
                                  user_chat_id="", send_identity="user")
    assert result.outcome == "permanent"
    assert result.error == "user_identity_token_unavailable"


def test_transport_user_intent_sends_as_user(monkeypatch):
    transport = FeishuTransport()
    seen = {}

    def fake_send_as_user(chat_id, text, token):
        seen.update(chat_id=chat_id, text=text, token=token)
        return TransportResult("sent", receipt_id="om_user_receipt")

    monkeypatch.setattr(transport, "_send_as_user", fake_send_as_user)
    result = transport.reply_text("om_target", "正文", "idem-3",
                                  user_access_token="u-token",
                                  user_chat_id="oc_1", send_identity="user")
    assert result.outcome == "sent" and result.receipt_id == "om_user_receipt"
    assert seen == {"chat_id": "oc_1", "text": "正文", "token": "u-token"}


def test_transport_bot_intent_keeps_reply_path(monkeypatch):
    transport = FeishuTransport()
    replied = {}

    def fake_reply(message_id, msg_type, content, idempotency_key,
                   reply_in_thread=False):
        replied["message_id"] = message_id
        return TransportResult("sent", receipt_id="om_bot_receipt")

    monkeypatch.setattr(transport, "_reply", fake_reply)
    # 显式 bot 意图（含获批 fallback）仍走既有 bot 回复路径
    result = transport.reply_text("om_target", "正文", "idem-4",
                                  user_access_token="", user_chat_id="oc_1",
                                  send_identity="bot")
    assert result.outcome == "sent" and result.receipt_id == "om_bot_receipt"
    assert replied["message_id"] == "om_target"
    # 兼容：未传 send_identity 的旧调用按机器人身份处理
    result2 = transport.reply_text("om_target", "旧调用", "idem-5")
    assert result2.outcome == "sent"


# ── 适配器链路：解密失败 / refresh 失败 / 撤权 / 恢复 ────────────────────────


def _authorized_identity(session):
    return identity.save_authorization(
        session,
        tenant="default",
        result={
            "accessToken": "u-freeze-token",
            "refreshToken": "r-freeze-refresh",
            "expiresIn": 7200,
            "refreshExpiresIn": 86400,
            "scope": "im:message im:message.send_as_user",
            "user": {"openId": OWNER, "unionId": "", "name": "冻结契约用户"},
        },
    )


def _dispatch(transport):
    return outbox_module.dispatch_outbox(SessionLocal, transport=transport, limit=3)


def test_decrypt_failure_goes_waiting_not_silent_bot():
    intent = f"freeze-decrypt-{uuid4().hex[:8]}"
    target = f"om-{uuid4().hex[:10]}"
    with session_scope() as session:
        ident = _authorized_identity(session)
        # 模拟密文损坏/密钥更换：解密失败 → token 不可用
        ident.access_token_enc = "definitely-not-a-valid-fernet-token"
        ident.refresh_token_enc = ""
        session.flush()
        _approve(session, target_id=target, payload=PAYLOAD)
        _enqueue(session, target_id=target, payload=PAYLOAD, intent=intent)
    transport = FakeTransport()
    results = _dispatch(transport)
    assert transport.calls == 0, "解密失败且未批准 fallback 时不得发送"
    assert results[0].status == OutboxStatus.WAITING_AUTHORIZATION
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        assert row.status == OutboxStatus.WAITING_AUTHORIZATION
        assert "waiting_authorization:token_unavailable" in row.last_error


def test_refresh_failure_goes_waiting_not_silent_bot(monkeypatch):
    intent = f"freeze-refresh-{uuid4().hex[:8]}"
    target = f"om-{uuid4().hex[:10]}"
    with session_scope() as session:
        ident = _authorized_identity(session)
        ident.expires_at = 0  # 已过期，必须走 refresh
        session.flush()
    monkeypatch.setattr(oauth, "refresh_access_token",
                        lambda refresh: {"ok": False, "error": "refresh_failed"})
    with session_scope() as session:
        assert identity.usable_access_token(session, OWNER) is None
        _approve(session, target_id=target, payload=PAYLOAD)
        _enqueue(session, target_id=target, payload=PAYLOAD, intent=intent)
    transport = FakeTransport()
    results = _dispatch(transport)
    assert transport.calls == 0, "refresh 失败且未批准 fallback 时不得发送"
    assert results[0].status == OutboxStatus.WAITING_AUTHORIZATION


def test_reauthorization_retries_waiting_intent_to_sent_as_user():
    intent = f"freeze-retry-{uuid4().hex[:8]}"
    target = f"om-{uuid4().hex[:10]}"
    with session_scope() as session:
        _approve(session, target_id=target, payload=PAYLOAD)
        _enqueue(session, target_id=target, payload=PAYLOAD, intent=intent)
    transport = FakeTransport()
    first = _dispatch(transport)
    assert first[0].status == OutboxStatus.WAITING_AUTHORIZATION
    assert transport.calls == 0
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        next_at = row.next_at
        # 本人重新授权：可用 token 恢复
        _authorized_identity(session)
    second = outbox_module.dispatch_outbox(SessionLocal, transport=transport, limit=3,
                                           now=next_at)
    assert second[0].status == OutboxStatus.SENT
    assert transport.as_user_sent, "token 恢复后应以本人身份发送"
    assert transport.send_identities == ["user"]
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        assert row.status == OutboxStatus.SENT
        assert row.sent_identity == "user"
        assert row.last_error == ""


def test_approved_fallback_sends_as_bot_with_honest_receipt():
    intent = f"freeze-fallback-{uuid4().hex[:8]}"
    target = f"om-{uuid4().hex[:10]}"
    with session_scope() as session:
        _approve(session, target_id=target, payload=PAYLOAD,
                 actions={"send_as_user", "send_as_bot"}, allow_bot_fallback=True)
        _enqueue(session, target_id=target, payload=PAYLOAD, intent=intent)
    transport = FakeTransport()
    results = _dispatch(transport)
    assert results[0].status == OutboxStatus.SENT
    assert not transport.as_user_sent, "fallback 不得伪装本人发送"
    assert transport.send_identities == ["bot"]
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        assert row.status == OutboxStatus.SENT
        assert row.sent_identity == "bot"


def test_revocation_blocks_waiting_intent_on_retry():
    intent = f"freeze-revoke-{uuid4().hex[:8]}"
    target = f"om-{uuid4().hex[:10]}"
    org_id = ""
    with session_scope() as session:
        decision = _approve(session, target_id=target, payload=PAYLOAD)
        org_id = decision.org_id
        _enqueue(session, target_id=target, payload=PAYLOAD, intent=intent)
    transport = FakeTransport()
    first = _dispatch(transport)
    assert first[0].status == OutboxStatus.WAITING_AUTHORIZATION
    with session_scope() as session:
        authorization.revoke(session, OWNER, target_type="message",
                             target_id=target, org_id=org_id)
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        next_at = row.next_at
    second = outbox_module.dispatch_outbox(SessionLocal, transport=transport, limit=3,
                                           now=next_at)
    assert transport.calls == 0, "撤权后重试必须被拒"
    assert second[0].status == OutboxStatus.CANCELLED
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        assert row.status == OutboxStatus.CANCELLED
        assert "decision_revoked" in row.last_error


def test_unbound_target_keeps_legacy_bot_fallback():
    intent = f"freeze-legacy-{uuid4().hex[:8]}"
    target = f"om-{uuid4().hex[:10]}"
    with session_scope() as session:
        _enqueue(session, target_id=target, payload=PAYLOAD, intent=intent)
    transport = FakeTransport()
    results = _dispatch(transport)
    assert results[0].status == OutboxStatus.SENT
    assert not transport.as_user_sent
    assert transport.send_identities == ["bot"], "无治理绑定沿用既有 bot 兜底口径"
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        assert row.sent_identity == "bot"
