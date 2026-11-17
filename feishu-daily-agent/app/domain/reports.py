"""日报：逻辑日窗口、同日幂等、跨天待办、缺口与补记。"""
from __future__ import annotations

import json
import re
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


def _resolve_owner_open_id(session: Session) -> str:
    """只配 chat_id 时，用首条消息学到的 owner open_id（卡片令牌按它绑定）。"""
    try:
        from app.services.owner_bootstrap import resolve_owner_open_id

        return resolve_owner_open_id(session)
    except Exception:  # noqa: BLE001 — 解析失败不能阻断日报生成
        return ""


def _message_map(session: Session, message_ids: list[str]) -> dict[str, Message]:
    if not message_ids:
        return {}
    return {
        message.message_id: message
        for message in session.scalars(
            select(Message).where(Message.message_id.in_(message_ids))
        )
    }


_OPEN_ID_RE = re.compile(r"^(ou_|on_|oc_)[A-Za-z0-9]+$")


def mask_sender(value: str) -> str:
    """日报展示层脱敏：open_id/长串只留前缀，人名原样返回。

    飞书 open_id 对使用者没有可读性，直接进日报既刺眼又泄露身份；
    保留前 6 位足以在需要时定位，但不暴露完整标识。
    """
    text = (value or "").strip()
    if not text:
        return ""
    if _OPEN_ID_RE.match(text) or (len(text) >= 16 and " " not in text):
        return text[:6] + "***"
    return text


def _question_key(text: str) -> str:
    """只归一空白、大小写和句末标点，保留完整问题与运算符。"""
    return re.sub(r"\s+", "", (text or "").casefold()).rstrip("?？!！。.")


def cluster_shadow_drafts(items: list[dict]) -> list[dict]:
    """影子草稿按问题聚类去重：同题只保留代表条目，附 duplicate_count。

    影子只记录决策、不生成发送，重复条目对使用者没有信息量；
    待批（非影子）草稿仍需逐条决策，不参与聚类。
    """
    kept: list[dict] = []
    seen: dict[str, dict] = {}
    for item in items:
        if not item.get("shadow"):
            kept.append(item)
            continue
        key = _question_key(item.get("question") or item.get("content") or "")
        if not key:
            kept.append(item)
            continue
        rep = seen.get(key)
        if rep is None:
            item["duplicate_count"] = 1
            seen[key] = item
            kept.append(item)
        else:
            rep["duplicate_count"] = int(rep.get("duplicate_count") or 1) + 1
            rep["score"] = max(float(rep.get("score") or 0),
                               float(item.get("score") or 0))
    return kept


