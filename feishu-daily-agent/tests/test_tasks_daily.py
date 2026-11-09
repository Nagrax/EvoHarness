"""F04：任务生命周期、逻辑日、日报幂等与卡片批示。"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.clock import previous_day_window
from app.core.database import session_scope
from app.domain import reports
from app.domain import review as review_module
from app.domain import tasks as tasks_module
from app.models import (
    ActionToken,
    Category,
    DailyReport,
    DraftStatus,
    Message,
    Outbox,
    ReplyDraft,
    Source,
    Task,
    TaskStatus,
)
from app.services import reply_engine
from tests.helpers import FakeTransport, incoming


def _task_extractor(*, assignee="ou_owner", due="", needs_confirmation=False):
    def extract(text, message_id):
        return {
            "tasks": [
                {
                    "action": text,
                    "assignee": assignee,
                    "due": due,
                    "evidence": [message_id],
                    "confidence": 0.9,
                    "needs_confirmation": needs_confirmation,
                }
            ]
        }

    return extract


def _process_task(source, monkeypatch, text="周五前交材料", mid="om_task1", **kwargs):
    monkeypatch.setattr(
        reply_engine, "classify_message",
        lambda t: {"category": Category.TASK, "summary": text, "due": ""},
    )
    import app.domain.tasks as tasks_module_local

    monkeypatch.setattr(tasks_module_local, "extract_tasks", _task_extractor(**kwargs))
    with session_scope() as session:
        return reply_engine.process_incoming(
            session, incoming(text, mid), source=source
        )


def test_task_needs_confirmation_when_assignee_missing(source, monkeypatch):
    _process_task(source, monkeypatch, assignee="", needs_confirmation=False)
    with session_scope() as session:
        task = session.query(Task).one()
        assert task.status == TaskStatus.NEEDS_CONFIRMATION
        assert task.needs_confirmation
        assert task.evidence()


def test_task_completion_signal_and_manual_reopen(source, monkeypatch):
    _process_task(source, monkeypatch)
    with session_scope() as session:
        task = session.query(Task).one()
        assert task.status == TaskStatus.OPEN
        task_id = task.id
    monkeypatch.setattr(
        reply_engine, "classify_message",
        lambda t: {"category": Category.NOTICE, "summary": "", "due": ""},
    )
    with session_scope() as session:
        reply_engine.process_incoming(
            session, incoming("材料已完成", "om_done"), source=source
        )
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.DONE
        tasks_module.set_status(
            session, task_id, status=TaskStatus.OPEN, actor="ou_owner",
            detail="reopen",
        )
        assert session.get(Task, task_id).status == TaskStatus.OPEN


def test_daily_report_is_idempotent_and_revised_on_late_data(source):
    target = date(2026, 11, 5)
    report_date = target + timedelta(days=1)
    with session_scope() as session:
        first = reports.generate_report(session, logical_date=report_date)
        assert first.created
        assert session.query(DailyReport).count() == 1
        assert session.query(Outbox).count() == 1
        again = reports.generate_report(session, logical_date=report_date)
        assert not again.created and not again.revised
        assert session.query(Outbox).count() == 1

        session.add(
            Task(
                task_key="late-task", tenant="default", owner="ou_owner",
                chat_id="oc_1", thread_id="", title="迟到任务", action="迟到任务",
                assignee_id="ou_owner", assignee_name="owner",
                status=TaskStatus.OPEN, needs_confirmation=False,
                evidence_json="[]", source_message_ids="[]",
            )
        )
        session.flush()
        revised = reports.generate_report(session, logical_date=report_date)
        assert revised.revised
        assert session.query(DailyReport).count() == 1
        assert session.query(Outbox).count() == 2


def test_daily_card_contains_required_sections_and_gaps(source, monkeypatch):
    _process_task(source, monkeypatch)
    target = date(2026, 11, 5)
    report_date = target + timedelta(days=1)
    with session_scope() as session:
        result = reports.generate_report(session, logical_date=report_date)
        report = result.report
        card = json.loads(session.query(Outbox).one().payload_json)["card"]
        text = json.dumps(card, ensure_ascii=False)
        assert "我待处理" in text
        assert "待批回复" in text
        assert "已发回复" in text
        assert "经验变更" in text
        db_source = session.get(Source, source.id)
        db_source.coverage_status = "partial"
        session.flush()
        reports.generate_report(session, logical_date=report_date)
        card = json.loads(
            session.query(Outbox).order_by(Outbox.id.desc()).first().payload_json
        )["card"]
        assert "采集与投递缺口" in json.dumps(card, ensure_ascii=False)


def test_daily_card_contains_supported_edit_instruction(source, approval_mode, monkeypatch):
    from app.services import reply_engine

    monkeypatch.setattr(reply_engine, "classify_message", lambda t: {"category": Category.QUESTION, "summary": "", "due": ""})
    import app.domain.tasks as tasks_module_local

    monkeypatch.setattr(tasks_module_local, "extract_tasks", lambda text, mid: {"tasks": []})
    with session_scope() as session:
        reply_engine.process_incoming(session, incoming("请问年假怎么申请", "om_form"), source=source)
    with session_scope() as session:
        reports.generate_report(session, logical_date=date(2026, 11, 6))
        card = json.loads(session.query(Outbox).one().payload_json)["card"]
        text = json.dumps(card, ensure_ascii=False)
        # 飞书 Card 2.0 不支持在 column 内嵌 form；用 owner 指令完成编辑。
        assert "改 #" in text
        assert '"tag": "form"' not in text


def test_daily_card_uses_client_v1_structure(source, approval_mode, monkeypatch):
    _process_task(source, monkeypatch)
    monkeypatch.setattr(
        reply_engine, "classify_message",
        lambda t: {"category": Category.QUESTION, "summary": "", "due": ""},
    )
    import app.domain.tasks as tasks_module_local

    monkeypatch.setattr(tasks_module_local, "extract_tasks", lambda text, mid: {"tasks": []})
    with session_scope() as session:
        session.query(Task).one().owner = "ou_owner"
        reply_engine.process_incoming(
            session, incoming("请问年假怎么申请", "om_v1_card"), source=source
        )
    with session_scope() as session:
        reports.generate_report(session, logical_date=date(2026, 11, 6))
        card = json.loads(session.query(Outbox).one().payload_json)["card"]

    # 卡片 2.0 在旧版网页端会降级为占位图；1.0 结构必须全局兼容。
    assert "schema" not in card
    assert card["config"]["wide_screen_mode"] is True
    buttons: list[dict] = []

    def walk(elements: list[dict]) -> None:
        for element in elements:
            assert element.get("tag") != "markdown"
            if element.get("tag") == "button":
                buttons.append(element)
                assert "behaviors" not in element
                assert element["value"]["action_id"]
            if element.get("tag") == "column":
                assert all(item.get("tag") != "action" for item in element.get("elements", []))
            walk(element.get("elements", []))
            walk(element.get("actions", []))
            for column in element.get("columns", []):
                walk([column])

    walk(card["elements"])
    assert len(buttons) >= 3


def test_card_callback_result_card_uses_client_v1_structure():
    from app.handlers.card_callback import _result_card

    card = _result_card("已批准并排队发送")
    assert "schema" not in card
    assert card["config"]["wide_screen_mode"] is True
    assert card["elements"][0]["tag"] == "div"
    assert card["elements"][0]["text"]["tag"] == "lark_md"


def test_daily_card_contains_edit_form_for_pending_draft(source, approval_mode, monkeypatch):
    """基线节点保留：卡片不支持 form 后，编辑入口改为 owner 指令断言。"""
    test_daily_card_contains_supported_edit_instruction(source, approval_mode, monkeypatch)


def test_edit_action_requires_non_empty_content(source, approval_mode, monkeypatch):
    from app.domain import review as review_module
    from app.services import reply_engine

    monkeypatch.setattr(reply_engine, "classify_message", lambda t: {"category": Category.QUESTION, "summary": "", "due": ""})
    import app.domain.tasks as tasks_module_local

    monkeypatch.setattr(tasks_module_local, "extract_tasks", lambda text, mid: {"tasks": []})
    with session_scope() as session:
        draft = reply_engine.process_incoming(session, incoming("请问年假怎么申请", "om_empty_edit"), source=source)
        token = review_module.create_action_token(
            session, owner="ou_owner", action="edit", target_type="draft",
            target_id=draft.id, target_version=1,
        )
        action_id = token.action_id
    try:
        with session_scope() as session:
            review_module.handle_card_action(
                session, action_id=action_id, operator_id="ou_owner", edited_content=""
            )
        raise AssertionError("empty edit must be rejected")
    except review_module.ReviewError as exc:
        assert "empty_edit" in str(exc)


def test_report_card_complete_task_then_approve_draft(source, approval_mode, monkeypatch):
    _process_task(source, monkeypatch)
    monkeypatch.setattr(
        reply_engine, "classify_message",
        lambda t: {"category": Category.QUESTION, "summary": "", "due": ""},
    )
    import app.domain.tasks as tasks_module_local

    monkeypatch.setattr(tasks_module_local, "extract_tasks", lambda text, mid: {"tasks": []})
    with session_scope() as session:
        draft = reply_engine.process_incoming(
            session, incoming("请问年假怎么申请", "om_q"), source=source
        )
        draft_id = draft.id
    target = date(2026, 11, 5)
    report_date = target + timedelta(days=1)
    with session_scope() as session:
        reports.generate_report(session, logical_date=report_date)
        task_token = session.query(ActionToken).filter(ActionToken.action == "complete_task").one()
        approve_token = session.query(ActionToken).filter(ActionToken.action == "approve").one()
        task_action_id = task_token.action_id
        approve_action_id = approve_token.action_id
    with session_scope() as session:
        review_module.handle_card_action(
            session, action_id=task_action_id, operator_id="ou_owner"
        )
    with session_scope() as session:
        assert session.query(Task).one().status == TaskStatus.DONE
        review_module.handle_card_action(
            session, action_id=approve_action_id, operator_id="ou_owner",
            edited_content="修改后的回复",
        )
    transport = FakeTransport()
    reply_engine.dispatch_pending(transport=transport)
    with session_scope() as session:
        saved = (
            session.query(ReplyDraft)
            .filter(ReplyDraft.message_id == "om_q")
            .order_by(ReplyDraft.id.desc())
            .first()
        )
        assert saved.status == DraftStatus.SENT
        assert saved.version == 2
        assert saved.draft_content == "修改后的回复"
    assert transport.replies[0][1] == "修改后的回复"


def test_shanghai_logical_day_boundary():
    start, end = previous_day_window(date(2026, 11, 5), "Asia/Shanghai")
    expected_start = datetime(2026, 11, 4, 16, 0, tzinfo=ZoneInfo("UTC"))
    expected_end = datetime(2026, 11, 5, 16, 0, tzinfo=ZoneInfo("UTC"))
    assert start == int(expected_start.timestamp() * 1000)
    assert end == int(expected_end.timestamp() * 1000)


def test_sent_reply_is_attributed_by_receipt_time_in_utc():
    from app.core.clock import from_epoch_ms
    from app.models import OutboxStatus

    the_day = date(2026, 11, 5)
    start_ms, end_ms = previous_day_window(the_day, "Asia/Shanghai")
    with session_scope() as session:
        for index, sent_ms in enumerate((start_ms + 60_000, start_ms - 60_000, end_ms - 60_000)):
            session.add(
                Outbox(
                    intent_key=f"receipt-time-{index}", tenant="default", kind="reply",
                    chat_id="oc_1", target_type="message", target_id=f"om_{index}",
                    payload_json='{"text":"x"}', status=OutboxStatus.SENT,
                    sent_at=from_epoch_ms(sent_ms),
                )
            )
        session.flush()
        report = reports.generate_report(
            session, logical_date=the_day + timedelta(days=1), enqueue_delivery=False
        )
        content = json.loads(report.report.content_json)
        assert len(content["sent_replies"]) == 2


def test_daily_pipeline_regenerates_latest_report_for_late_data(settings):
    from app.pipeline.daily import run_daily_pipeline
    from tests.helpers import FakeTransport

    today = date.today()
    with session_scope() as session:
        first = reports.generate_report(session, logical_date=today, enqueue_delivery=False)
        assert first.report.revision == 1
        session.add(
            Task(
                task_key="late-pipeline-task", tenant="default", owner="ou_owner",
                chat_id="oc_1", thread_id="", title="迟到任务", action="迟到任务",
                assignee_id="ou_owner", assignee_name="owner",
                status=TaskStatus.OPEN, needs_confirmation=False,
                evidence_json="[]", source_message_ids="[]",
            )
        )
        session.flush()
    run_daily_pipeline(report_date=today, transport=FakeTransport(chats=[]))
    with session_scope() as session:
        report = session.query(DailyReport).filter(DailyReport.logical_date == today.isoformat()).one()
        assert report.revision == 2


def test_downtime_catch_up_generates_missing_reports():
    target = date(2026, 11, 3)
    with session_scope() as session:
        reports.generate_report(session, logical_date=target)
        # 模拟停机两天后恢复：由外部循环补跑缺失日期
        for offset in (1, 2):
            reports.generate_report(session, logical_date=target + timedelta(days=offset))
        dates = [row.logical_date for row in session.query(DailyReport).order_by(DailyReport.logical_date)]
        assert dates == ["2026-11-03", "2026-11-04", "2026-11-05"]
