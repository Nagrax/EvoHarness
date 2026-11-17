"""R09 发送身份冻结：user token 失效不悄悄改用 bot，统一身份冻结接到真实发送链。

独立审核探针（send_identity_fallback）口径：对合成消息建立 send_as_user、
allow_bot_fallback=False 的批准；outbox.enqueue 后用与适配器一致的核心 precheck
（outbox._precheck + decisions.check_send_allowed 组合）调 dispatch_outbox，
user_token_resolver 返回空串——
修复前：假传输被调用且收到空 user_access_token，最终状态 sent；
修复后：token 不可用且未批准 fallback 时传输绝不被调用，意图转
waiting_authorization（非 sent 等待态），token 恢复后可对账重试。
全部离线：假传输 / 假 resolver / 合成数据，无网络。
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evoharness import outbox as core_outbox
from evoharness.clock import utcnow
from evoharness.governance import authorization, decisions
from evoharness.ids import sha256_json
from evoharness.models import Base, Outbox, OutboxStatus
from evoharness.ports import TransportResult

POLICY = SimpleNamespace(
    deployment_mode="offline",
    writes_enabled=True,
    business_reply_enabled=True,
    auto_reply_enabled=False,
    reply_allowed_for=lambda chat: True,
)

PAYLOAD = {"text": "合成回复正文", "twin_open_id": "alice", "is_p2p": True}


class Transport:
    """假传输：记录每次回复的真实发送身份与 token，不触网。"""

    def __init__(self, results=None):
        self.calls: list[dict] = []
        self._results = list(results or [])

    def reply_text(self, message_id, text, idempotency_key="", reply_in_thread=False, *,
                   user_access_token="", user_chat_id="", send_identity="bot"):
        self.calls.append({
            "message_id": message_id, "text": text,
            "user_access_token": user_access_token,
            "user_chat_id": user_chat_id,
            "send_identity": send_identity,
        })
        if self._results:
            return self._results.pop(0)
        return TransportResult("sent", receipt_id=f"om_receipt_{len(self.calls)}")

    def reply_card(self, message_id, card, idempotency_key="", reply_in_thread=False):
        self.calls.append({"card": True})
        return TransportResult("sent", receipt_id="om_card_receipt")

    def send_text(self, receive_id, text, idempotency_key=""):
        return TransportResult("sent", receipt_id="om_text_receipt")

    def send_card(self, receive_id, card, idempotency_key=""):
        return TransportResult("sent", receipt_id="om_card2_receipt")


@pytest.fixture()
def env():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    transport = Transport()
    context = {"factory": factory, "transport": transport, "engine": engine}
    try:
        yield context
    finally:
        engine.dispose()


def _precheck(session, row):
    """与适配器一致的核心 precheck 组合（审核探针同口径）。"""
    ok, reason = core_outbox._precheck(session, row, policy=POLICY)
    return decisions.check_send_allowed(session, row) if ok else (ok, reason)


def _approve_send_as_user(session, *, target_id="msg-1", grantee_id="alice",
                          allow_bot_fallback=False, payload=None,
                          expires_at=None, actions=None):
    payload = payload if payload is not None else PAYLOAD
    return authorization.approve(
        session, "owner", org_id="org", target_type="message", target_id=target_id,
        target_revision=1, payload_hash=sha256_json(payload),
        actions=actions or {"send_as_user"}, grantee_id=grantee_id,
        allow_bot_fallback=allow_bot_fallback, expires_at=expires_at)


def _enqueue(session, *, target_id="msg-1", payload=None, intent="reply|freeze|1"):
    payload = payload if payload is not None else PAYLOAD
    row = core_outbox.enqueue(session, intent=intent, tenant="tenant", kind="reply",
                              target_type="message", target_id=target_id,
                              payload=payload, chat_id="oc_chat_1")
    session.flush()
    return row


def _dispatch(context, *, resolver, now=None, limit=3):
    return core_outbox.dispatch_outbox(
        context["factory"], transport=context["transport"], limit=limit,
        outbox_id=None, policy=POLICY, precheck=_precheck,
        user_token_resolver=resolver, now=now)


def _row(factory, intent):
    with factory() as session:
        row = session.query(Outbox).filter(Outbox.intent_key == intent).one()
        session.expunge(row)
        return row


# ── 探针负例：token 不可用 + 未批准 fallback → 传输绝不被调用 ────────────────


def test_token_unavailable_without_fallback_never_calls_transport(env):
    with env["factory"].begin() as session:
        _approve_send_as_user(session, allow_bot_fallback=False)
        row = _enqueue(session)
        intent = row.intent_key
    results = _dispatch(env, resolver=lambda *args: "")
    assert env["transport"].calls == [], "未批准 fallback 时传输绝不能被调用"
    assert [r.status for r in results] == [OutboxStatus.WAITING_AUTHORIZATION]
    row = _row(env["factory"], intent)
    assert row.status == OutboxStatus.WAITING_AUTHORIZATION
    assert "waiting_authorization:token_unavailable" in row.last_error
    assert row.claim_token == "" and row.lease_until is None


def test_resolver_exception_counts_as_token_unavailable(env):
    with env["factory"].begin() as session:
        _approve_send_as_user(session)
        row = _enqueue(session, intent="reply|freeze|boom")
        intent = row.intent_key

    def broken_resolver(*args):
        raise RuntimeError("decrypt failed")

    results = _dispatch(env, resolver=broken_resolver)
    assert env["transport"].calls == []
    assert [r.status for r in results] == [OutboxStatus.WAITING_AUTHORIZATION]
    assert "waiting_authorization:token_unavailable" in _row(env["factory"], intent).last_error


# ── 可对账恢复：token 恢复后 worker 按 next_at 重试成功 ─────────────────────


def test_token_recovered_after_waiting_sends_as_user(env):
    with env["factory"].begin() as session:
        _approve_send_as_user(session)
        row = _enqueue(session, intent="reply|freeze|recover")
        intent = row.intent_key
    first = _dispatch(env, resolver=lambda *args: "")
    assert first[0].status == OutboxStatus.WAITING_AUTHORIZATION
    waiting = _row(env["factory"], intent)
    assert waiting.next_at is not None
    # token 恢复（resolver 返回可用 token）：按 next_at 重试，本人身份发送
    second = _dispatch(env, resolver=lambda *args: "u-fresh-token",
                       now=waiting.next_at + timedelta(seconds=1))
    assert second[0].status == OutboxStatus.SENT
    assert len(env["transport"].calls) == 1
    call = env["transport"].calls[0]
    assert call["send_identity"] == "user"
    assert call["user_access_token"] == "u-fresh-token"
    assert call["user_chat_id"] == "oc_chat_1"
    row = _row(env["factory"], intent)
    assert row.status == OutboxStatus.SENT
    assert row.receipt_message_id == "om_receipt_1"
    assert row.sent_identity == "user", "回执必须记录真实发送身份"
    assert row.last_error == ""


# ── 获批 fallback：只有显式 allow_bot_fallback=True 才允许 bot 身份 ─────────


def test_approved_bot_fallback_sends_as_bot_and_records_receipt(env):
    with env["factory"].begin() as session:
        _approve_send_as_user(session, allow_bot_fallback=True,
                              actions={"send_as_user", "send_as_bot"})
        row = _enqueue(session, intent="reply|freeze|fallback")
        intent = row.intent_key
    results = _dispatch(env, resolver=lambda *args: "")
    assert results[0].status == OutboxStatus.SENT
    call = env["transport"].calls[0]
    assert call["send_identity"] == "bot"
    assert call["user_access_token"] == "", "fallback 不得携带本人 token 伪装本人发送"
    row = _row(env["factory"], intent)
    assert row.status == OutboxStatus.SENT
    assert row.sent_identity == "bot", "fallback 回执必须标记 sent_as_bot"


def test_bot_fallback_requires_explicit_allow_flag(env):
    with env["factory"].begin() as session:
        # actions 含 send_as_bot 但 allow_bot_fallback=False：token 失效仍不得降级
        _approve_send_as_user(session, actions={"send_as_user", "send_as_bot"},
                              allow_bot_fallback=False)
        row = _enqueue(session, intent="reply|freeze|nofallback")
        intent = row.intent_key
    results = _dispatch(env, resolver=lambda *args: "")
    assert env["transport"].calls == []
    assert results[0].status == OutboxStatus.WAITING_AUTHORIZATION
    assert "waiting_authorization:token_unavailable" in _row(env["factory"], intent).last_error


# ── 批准要素逐一校验：撤权 / 过期 / 载荷 / 被授权人 ─────────────────────────


def test_revoked_decision_cancels_waiting_intent_before_send(env):
    with env["factory"].begin() as session:
        _approve_send_as_user(session)
        row = _enqueue(session, intent="reply|freeze|revoke")
        intent = row.intent_key
    first = _dispatch(env, resolver=lambda *args: "")
    assert first[0].status == OutboxStatus.WAITING_AUTHORIZATION
    with env["factory"].begin() as session:
        authorization.revoke(session, "owner", target_type="message",
                             target_id="msg-1", org_id="org")
        # waiting 状态不在 revoke 的即时取消范围，由发送前重验拦截
    waiting = _row(env["factory"], intent)
    second = _dispatch(env, resolver=lambda *args: "",
                       now=waiting.next_at + timedelta(seconds=1))
    assert env["transport"].calls == [], "撤权后绝不能再发送"
    assert second[0].status == OutboxStatus.CANCELLED
    row = _row(env["factory"], intent)
    assert row.status == OutboxStatus.CANCELLED
    assert "decision_revoked" in row.last_error


def test_expired_approval_waits_instead_of_sending(env):
    with env["factory"].begin() as session:
        _approve_send_as_user(session, expires_at=utcnow() - timedelta(hours=1))
        row = _enqueue(session, intent="reply|freeze|expired")
        intent = row.intent_key
    results = _dispatch(env, resolver=lambda *args: "")
    assert env["transport"].calls == []
    assert results[0].status == OutboxStatus.WAITING_AUTHORIZATION
    assert "decision_expired" in _row(env["factory"], intent).last_error


def test_payload_drift_after_approval_waits_instead_of_sending(env):
    with env["factory"].begin() as session:
        _approve_send_as_user(session, payload=dict(PAYLOAD))
        row = _enqueue(session, payload={**PAYLOAD, "text": "被篡改的正文"},
                       intent="reply|freeze|drift")
        intent = row.intent_key
    results = _dispatch(env, resolver=lambda *args: "u-valid-token")
    assert env["transport"].calls == [], "载荷与批准不匹配即不发送（token 可用也不行）"
    assert results[0].status == OutboxStatus.WAITING_AUTHORIZATION
    assert "decision_payload_changed" in _row(env["factory"], intent).last_error


def test_grantee_mismatch_waits_instead_of_sending(env):
    bob_payload = {**PAYLOAD, "twin_open_id": "bob"}
    with env["factory"].begin() as session:
        # 批准绑定 grantee=alice，发送意图却声明 bob 的分身（载荷一致，仅主体不符）
        _approve_send_as_user(session, grantee_id="alice", payload=bob_payload)
        row = _enqueue(session, payload=bob_payload, intent="reply|freeze|grantee")
        intent = row.intent_key
    results = _dispatch(env, resolver=lambda *args: "u-bob-token")
    assert env["transport"].calls == [], "被授权人与分身本人不符即不发送"
    assert results[0].status == OutboxStatus.WAITING_AUTHORIZATION
    assert "decision_grantee_mismatch" in _row(env["factory"], intent).last_error


def test_identity_not_in_approved_actions_waits(env):
    with env["factory"].begin() as session:
        # 批准只覆盖 bot 身份；分身意图（user）不在批准动作内 → 不发送
        _approve_send_as_user(session, actions={"send_as_bot"})
        row = _enqueue(session, intent="reply|freeze|action")
        intent = row.intent_key
    results = _dispatch(env, resolver=lambda *args: "u-alice-token")
    assert env["transport"].calls == []
    assert results[0].status == OutboxStatus.WAITING_AUTHORIZATION
    assert "action_not_in_decision" in _row(env["factory"], intent).last_error


# ── 无治理绑定：沿用既有口径（零行为变化）────────────────────────────────────


def test_no_binding_keeps_legacy_bot_fallback(env):
    with env["factory"].begin() as session:
        row = _enqueue(session, intent="reply|freeze|legacy-bot")
        intent = row.intent_key
    results = _dispatch(env, resolver=lambda *args: "")
    assert results[0].status == OutboxStatus.SENT
    call = env["transport"].calls[0]
    assert call["send_identity"] == "bot"
    assert call["user_access_token"] == ""
    row = _row(env["factory"], intent)
    assert row.status == OutboxStatus.SENT
    assert row.sent_identity == "bot"


def test_no_binding_user_token_still_sends_as_user(env):
    with env["factory"].begin() as session:
        row = _enqueue(session, intent="reply|freeze|legacy-user")
        intent = row.intent_key
    results = _dispatch(env, resolver=lambda *args: "u-legacy-token")
    assert results[0].status == OutboxStatus.SENT
    call = env["transport"].calls[0]
    assert call["send_identity"] == "user"
    assert call["user_access_token"] == "u-legacy-token"
    assert _row(env["factory"], intent).sent_identity == "user"
