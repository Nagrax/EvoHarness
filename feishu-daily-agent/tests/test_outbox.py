"""F03：事务入队、可信批示、版本绑定、回执与恢复。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.clock import utcnow
from app.core.database import SessionLocal, session_scope
from app.domain import outbox as outbox_module
from app.domain import review as review_module
from app.feishu.transport import TransportResult
from app.models import (
    Category,
    DraftStatus,
    Outbox,
    OutboxStatus,
    ReplyDraft,
    Source,
)
from app.services import reply_engine
from tests.helpers import FakeTransport, incoming


def _pending_draft(source):
    with session_scope() as session:
        return reply_engine.process_incoming(
            session, incoming("请问年假怎么申请", "om_1"), source=source
        )


def _approve(draft_id: int) -> None:
    with session_scope() as session:
        reply_engine.approve_draft(session, draft_id)


def test_approval_queues_before_send(source, approval_mode):
    draft = _pending_draft(source)
    _approve(draft.id)
    with session_scope() as session:
        rows = session.query(Outbox).all()
        assert len(rows) == 1
        assert rows[0].status == OutboxStatus.QUEUED
        assert rows[0].kind == "reply"
        assert rows[0].payload_hash
        assert session.get(ReplyDraft, draft.id).status == DraftStatus.APPROVED
    transport = FakeTransport()
    reply_engine.dispatch_pending(transport=transport)
    with session_scope() as session:
        saved = session.get(ReplyDraft, draft.id)
        assert saved.status == DraftStatus.SENT
        assert saved.sent_message_id == "om_sent_1"


def test_duplicate_approval_does_not_duplicate_send(source, approval_mode):
    draft = _pending_draft(source)
    _approve(draft.id)
    _approve(draft.id)
    with session_scope() as session:
        assert session.query(Outbox).count() == 1
    transport = FakeTransport()
    reply_engine.dispatch_pending(transport=transport)
    reply_engine.dispatch_pending(transport=transport)
    assert len(transport.replies) == 1


def test_approved_draft_cannot_be_edited_or_approved_twice(source, approval_mode):
    draft = _pending_draft(source)
    with session_scope() as session:
        token = review_module.create_action_token(
            session, owner="ou_owner", action="edit", target_type="draft",
            target_id=draft.id, target_version=1,
        )
        action_id = token.action_id
    _approve(draft.id)
    with pytest.raises(review_module.ReviewError):
        with session_scope() as session:
            review_module.handle_card_action(
                session, action_id=action_id, operator_id="ou_owner",
                edited_content="并发修改",
            )
    with session_scope() as session:
        assert session.query(Outbox).count() == 1


def test_reject_after_approve_is_rejected_and_does_not_duplicate(source, approval_mode):
    draft = _pending_draft(source)
    with session_scope() as session:
        token = review_module.create_action_token(
            session, owner="ou_owner", action="reject", target_type="draft",
            target_id=draft.id, target_version=1,
        )
        action_id = token.action_id
    _approve(draft.id)
    with pytest.raises(review_module.ReviewError):
        with session_scope() as session:
            review_module.handle_card_action(
                session, action_id=action_id, operator_id="ou_owner", reason="late reject"
            )
    with session_scope() as session:
        assert session.query(Outbox).count() == 1
        assert session.get(ReplyDraft, draft.id).status == DraftStatus.APPROVED


def test_empty_correction_is_rejected_and_does_not_pause_experience(source, approval_mode):
    from app.memory.experience import create_candidate
    from app.models import Experience, Message

    with session_scope() as session:
        session.add(Message(message_id="om_sent", tenant="default", chat_id="oc_1",
                            create_time=0, content_text="问题"))
        exp = create_candidate(session, question="问题", answer="旧答案",
                               visibility="public", sensitive_level="internal")
        draft = ReplyDraft(tenant="default", message_id="om_sent", chat_id="oc_1",
                           experience_id=exp.id, draft_content="旧答案",
                           status=DraftStatus.SENT, sent_message_id="om_receipt")
        session.add(draft)
        session.flush()
        token = review_module.create_action_token(
            session, owner="ou_owner", action="correct_sent_reply",
            target_type="draft", target_id=draft.id, target_version=draft.version,
        )
        action_id = token.action_id
        exp_id = exp.id
    with pytest.raises(review_module.ReviewError):
        with session_scope() as session:
            review_module.handle_card_action(
                session, action_id=action_id, operator_id="ou_owner", edited_content="   "
            )
    with session_scope() as session:
        assert session.get(Experience, exp_id).status == "candidate"


def test_forged_operator_is_rejected(source, approval_mode):
    draft = _pending_draft(source)
    with session_scope() as session:
        token = review_module.create_action_token(
            session, owner="ou_owner", action="approve", target_type="draft",
            target_id=draft.id, target_version=1,
        )
        action_id = token.action_id
    with pytest.raises(review_module.ReviewError):
        with session_scope() as session:
            review_module.handle_card_action(
                session, action_id=action_id, operator_id="ou_attacker"
            )
    with session_scope() as session:
        assert session.query(Outbox).count() == 0


def test_stale_card_version_is_rejected(source, approval_mode):
    draft = _pending_draft(source)
    with session_scope() as session:
        token = review_module.create_action_token(
            session, owner="ou_owner", action="approve", target_type="draft",
            target_id=draft.id, target_version=99,
        )
        action_id = token.action_id
    with pytest.raises(review_module.ReviewError):
        with session_scope() as session:
            review_module.handle_card_action(
                session, action_id=action_id, operator_id="ou_owner"
            )


def test_transaction_rollback_has_no_external_side_effect(source, approval_mode):
    draft = _pending_draft(source)
    class Boom(RuntimeError):
        pass
    with pytest.raises(Boom):
        with session_scope() as session:
            review_module.approve_draft_direct(
                session, draft.id, actor="ou_owner"
            )
            raise Boom()
    with session_scope() as session:
        assert session.query(Outbox).count() == 0
    transport = FakeTransport()
    reply_engine.dispatch_pending(transport=transport)
    assert transport.calls == 0


def test_retryable_then_sent(approval_mode):
    with session_scope() as session:
        row = outbox_module.enqueue(
            session, intent="notice|retry", tenant="default", kind="notice",
            target_type="open_id", target_id="ou_owner", payload={"text": "hi"},
        )
        row_id = row.id
    transport = FakeTransport(
        results=[
            TransportResult("retryable", error="429", retry_after=30),
            TransportResult("sent", receipt_id="om_ok"),
        ]
    )
    first = outbox_module.dispatch_outbox(SessionLocal, transport=transport)
    assert first[0].status == OutboxStatus.RETRY_WAIT
    assert len(transport.sent_text) == 1
    with session_scope() as session:
        row = session.get(Outbox, row_id)
        next_at = row.next_at
    second = outbox_module.dispatch_outbox(SessionLocal, transport=transport, now=next_at)
    assert second[0].status == OutboxStatus.SENT
    assert transport.sent_text[-1][2] == "om_ok"


def test_unknown_requires_reconciliation_and_never_blind_retries(approval_mode):
    with session_scope() as session:
        row = outbox_module.enqueue(
            session, intent="notice|unknown", tenant="default", kind="notice",
            target_type="open_id", target_id="ou_owner", payload={"text": "hi"},
        )
        row_id = row.id
    transport = FakeTransport(results=[TransportResult("unknown", error="timeout")])
    first = outbox_module.dispatch_outbox(SessionLocal, transport=transport)
    assert first[0].status == OutboxStatus.DELIVERY_UNKNOWN
    # 再次调度不会重发
    second = outbox_module.dispatch_outbox(SessionLocal, transport=transport)
    assert second == []
    assert transport.calls == 1
    with session_scope() as session:
        row = outbox_module.reconcile_unknown(
            session, row_id, receipt_id="om_real", actor="ou_owner"
        )
        assert row.status == OutboxStatus.SENT
        assert row.receipt_message_id == "om_real"


def test_permanent_error_is_quarantined(approval_mode):
    with session_scope() as session:
        row = outbox_module.enqueue(
            session, intent="notice|perm", tenant="default", kind="notice",
            target_type="open_id", target_id="ou_owner", payload={"text": "hi"},
        )
        row_id = row.id
    transport = FakeTransport(results=[TransportResult("permanent", error="forbidden")])
    outbox_module.dispatch_outbox(SessionLocal, transport=transport)
    outbox_module.dispatch_outbox(SessionLocal, transport=transport)
    with session_scope() as session:
        assert session.get(Outbox, row_id).status == OutboxStatus.FAILED
    assert transport.calls == 1


def test_expired_lease_becomes_delivery_unknown_in_offline_mode():
    with session_scope() as session:
        row = outbox_module.enqueue(
            session, intent="notice|lease", tenant="default", kind="notice",
            target_type="open_id", target_id="ou_owner", payload={"text": "hi"},
        )
        row_id = row.id
        claimed = outbox_module.claim_next(session)
        assert claimed.id == row_id
        claimed.lease_until = utcnow() - timedelta(seconds=1)
        session.flush()
        recovered = outbox_module.recover_expired_leases(session)
        assert row_id in recovered
        assert session.get(Outbox, row_id).status == OutboxStatus.DELIVERY_UNKNOWN


def test_paused_auto_reply_cancels_pending_intent(source, auto_mode, monkeypatch):
    monkeypatch.setattr(auto_mode, "auto_reply_enabled", False)
    with session_scope() as session:
        row = outbox_module.enqueue(
            session, intent="reply|paused", tenant="default", kind="reply",
            chat_id="oc_1", target_type="message", target_id="om_1",
            payload={"text": "hi", "auto": True}, source_id=source.id,
        )
        row_id = row.id
    transport = FakeTransport()
    outbox_module.dispatch_outbox(SessionLocal, transport=transport)
    with session_scope() as session:
        assert session.get(Outbox, row_id).status == OutboxStatus.CANCELLED
    assert transport.calls == 0


def test_revoked_source_cancels_pending_intent(source, approval_mode):
    with session_scope() as session:
        db_source = session.get(Source, source.id)
        db_source.revoked_at = utcnow()
        row = outbox_module.enqueue(
            session, intent="reply|revoked", tenant="default", kind="reply",
            chat_id="oc_1", target_type="message", target_id="om_1",
            payload={"text": "hi"}, source_id=source.id,
        )
        row_id = row.id
    transport = FakeTransport()
    outbox_module.dispatch_outbox(SessionLocal, transport=transport)
    with session_scope() as session:
        assert session.get(Outbox, row_id).status == OutboxStatus.CANCELLED
    assert transport.calls == 0
