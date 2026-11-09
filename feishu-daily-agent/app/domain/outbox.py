"""事务内入队、提交后发送、回执与恢复。

关键顺序：业务状态与 outbox 同一事务提交；发送发生在提交之后；
只有真实回执才能把状态标为 sent。超时等模糊结果停在 delivery_unknown，
等待对账，禁止盲目重发。
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.ids import new_claim_token, sha256_json, sha256_text
from app.feishu.transport import IDEMPOTENCY_WINDOW_SECONDS, TransportResult, get_transport
from app.models import (
    DailyReport,
    Experience,
    ExperienceStatus,
    Message,
    Outbox,
    OutboxStatus,
    ReplyDraft,
    DraftStatus,
    Source,
)

MAX_ATTEMPTS = 5
LEASE_SECONDS = 120


@dataclass
class DispatchResult:
    outbox_id: int
    status: str
    receipt_id: str = ""
    error: str = ""


def intent_key(*parts: str) -> str:
    return "|".join(str(part) for part in parts)


def enqueue(
    session: Session,
    *,
    intent: str,
    tenant: str,
    kind: str,
    target_type: str,
    target_id: str,
    payload: dict,
    chat_id: str = "",
    policy_version: str = "",
    experience_id: int | None = None,
    source_id: int | None = None,
    idempotency_key: str = "",
    next_at: datetime | None = None,
) -> Outbox:
    existing = session.scalar(select(Outbox).where(Outbox.intent_key == intent))
    if existing is not None:
        return existing
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    row = Outbox(
        intent_key=intent,
        tenant=tenant,
        kind=kind,
        chat_id=chat_id,
        target_type=target_type,
        target_id=target_id,
        payload_json=payload_json,
        payload_hash=sha256_text(payload_json),
        policy_version=policy_version,
        experience_id=experience_id,
        source_id=source_id,
        auto=bool(payload.get("auto")),
        idempotency_key=idempotency_key or f"outbox-{sha256_text(intent)[:40]}",
        next_at=next_at or utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _precheck(session: Session, row: Outbox) -> tuple[bool, str]:
    settings = get_settings()
    if row.kind in {"reply", "correction"}:
        try:
            payload = json.loads(row.payload_json)
        except ValueError:
            payload = {}
        draft_id = int(payload.get("draft_id") or 0)
        draft = session.get(ReplyDraft, draft_id) if draft_id else None
        if draft is not None and draft.status in {
            DraftStatus.REJECTED,
            DraftStatus.CANCELLED,
            DraftStatus.SUPERSEDED,
        }:
            return False, "draft_not_approved"
        if draft is None and draft_id:
            return False, "draft_missing"
        message = session.scalar(
            select(Message).where(Message.message_id == row.target_id)
        )
        if message is not None and message.recalled:
            return False, "message_recalled"
        if row.source_id is not None:
            source = session.get(Source, row.source_id)
            if source is None or source.revoked_at is not None or not source.enabled:
                return False, "source_revoked"
            if not source.reply_allowed or not settings.reply_allowed_for(source.chat_id):
                return False, "reply_permission_missing"
            if not settings.business_reply_enabled:
                return False, "business_reply_disabled"
        if row.experience_id is not None:
            experience = session.get(Experience, row.experience_id)
            if experience is None:
                return False, "experience_missing"
            if experience.status not in {
                ExperienceStatus.APPROVED_AUTO,
                ExperienceStatus.ASSIST,
                ExperienceStatus.CANDIDATE,
            }:
                return False, "experience_not_available"
            if experience.valid_until is not None and experience.valid_until <= utcnow():
                return False, "experience_expired"
        try:
            payload = json.loads(row.payload_json)
        except ValueError:
            payload = {}
        if payload.get("auto") and not settings.auto_reply_enabled:
            return False, "auto_reply_paused"
    if not settings.writes_enabled and row.kind != "daily":
        return False, "writes_disabled"
    return True, ""


def claim_next(session: Session, *, now: datetime | None = None) -> Outbox | None:
    now = now or utcnow()
    candidate = session.scalar(
        select(Outbox)
        .where(
            Outbox.status.in_([OutboxStatus.QUEUED, OutboxStatus.RETRY_WAIT]),
            Outbox.next_at <= now,
        )
        .order_by(Outbox.next_at, Outbox.id)
        .limit(1)
    )
    if candidate is None:
        return None
    token = new_claim_token()
    lease = now + timedelta(seconds=LEASE_SECONDS)
    result = session.execute(
        update(Outbox)
        .where(
            Outbox.id == candidate.id,
            Outbox.status.in_([OutboxStatus.QUEUED, OutboxStatus.RETRY_WAIT]),
            Outbox.attempts == candidate.attempts,
        )
        .values(
            status=OutboxStatus.SENDING,
            claim_token=token,
            lease_until=lease,
            attempts=candidate.attempts + 1,
            updated_at=now,
        )
    )
    session.flush()
    if result.rowcount != 1:
        return None
    session.refresh(candidate)
    return candidate


def _complete(
    session: Session, row: Outbox, token: str, values: dict
) -> bool:
    result = session.execute(
        update(Outbox)
        .where(
            Outbox.id == row.id,
            Outbox.status == OutboxStatus.SENDING,
            Outbox.claim_token == token,
        )
        .values(**values)
    )
    session.flush()
    return result.rowcount == 1


def mark_sent(
    session: Session, row: Outbox, *, token: str, receipt_id: str, now: datetime | None = None
) -> bool:
    now = now or utcnow()
    ok = _complete(
        session,
        row,
        token,
        {
            "status": OutboxStatus.SENT,
            "receipt_message_id": receipt_id,
            "sent_at": now,
            "claim_token": "",
            "lease_until": None,
            "last_error": "",
            "updated_at": now,
        },
    )
    if ok:
        if row.kind in {"reply", "correction"}:
            try:
                draft_id = int(json.loads(row.payload_json).get("draft_id") or 0)
            except (TypeError, ValueError):
                draft_id = 0
            draft = session.get(ReplyDraft, draft_id) if draft_id else None
            if draft is not None and draft.status in {DraftStatus.PENDING, DraftStatus.APPROVED}:
                draft.status = DraftStatus.SENT
                draft.sent_message_id = receipt_id
        elif row.kind == "daily":
            report = session.scalar(
                select(DailyReport).where(DailyReport.delivery_intent_key == row.intent_key)
            )
            if report is not None:
                report.status = "sent"
                report.delivered_at = now
    return ok


def mark_failure(
    session: Session,
    row: Outbox,
    *,
    token: str,
    result: TransportResult,
    now: datetime | None = None,
    jitter: Callable[[], float] | None = None,
) -> bool:
    now = now or utcnow()
    if result.outcome == "unknown":
        return _complete(
            session,
            row,
            token,
            {
                "status": OutboxStatus.DELIVERY_UNKNOWN,
                "last_error": result.error[:2000],
                "claim_token": "",
                "lease_until": None,
                "updated_at": now,
            },
        )
    if result.outcome == "retryable" and row.attempts < MAX_ATTEMPTS:
        rand = jitter or (lambda: random.uniform(0, 1))
        base = result.retry_after or min(60.0, 2.0 ** max(0, row.attempts))
        delay = base + rand() * min(10.0, max(1.0, base / 2))
        return _complete(
            session,
            row,
            token,
            {
                "status": OutboxStatus.RETRY_WAIT,
                "next_at": now + timedelta(seconds=delay),
                "last_error": result.error[:2000],
                "claim_token": "",
                "lease_until": None,
                "updated_at": now,
            },
        )
    ok = _complete(
        session,
        row,
        token,
        {
            "status": OutboxStatus.FAILED,
            "last_error": result.error[:2000],
            "claim_token": "",
            "lease_until": None,
            "updated_at": now,
        },
    )
    if ok and row.kind in {"reply", "correction"}:
        try:
            draft_id = int(json.loads(row.payload_json).get("draft_id") or 0)
        except (TypeError, ValueError):
            draft_id = 0
        draft = session.get(ReplyDraft, draft_id) if draft_id else None
        if draft is not None and draft.status == DraftStatus.APPROVED:
            draft.status = DraftStatus.FAILED
            draft.note = result.error[:500]
    return ok


def recover_expired_leases(
    session: Session, *, now: datetime | None = None
) -> list[int]:
    now = now or utcnow()
    recovered: list[int] = []
    settings = get_settings()
    for row in session.scalars(
        select(Outbox).where(
            Outbox.status == OutboxStatus.SENDING,
            Outbox.lease_until.is_not(None),
            Outbox.lease_until < now,
        )
    ):
        if settings.deployment_mode == "offline":
            new_status = OutboxStatus.DELIVERY_UNKNOWN
        else:
            age = (now - row.updated_at).total_seconds()
            new_status = (
                OutboxStatus.QUEUED
                if age <= IDEMPOTENCY_WINDOW_SECONDS
                else OutboxStatus.DELIVERY_UNKNOWN
            )
        row.status = new_status
        row.last_error = "worker lease expired"
        row.claim_token = ""
        row.lease_until = None
        row.updated_at = now
        recovered.append(row.id)
    session.flush()
    return recovered


def dispatch_outbox(
    session_factory,
    *,
    transport=None,
    limit: int = 20,
    now: datetime | None = None,
) -> list[DispatchResult]:
    """领取→发送→落回执。每个阶段独立事务，网络调用期间不持有写事务。"""
    transport = transport or get_transport()
    results: list[DispatchResult] = []
    for _ in range(limit):
        session = session_factory()
        try:
            recovered = recover_expired_leases(session)
            session.commit()
            row = claim_next(session, now=now)
            if row is None:
                session.commit()
                break
            token = row.claim_token
            ok, reason = _precheck(session, row)
            if not ok:
                row.status = OutboxStatus.CANCELLED
                row.last_error = f"precheck:{reason}"
                row.claim_token = ""
                row.lease_until = None
                session.commit()
                results.append(DispatchResult(row.id, OutboxStatus.CANCELLED, error=reason))
                continue
            payload = json.loads(row.payload_json)
            kind = row.kind
            target = row.target_id
            idem = row.idempotency_key
            session.commit()
        finally:
            session.close()

        if kind in {"reply", "correction"}:
            if payload.get("card"):
                result = transport.reply_card(
                    target, payload["card"], idem, bool(payload.get("reply_in_thread"))
                )
            else:
                result = transport.reply_text(
                    target, payload.get("text", ""), idem, bool(payload.get("reply_in_thread"))
                )
        elif kind == "daily":
            result = transport.send_card(target, payload.get("card", {}), idem)
        elif kind == "notice":
            result = transport.send_text(target, payload.get("text", ""), idem)
        else:
            result = TransportResult("permanent", error=f"unknown_kind:{kind}")

        session = session_factory()
        try:
            row = session.get(Outbox, row.id)
            if row is None:
                continue
            if result.outcome == "sent" and not result.receipt_id:
                result = TransportResult("unknown", error="sent_without_receipt")
            if result.outcome == "sent" and result.receipt_id:
                mark_sent(session, row, token=token, receipt_id=result.receipt_id, now=now)
                status = OutboxStatus.SENT
            else:
                mark_failure(session, row, token=token, result=result, now=now)
                status = row.status
            session.commit()
            results.append(
                DispatchResult(row.id, status, receipt_id=result.receipt_id, error=result.error)
            )
        finally:
            session.close()
    return results


def list_unresolved(session: Session, *, tenant: str | None = None) -> list[Outbox]:
    query = select(Outbox).where(
        Outbox.status.in_(
            [OutboxStatus.DELIVERY_UNKNOWN, OutboxStatus.FAILED, OutboxStatus.RETRY_WAIT]
        )
    )
    if tenant:
        query = query.where(Outbox.tenant == tenant)
    return list(session.scalars(query.order_by(Outbox.updated_at.desc())))


def reconcile_unknown(
    session: Session, outbox_id: int, *, receipt_id: str, actor: str
) -> Outbox | None:
    """人工对账：只有拿到可核对的远端 message_id 才能标记 sent。"""
    row = session.get(Outbox, outbox_id)
    if row is None or row.status != OutboxStatus.DELIVERY_UNKNOWN:
        return None
    if not receipt_id:
        return None
    row.status = OutboxStatus.SENT
    row.receipt_message_id = receipt_id
    row.sent_at = utcnow()
    row.last_error = f"reconciled by {actor}"
    session.flush()
    return row
