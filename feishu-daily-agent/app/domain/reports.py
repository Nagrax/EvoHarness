"""日报：逻辑日窗口、同日幂等、跨天待办、缺口与补记。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import from_epoch_ms, logical_date_for, previous_day_window, utcnow
from app.core.config import get_settings
from app.core.ids import sha256_json, sha256_text
from app.domain import outbox as outbox_module
from app.domain import tasks as tasks_module
from app.models import (
    DailyReport,
    DraftMode,
    DraftStatus,
    Experience,
    ExperienceEvent,
    ExperienceStatus,
    Message,
    Outbox,
    OutboxStatus,
    ReplyDraft,
    Source,
    SourceSync,
    TaskStatus,
)


@dataclass
class ReportResult:
    report: DailyReport
    created: bool
    revised: bool
    outbox_id: int | None


def report_key(tenant: str, owner: str, logical_date: date, tz: str) -> str:
    return f"{tenant}|{owner}|{logical_date.isoformat()}|{tz}"


def _message_map(session: Session, message_ids: list[str]) -> dict[str, Message]:
    if not message_ids:
        return {}
    return {
        message.message_id: message
        for message in session.scalars(
            select(Message).where(Message.message_id.in_(message_ids))
        )
    }


def _task_items(session: Session, tenant: str, owner: str) -> list[dict]:
    tasks = [
        task
        for task in tasks_module.open_tasks(session, tenant=tenant, owner=owner)
        if task.status in {TaskStatus.OPEN, TaskStatus.NEEDS_CONFIRMATION}
    ]
    messages = _message_map(
        session, [mid for task in tasks for mid in task.source_ids()]
    )
    items = []
    for task in tasks:
        sources = []
        for message_id in task.source_ids():
            message = messages.get(message_id)
            if message:
                sources.append(
                    {
                        "message_id": message.message_id,
                        "chat_id": message.chat_id,
                        "sender": message.sender_name or message.sender_id,
                        "text": (message.content_text or "")[:200],
                        "create_time": message.create_time,
                    }
                )
        items.append(
            {
                "id": task.id,
                "title": task.title,
                "status": task.status,
                "needs_confirmation": task.needs_confirmation,
                "assignee": task.assignee_name,
                "due": task.due_text,
                "sources": sources,
            }
        )
    return items


def _pending_drafts(session: Session, tenant: str) -> list[dict]:
    drafts = list(
        session.scalars(
            select(ReplyDraft)
            .where(
                ReplyDraft.tenant == tenant,
                ReplyDraft.status == DraftStatus.PENDING,
                ReplyDraft.mode.in_([DraftMode.DRAFT, DraftMode.SHADOW]),
            )
            .order_by(ReplyDraft.id)
        )
    )
    messages = _message_map(session, [d.message_id for d in drafts])
    items = []
    for draft in drafts:
        message = messages.get(draft.message_id)
        items.append(
            {
                "id": draft.id,
                "version": draft.version,
                "question": message.content_text if message else "",
                "sender": (message.sender_name or message.sender_id) if message else "",
                "content": draft.draft_content,
                "score": round(float(draft.score or 0), 3),
                "experience_id": draft.experience_id,
                "mode": draft.mode,
                "shadow": draft.shadow,
                "reasons": [item for item in (draft.note or "").split(",") if item],
            }
        )
    return items


def _sent_drafts(session: Session, tenant: str, start_ms: int, end_ms: int) -> list[dict]:
    # 按真实回执时间归属；发信意图可能延迟到下一个窗口，不能在审批时间上凑数。
    rows = list(
        session.scalars(
            select(Outbox).where(
                Outbox.tenant == tenant,
                Outbox.kind.in_(["reply", "correction"]),
                Outbox.status == OutboxStatus.SENT,
                Outbox.sent_at.is_not(None),
                Outbox.sent_at >= from_epoch_ms(start_ms),
                Outbox.sent_at < from_epoch_ms(end_ms),
            ).order_by(Outbox.sent_at, Outbox.id)
        )
    )
    items = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except ValueError:
            payload = {}
        draft = session.get(ReplyDraft, int(payload.get("draft_id") or 0))
        message = session.scalar(
            select(Message).where(Message.message_id == row.target_id)
        ) if row.target_id else None
        items.append(
            {
                "id": draft.id if draft else row.id,
                "question": message.content_text if message else "",
                "content": (draft.draft_content if draft else payload.get("text", "")),
                "receipt": row.receipt_message_id or (draft.sent_message_id if draft else ""),
                "mode": draft.mode if draft else "reply",
                "sent_at": row.sent_at.isoformat() if row.sent_at else "",
            }
        )
    return items


def _experience_changes(session: Session, tenant: str, start_ms: int, end_ms: int) -> list[dict]:
    start = from_epoch_ms(start_ms)
    end = from_epoch_ms(end_ms)
    events = list(
        session.scalars(
            select(ExperienceEvent).where(
                ExperienceEvent.created_at >= start,
                ExperienceEvent.created_at < end,
            ).order_by(ExperienceEvent.id)
        )
    )
    items = []
    for event in events:
        experience = session.get(Experience, event.experience_id)
        if experience is None or experience.tenant != tenant:
            continue
        items.append(
            {
                "experience_id": experience.id,
                "intent_id": experience.intent_id,
                "version": experience.version,
                "status": experience.status,
                "action": event.action,
                "question": experience.question_pattern,
                "actor": event.actor,
            }
        )
    return items


def _coverage(session: Session, tenant: str) -> list[dict]:
    sources = list(session.scalars(select(Source).where(Source.tenant == tenant)))
    gaps = []
    for source in sources:
        if source.coverage_status != "complete" or source.revoked_at is not None:
            last_sync = session.scalar(
                select(SourceSync)
                .where(SourceSync.source_id == source.id)
                .order_by(SourceSync.updated_at.desc())
            )
            gaps.append(
                {
                    "chat_id": source.chat_id,
                    "thread_id": source.thread_id,
                    "status": source.coverage_status,
                    "revoked": source.revoked_at is not None,
                    "last_success_watermark_ms": source.last_success_watermark_ms,
                    "error": last_sync.error_detail if last_sync else "",
                }
            )
    unresolved = list(
        session.scalars(
            select(Outbox).where(
                Outbox.tenant == tenant,
                Outbox.status.in_(
                    [OutboxStatus.DELIVERY_UNKNOWN, OutboxStatus.FAILED, OutboxStatus.RETRY_WAIT]
                ),
            )
        )
    )
    return gaps + [
        {
            "delivery": row.kind,
            "target": row.target_id,
            "status": row.status,
            "error": row.last_error,
        }
        for row in unresolved
    ]


def build_content(
    session: Session,
    *,
    tenant: str,
    owner: str,
    target_date: date,
    timezone: str,
) -> tuple[dict, dict]:
    start_ms, end_ms = previous_day_window(target_date, timezone)
    content = {
        "date": target_date.isoformat(),
        "timezone": timezone,
        "window": {"start_ms": start_ms, "end_ms": end_ms},
        "tasks": _task_items(session, tenant, owner),
        "pending_replies": _pending_drafts(session, tenant),
        "sent_replies": _sent_drafts(session, tenant, start_ms, end_ms),
        "experience_changes": _experience_changes(session, tenant, start_ms, end_ms),
        "gaps": _coverage(session, tenant),
    }
    content["summary"] = {
        "tasks": len(content["tasks"]),
        "pending": sum(1 for item in content["pending_replies"] if not item.get("shadow")),
        "shadow_pending": sum(1 for item in content["pending_replies"] if item.get("shadow")),
        "sent": len(content["sent_replies"]),
        "experience_changes": len(content["experience_changes"]),
        "gaps": len(content["gaps"]),
    }
    snapshot = {
        "task_ids": [item["id"] for item in content["tasks"]],
        "pending_ids": [item["id"] for item in content["pending_replies"]],
        "sent_ids": [item["id"] for item in content["sent_replies"]],
        "experience_events": [
            f"{item['experience_id']}:{item['action']}" for item in content["experience_changes"]
        ],
        "gaps": len(content["gaps"]),
    }
    return content, snapshot


def generate_report(
    session: Session,
    *,
    logical_date: date | None = None,
    tenant: str | None = None,
    owner: str | None = None,
    timezone: str | None = None,
    enqueue_delivery: bool = True,
) -> ReportResult:
    settings = get_settings()
    tenant = tenant or settings.tenant_id
    owner = owner or settings.feishu_owner_open_id
    timezone = timezone or settings.daily_report_timezone
    logical_date = logical_date or logical_date_for(utcnow(), timezone)
    target_date = logical_date - timedelta(days=1)
    key = report_key(tenant, owner, logical_date, timezone)
    content, snapshot = build_content(
        session, tenant=tenant, owner=owner, target_date=target_date, timezone=timezone
    )
    content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
    content_hash = sha256_text(content_json)
    report = session.scalar(select(DailyReport).where(DailyReport.report_key == key))
    if report is None:
        report = DailyReport(
            report_key=key,
            tenant=tenant,
            owner=owner,
            logical_date=logical_date.isoformat(),
            timezone=timezone,
            window_start_ms=content["window"]["start_ms"],
            window_end_ms=content["window"]["end_ms"],
            content_json=content_json,
            content_hash=content_hash,
            snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            status="draft",
            revision=1,
        )
        session.add(report)
        session.flush()
        intent = outbox_module.intent_key(tenant, owner, "daily", logical_date.isoformat(), "v1")
        row = None
        if enqueue_delivery:
            from app.feishu.cards import build_report_card

            card = build_report_card(session, report)
            row = outbox_module.enqueue(
                session,
                intent=intent,
                tenant=tenant,
                kind="daily",
                chat_id="",
                target_type="open_id",
                target_id=owner,
                payload={
                    "report_id": report.id,
                    "date": logical_date.isoformat(),
                    "card": card,
                },
                policy_version=settings.policy_version,
            )
            report.delivery_intent_key = intent
        return ReportResult(report, True, False, row.id if row else None)

    if report.content_hash == content_hash:
        return ReportResult(report, False, False, None)

    report.revision = int(report.revision or 1) + 1
    report.content_json = content_json
    report.content_hash = content_hash
    report.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    row = None
    if enqueue_delivery:
        intent = outbox_module.intent_key(
            tenant, owner, "daily", logical_date.isoformat(), f"revision:{report.revision}"
        )
        from app.feishu.cards import build_report_card

        card = build_report_card(session, report)
        row = outbox_module.enqueue(
            session,
            intent=intent,
            tenant=tenant,
            kind="daily",
            chat_id="",
            target_type="open_id",
            target_id=owner,
            payload={
                "report_id": report.id,
                "date": logical_date.isoformat(),
                "revision": report.revision,
                "late_data": True,
                "card": card,
            },
            policy_version=settings.policy_version,
        )
        report.delivery_intent_key = intent
    session.flush()
    return ReportResult(report, False, True, row.id if row else None)


def build_report_card(session: Session, report: DailyReport) -> dict:
    from app.feishu.cards import build_report_card as _build

    return _build(session, report)
