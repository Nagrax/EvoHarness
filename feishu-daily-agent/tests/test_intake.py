"""F02：准入、事件去重、编辑撤回、历史补采与覆盖。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.core.clock import previous_day_window
from app.core.database import session_scope
from app.domain import admission, intake
from app.models import (
    CoverageStatus,
    DraftStatus,
    Experience,
    ExperienceStatus,
    Message,
    Outbox,
    ReplyDraft,
    Source,
    SourceSync,
)
from tests.helpers import incoming


def test_event_dedupe_and_message_idempotent(source):
    with session_scope() as session:
        assert intake.record_event(
            session, tenant="default", event_id="evt-1", event_type="receive",
            message_id="om_1", payload={"a": 1},
        )
        assert not intake.record_event(
            session, tenant="default", event_id="evt-1", event_type="receive",
            message_id="om_1", payload={"a": 1},
        )
        intake.ingest_message(session, incoming("你好", "om_1"), source=source)
        intake.ingest_message(session, incoming("你好", "om_1"), source=source)
        assert session.query(Message).count() == 1


def test_admission_rejects_unknown_bot_identity(source, settings, monkeypatch):
    monkeypatch.setattr(settings, "feishu_bot_open_id", "")
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "oc_group,oc_1")
    with session_scope() as session:
        db_source = session.get(Source, source.id)
        group = admission.ensure_source(
            session, tenant="default", owner="ou_owner", chat_id="oc_group",
            source_type="group", reply_allowed=False,
        )
        session.flush()
        result = admission.admit(
            session,
            incoming("大家好", "om_g1", chat_id="oc_group", chat_type="group",
                     mentions=["ou_someone"], create_ms=1000),
        )
        assert not result.accepted
        assert result.reason == "bot_identity_unknown"


def test_admission_group_mention_with_known_bot(source, settings, monkeypatch):
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "oc_group")
    with session_scope() as session:
        admission.ensure_source(
            session, tenant="default", owner="ou_owner", chat_id="oc_group",
            source_type="group", reply_allowed=False,
        )
        session.flush()
        result = admission.admit(
            session,
            incoming("请帮我看看", "om_g2", chat_id="oc_group", chat_type="group",
                     mentions=["ou_bot"], create_ms=2000),
        )
        assert result.accepted
        assert result.should_process


def test_realtime_group_no_mention_is_ingested_without_processing(settings, monkeypatch):
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "oc_group")
    from app.handlers.events import persist_event

    with session_scope() as session:
        admission.ensure_source(
            session, tenant="default", owner="ou_owner", chat_id="oc_group",
            source_type="group", reply_allowed=True,
        )
    accepted, reason, should_process = persist_event(
        incoming("大家好", "om_group_noat", chat_id="oc_group", chat_type="group",
                 mentions=["ou_someone"])
    )
    assert accepted
    assert reason == "group_no_mention_ingest_only"
    assert not should_process
    with session_scope() as session:
        assert session.query(Message).filter(Message.message_id == "om_group_noat").one()


def test_admission_rejects_revoked_source(source):
    with session_scope() as session:
        db_source = session.get(Source, source.id)
        db_source.revoked_at = datetime.utcnow()
        session.flush()
        result = admission.admit(session, incoming("在吗", "om_1"))
        assert not result.accepted
        assert result.reason == "source_revoked"


def test_edit_invalidates_pending_draft_and_keeps_sent_receipt(source):
    with session_scope() as session:
        first = intake.ingest_message(session, incoming("请问年假怎么申请", "om_1"), source=source)
        session.add(
            ReplyDraft(
                message_id="om_1", chat_id=source.chat_id,
                draft_content="旧草稿", status=DraftStatus.PENDING,
            )
        )
        sent = ReplyDraft(
            message_id="om_1", chat_id=source.chat_id,
            draft_content="已发", status=DraftStatus.SENT, sent_message_id="om_already",
        )
        session.add(sent)
        session.flush()
        edited = intake.ingest_message(
            session, incoming("请问年假怎么申请（补充）", "om_1"), source=source
        )
        assert edited.edited
        assert edited.message.revision == 2
        pending = session.query(ReplyDraft).filter(ReplyDraft.status == DraftStatus.SUPERSEDED).one()
        assert pending.draft_content == "旧草稿"
        still_sent = session.query(ReplyDraft).filter(ReplyDraft.status == DraftStatus.SENT).one()
        assert still_sent.sent_message_id == "om_already"


def test_recall_stops_learning_and_pauses_experience(source):
    from app.memory.experience import create_candidate

    with session_scope() as session:
        intake.ingest_message(session, incoming("请问年假怎么申请", "om_1"), source=source)
        exp = create_candidate(
            session, question="请问年假怎么申请", answer="答案",
            source_message_id="om_1", visibility="public", sensitive_level="internal",
        )
        intake.mark_recalled(session, tenant="default", message_id="om_1")
        assert exp.status == ExperienceStatus.SUPERSEDED


class PagedFetcher:
    def __init__(self, pages, fail_on: str | None = None):
        self.pages = pages
        self.fail_on = fail_on
        self.calls = 0

    def fetch_page(self, chat_id, thread_id, start_ms, end_ms, page_token):
        self.calls += 1
        if self.fail_on and page_token == self.fail_on:
            raise RuntimeError("network down")
        index = int(page_token or 0)
        if index >= len(self.pages):
            return type("P", (), {"items": [], "next_token": "", "has_more": False})()
        return type(
            "P", (), {
                "items": self.pages[index],
                "next_token": str(index + 1),
                "has_more": index + 1 < len(self.pages),
            },
        )()


def test_history_sync_pages_overlap_and_idempotent(source):
    start = 1_000_000
    end = 2_000_000
    page1 = [incoming("a", "om_a", create_ms=1_100_000), incoming("b", "om_b", create_ms=1_200_000)]
    page2 = [incoming("b", "om_b", create_ms=1_200_000), incoming("c", "om_c", create_ms=1_300_000)]
    fetcher = PagedFetcher([page1, page2])
    with session_scope() as session:
        db_source = session.get(Source, source.id)
        result = intake.sync_source_window(session, db_source, start_ms=start, end_ms=end, fetcher=fetcher)
        assert result.status == CoverageStatus.COMPLETE
        assert result.pages == 2
        assert session.query(Message).count() == 3
        db_source = session.get(Source, source.id)
        assert db_source.last_success_watermark_ms == end - 1
        # 重跑整窗：复用已完成状态，不重复处理
        again = intake.sync_source_window(session, db_source, start_ms=start, end_ms=end, fetcher=fetcher)
        assert again.status == CoverageStatus.COMPLETE
        assert session.query(Message).count() == 3


def test_history_sync_failure_is_partial_and_resumable(source):
    start = 1_000_000
    end = 2_000_000
    page1 = [incoming("a", "om_a", create_ms=1_100_000)]
    page2 = [incoming("b", "om_b", create_ms=1_200_000)]
    failing = PagedFetcher([page1, page2], fail_on="1")
    with session_scope() as session:
        db_source = session.get(Source, source.id)
        result = intake.sync_source_window(session, db_source, start_ms=start, end_ms=end, fetcher=failing)
        assert result.status == CoverageStatus.PARTIAL
        assert session.query(Message).count() == 1
        assert db_source.coverage_status == CoverageStatus.PARTIAL
        # 同一窗口重入：从已保存 cursor 继续
        resumed = intake.sync_source_window(
            session, db_source, start_ms=start, end_ms=end,
            fetcher=PagedFetcher([page1, page2]),
        )
        assert resumed.status == CoverageStatus.COMPLETE
        assert session.query(Message).count() == 2


def test_edit_reprocesses_message_instead_of_staying_handled(source):
    from app.services import reply_engine

    with session_scope() as session:
        first = reply_engine.process_incoming(
            session, incoming("请问年假怎么申请", "om_edit"), source=source
        )
        first_id = first.id
    with session_scope() as session:
        second = reply_engine.process_incoming(
            session, incoming("请问年假怎么申请（补充）", "om_edit"), source=source
        )
        assert second is not None
        assert second.id != first_id
        assert session.get(ReplyDraft, first_id).status == DraftStatus.SUPERSEDED


def test_history_message_is_classified_when_processed(source):
    from app.services import reply_engine

    with session_scope() as session:
        intake.ingest_message(session, incoming("请问年假怎么申请", "om_hist"), source=source)
    with session_scope() as session:
        draft = reply_engine.process_incoming(
            session, incoming("请问年假怎么申请", "om_hist"), source=source
        )
        assert draft is not None
        assert draft.status == DraftStatus.PENDING
        message = session.query(Message).filter(Message.message_id == "om_hist").one()
        assert message.category == "question"


def test_from_event_accepts_message_type_field():
    """飞书事件模型用 message_type；历史接口模型用 msg_type，两者都必须可转换。"""
    from app.feishu.messages import from_event

    def make_message(**overrides):
        values = {
            "message_id": "om_event",
            "chat_id": "oc_1",
            "chat_type": "p2p",
            "content": json.dumps({"text": "你好"}, ensure_ascii=False),
            "create_time": 1_700_000_000_000,
            "thread_id": "",
            "mentions": [],
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def make_event(message):
        sender = SimpleNamespace(
            sender_id=SimpleNamespace(open_id="ou_sender"), sender_type="user"
        )
        return SimpleNamespace(message=message, sender=sender)

    event_message = from_event(make_event(make_message(message_type="text")))
    assert event_message.msg_type == "text"
    assert event_message.text == "你好"

    history_message = from_event(make_event(make_message(msg_type="text")))
    assert history_message.msg_type == "text"
    assert history_message.text == "你好"


def test_history_trim_drops_out_of_window_items(source):
    start = 1_000_000
    end = 2_000_000
    page = [incoming("before", "om_before", create_ms=900_000), incoming("inside", "om_inside", create_ms=1_500_000)]
    with session_scope() as session:
        db_source = session.get(Source, source.id)
        intake.sync_source_window(session, db_source, start_ms=start, end_ms=end, fetcher=PagedFetcher([page]))
        ids = {message.message_id for message in session.query(Message).all()}
        assert ids == {"om_inside"}
