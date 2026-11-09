"""崩溃恢复：收件事件处理标记与未处理问题重放。"""
from __future__ import annotations

from sqlalchemy import select

from app.core.database import session_scope
from app.domain import intake
from app.handlers import events
from app.models import InboxEvent, ReplyDecision
from tests.helpers import incoming


def _seed(source, text: str, message_id: str) -> None:
    with session_scope() as session:
        intake.record_event(
            session,
            tenant="default",
            event_id=f"evt-{message_id}",
            event_type="im.message.receive_v1",
            message_id=message_id,
            payload=text,
        )
        intake.ingest_message(
            session,
            incoming(text, message_id, chat_id=source.chat_id, sender=source.owner),
            source=source,
        )


def test_replay_processes_unhandled_question(source):
    _seed(source, "请问年假怎么申请？", "om_replay_q")

    replayed = events.replay_recent_questions(window_seconds=3600)

    assert replayed == 1
    with session_scope() as session:
        decision = session.scalar(
            select(ReplyDecision).where(ReplyDecision.message_id == "om_replay_q")
        )
        event = session.scalar(
            select(InboxEvent).where(InboxEvent.event_id == "evt-om_replay_q")
        )
        assert decision is not None
        assert event.processed_at is not None


def test_replay_skips_command_and_marks_processed(source):
    _seed(source, "改 #999 新内容", "om_replay_cmd")

    replayed = events.replay_recent_questions(window_seconds=3600)

    assert replayed == 0
    with session_scope() as session:
        assert session.scalar(
            select(ReplyDecision).where(ReplyDecision.message_id == "om_replay_cmd")
        ) is None
        event = session.scalar(
            select(InboxEvent).where(InboxEvent.event_id == "evt-om_replay_cmd")
        )
        assert event.processed_at is not None


def test_replay_is_idempotent(source):
    _seed(source, "请问年假怎么申请？", "om_replay_twice")

    events.replay_recent_questions(window_seconds=3600)
    events.replay_recent_questions(window_seconds=3600)

    with session_scope() as session:
        count = session.query(ReplyDecision).filter(
            ReplyDecision.message_id == "om_replay_twice"
        ).count()
        assert count == 1