def _task_items(session: Session, tenant: str, owner: str) -> list[dict]:
    from evoharness.work.state import ACTIVE
    tasks = [
        task
        for task in tasks_module.open_tasks(session, tenant=tenant, owner=owner)
        if task.status in ACTIVE
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
                        "sender": mask_sender(message.sender_name or message.sender_id),
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


def _has_content(content: dict) -> bool:
    """日报是否有信息量。

    只有"确实有事要做/要看"才推送：待办、待批、昨日已发、经验变更任一非空。
    采集缺口（gaps）不算——那是运维信息，不该把用户变成运维。
    """
    return bool(
        content.get("tasks")
        or content.get("pending_replies")
        or content.get("sent_replies")
        or content.get("experience_changes")
    )


def _pending_drafts(
    session: Session, tenant: str, *, owner: str = "", is_admin: bool = False
) -> list[dict]:
    """待批草稿。

    多用户分流：管理员看全租户；普通使用者只看**自己会话里**的草稿。
    不这么分的话，每个人都会收到别人的待批回复——既是噪音也是泄露。
    """
    query = select(ReplyDraft).where(
        ReplyDraft.tenant == tenant,
        ReplyDraft.status == DraftStatus.PENDING,
        ReplyDraft.mode.in_([DraftMode.DRAFT, DraftMode.SHADOW]),
    )
    if not is_admin and owner:
        # 用该使用者的 p2p 会话过滤（草稿自身只记 chat_id）
        chat_ids = [
            chat_id
            for (chat_id,) in session.execute(
                select(Source.chat_id).where(
                    Source.tenant == tenant,
                    Source.owner == owner,
                    Source.source_type == "p2p",
                )
            ).all()
        ]
        if not chat_ids:
            return []
        query = query.where(ReplyDraft.chat_id.in_(chat_ids))
    drafts = list(session.scalars(query.order_by(ReplyDraft.id)))
    messages = _message_map(session, [d.message_id for d in drafts])
    items = []
    for draft in drafts:
        message = messages.get(draft.message_id)
        items.append(
            {
                "id": draft.id,
                "version": draft.version,
                "question": message.content_text if message else "",
                "sender": mask_sender(message.sender_name or message.sender_id) if message else "",
                "content": draft.draft_content,
                "score": round(float(draft.score or 0), 3),
                "experience_id": draft.experience_id,
                "mode": draft.mode,
                "shadow": draft.shadow,
                "reasons": [item for item in (draft.note or "").split(",") if item],
            }
        )
    return cluster_shadow_drafts(items)


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
                    "chat_id": mask_sender(source.chat_id),
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
    # 同类投递异常聚合成一类（kind/status/error 相同），避免日报被同一故障刷屏
    delivery: dict[tuple, dict] = {}
    for row in unresolved:
        key = (row.kind, row.status, (row.last_error or "")[:120])
        agg = delivery.get(key)
        if agg is None:
            delivery[key] = {
                "delivery": row.kind,
                "target": mask_sender(row.target_id),
                "status": row.status,
                "error": row.last_error,
                "count": 1,
            }
        else:
            agg["count"] += 1
    return gaps + list(delivery.values())


def build_content(
    session: Session,
    *,
    tenant: str,
    owner: str,
    target_date: date,
    timezone: str,
    is_admin: bool = True,
) -> tuple[dict, dict]:
    start_ms, end_ms = previous_day_window(target_date, timezone)
    content = {
        "date": target_date.isoformat(),
        "timezone": timezone,
        "window": {"start_ms": start_ms, "end_ms": end_ms},
        "tasks": _task_items(session, tenant, owner),
        "pending_replies": _pending_drafts(
            session, tenant, owner=owner, is_admin=is_admin
        ),
        "sent_replies": _sent_drafts(session, tenant, start_ms, end_ms),
        "experience_changes": _experience_changes(session, tenant, start_ms, end_ms),
        "gaps": _coverage(session, tenant),
    }
    content["summary"] = {
        "tasks": len(content["tasks"]),
        "pending": sum(1 for item in content["pending_replies"] if not item.get("shadow")),
        # 影子按同题聚类后的条数；duplicates 记录被收拢的重复量（展示负责任的去重量）
        "shadow_pending": sum(1 for item in content["pending_replies"] if item.get("shadow")),
        "shadow_duplicates": sum(
            int(item.get("duplicate_count") or 1) - 1
            for item in content["pending_replies"] if item.get("shadow")
        ),
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
    delivery_target: tuple[str, str] | None = None,
) -> ReportResult:
    settings = get_settings()
    tenant = tenant or settings.tenant_id
    if owner is None:
        _type, target_id = settings.owner_target()
        owner = (
            _resolve_owner_open_id(session)
            or target_id
            or settings.feishu_owner_open_id
        )
    timezone = timezone or settings.daily_report_timezone
    # 投递目标与 owner 身份解耦：open_id 可能在本应用命名空间未知，
    # 但 chat_id / 邮箱仍然可用（2026-11-10 接入实机发现）。
    # 多用户场景由调用方显式传入，避免全租户报告都发给 owner 一个人。
    if delivery_target:
        target_type, target_id = delivery_target
    else:
        target_type, target_id = settings.owner_target()
    target_id = target_id or owner
    logical_date = logical_date or logical_date_for(utcnow(), timezone)
    target_date = logical_date - timedelta(days=1)
    key = report_key(tenant, owner, logical_date, timezone)
    # 管理员看全局台账，普通使用者只看与自己相关的条目（见 _pending_drafts）。
    # owner 为空时按管理员处理：空 owner 只出现在"配置不全/系统级生成"的场景，
    # 此时不给任何待批会让报告永远为空（2026-11-11 实测）。
    admin_id = (settings.feishu_owner_open_id or "").strip()
    is_admin = not owner or (admin_id and owner == admin_id)
    content, snapshot = build_content(
        session,
        tenant=tenant,
        owner=owner,
        target_date=target_date,
        timezone=timezone,
        is_admin=is_admin,
    )
    content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
    content_hash = sha256_text(content_json)
    report = session.scalar(select(DailyReport).where(DailyReport.report_key == key))
    if report is None:
        # 空日报也照发：用户要靠它确认"机器人还在正常工作"
        # （2026-11-11 用户明确：无内容也得发，否则无从判断服务是否活着）。
        # 卡片里显示"今天没有需要你处理的事"，本身就是有效的状态信号。
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
                chat_id=target_id if target_type == "chat_id" else "",
                target_type=target_type,
                target_id=target_id,
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
            chat_id=target_id if target_type == "chat_id" else "",
            target_type=target_type,
            target_id=target_id,
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
