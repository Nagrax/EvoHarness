"""事务内入队、提交后发送、回执与恢复。

关键顺序：业务状态与 outbox 同一事务提交；发送发生在提交之后；
只有真实回执才能把状态标为 sent。超时等模糊结果停在 delivery_unknown，
等待对账，禁止盲目重发。
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.governance import decisions as _governance
from evoharness.ids import new_claim_token, sha256_json, sha256_text
from evoharness.ports import IDEMPOTENCY_WINDOW_SECONDS, TransportResult
logger = logging.getLogger(__name__)

from evoharness.models import (
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
# R09：waiting_authorization 的重试间隔。token 恢复/重新批准后由 worker
# 按 next_at 自动重试（沿用既有重试/对账语义，不丢意图）。
WAITING_AUTHORIZATION_BACKOFF_SECONDS = 300


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
    delivery_mode: str = "live",
    next_at: datetime | None = None,
) -> Outbox:
    if delivery_mode not in {"live", "dryrun"}:
        raise ValueError(f"invalid_delivery_mode:{delivery_mode}")
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
        delivery_mode=delivery_mode,
        next_at=next_at or utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _precheck(session: Session, row: Outbox, *, policy=None) -> tuple[bool, str]:
    if policy is None:
        raise ValueError("policy_adapter_required")
    settings = policy
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


def claim_next(session: Session, *, now: datetime | None = None, outbox_id: int | None = None) -> Outbox | None:
    now = now or utcnow()
    query = select(Outbox)
    if outbox_id is not None:
        query = query.where(Outbox.id == outbox_id)
    candidate = session.scalar(
        query
        .where(
            Outbox.status.in_(
                [OutboxStatus.QUEUED, OutboxStatus.RETRY_WAIT, OutboxStatus.WAITING_AUTHORIZATION]
            ),
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
            Outbox.status.in_(
                [OutboxStatus.QUEUED, OutboxStatus.RETRY_WAIT, OutboxStatus.WAITING_AUTHORIZATION]
            ),
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
    session: Session, row: Outbox, *, token: str, receipt_id: str,
    now: datetime | None = None, identity: str = "",
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
            # R09 回执对账：记录真实发送身份；fallback 用 bot 发出时不得伪装本人
            "sent_identity": str(identity or "")[:16],
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


def mark_waiting_authorization(
    session: Session, row: Outbox, *, token: str, reason: str,
    now: datetime | None = None,
) -> bool:
    """授权/身份缺口（R09）：意图转 waiting_authorization，不丢意图。

    last_error 记录具体缺失（token_unavailable / decision_payload_changed /
    decision_expired / action_not_in_decision / decision_grantee_mismatch…）；
    next_at 退避，token 恢复或重新批准后 worker 自动重试；draft 保持 APPROVED。
    """
    now = now or utcnow()
    return _complete(
        session,
        row,
        token,
        {
            "status": OutboxStatus.WAITING_AUTHORIZATION,
            "last_error": f"waiting_authorization:{reason}"[:2000],
            "next_at": now + timedelta(seconds=WAITING_AUTHORIZATION_BACKOFF_SECONDS),
            "claim_token": "",
            "lease_until": None,
            "updated_at": now,
        },
    )


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
    session: Session, *, now: datetime | None = None, policy=None
) -> list[int]:
    now = now or utcnow()
    recovered: list[int] = []
    if policy is None:
        raise ValueError("policy_adapter_required")
    settings = policy
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
    outbox_id: int | None = None,
    policy=None, precheck=None, user_token_resolver=None,
) -> list[DispatchResult]:
    """领取→发送→落回执。每个阶段独立事务，网络调用期间不持有写事务。"""
    if transport is None:
        raise ValueError("transport_adapter_required")
    precheck = precheck or (lambda session, row: _precheck(session, row, policy=policy))
    results: list[DispatchResult] = []
    for _ in range(limit):
        session = session_factory()
        try:
            recovered = recover_expired_leases(session, policy=policy)
            session.commit()
            row = claim_next(session, now=now, outbox_id=outbox_id)
            if row is None:
                session.commit()
                break
            token = row.claim_token
            ok, reason = precheck(session, row)
            if not ok:
                row.status = OutboxStatus.CANCELLED
                row.last_error = f"precheck:{reason}"
                row.claim_token = ""
                row.lease_until = None
                session.commit()
                results.append(DispatchResult(row.id, OutboxStatus.CANCELLED, error=reason))
                continue
            # BP-02 dryrun 边界：传输层拒绝 dryrun 意图——不调 transport、
            # 不产生真实远端副作用；意图留痕取消 + 具体原因。
            if (row.delivery_mode or "live") == "dryrun":
                row.status = OutboxStatus.CANCELLED
                row.last_error = "dryrun_intent_blocked"
                row.claim_token = ""
                row.lease_until = None
                session.commit()
                results.append(DispatchResult(
                    row.id, OutboxStatus.CANCELLED, error="dryrun_intent_blocked"))
                continue
            payload = json.loads(row.payload_json)
            kind = row.kind
            target = row.target_id
            idem = row.idempotency_key
            # 分身发送需要 chat_id（用户身份不支持 message_id），
            # 必须在 session 关闭前取出，别在闭包外访问 ORM 对象。
            row_chat_id = row.chat_id or ""
            # R09 发送身份冻结：发送前一刻解析本人 token（resolver 异常按
            # token 不可用处理），再经统一授权与身份校验定身份——不匹配不发送。
            token_for_user = ""
            send_identity = "bot"
            if kind in {"reply", "correction"}:
                if user_token_resolver:
                    # Persist the claim and release SQLite's writer before a
                    # resolver rotates tokens in its own short transaction.
                    # Recheck the claim and all authority after that network IO.
                    claimed_id = row.id
                    trusted_tenant = row.tenant
                    session.commit()
                    try:
                        token_for_user = str(user_token_resolver(session_factory,
                            {**payload, "_delivery_scope": {"tenant": trusted_tenant}}) or "")
                    except Exception as exc:  # noqa: BLE001 — resolver 异常 = token 不可用
                        logger.warning("user token resolver failed: %s", exc)
                        token_for_user = ""
                    session.expire_all()
                    row = session.get(Outbox, claimed_id)
                    if (row is None or row.claim_token != token
                            or row.status != OutboxStatus.SENDING):
                        session.rollback()
                        results.append(DispatchResult(claimed_id, "claim_lost", error="claim_lost"))
                        continue
                    ok, reason = precheck(session, row)
                    if not ok:
                        row.status = OutboxStatus.CANCELLED
                        row.last_error = f"precheck:{reason}"
                        row.claim_token = ""
                        row.lease_until = None
                        session.commit()
                        results.append(DispatchResult(row.id, OutboxStatus.CANCELLED, error=reason))
                        continue
                disposition, identity, gate_reason = _governance.resolve_send_identity(
                    session, row, payload=payload, user_token_valid=bool(token_for_user))
                if disposition == _governance.SEND_IDENTITY_WAITING:
                    mark_waiting_authorization(session, row, token=token,
                                               reason=gate_reason, now=now)
                    session.commit()
                    results.append(DispatchResult(
                        row.id, OutboxStatus.WAITING_AUTHORIZATION,
                        error=f"waiting_authorization:{gate_reason}"))
                    continue
                if disposition == _governance.SEND_IDENTITY_CANCEL:
                    row.status = OutboxStatus.CANCELLED
                    row.last_error = f"identity_gate:{gate_reason}"
                    row.claim_token = ""
                    row.lease_until = None
                    session.commit()
                    results.append(DispatchResult(row.id, OutboxStatus.CANCELLED,
                                                  error=gate_reason))
                    continue
                send_identity = identity
            session.commit()
        finally:
            session.close()

        if kind in {"reply", "correction"}:
            if payload.get("card"):
                result = transport.reply_card(
                    target, payload["card"], idem, bool(payload.get("reply_in_thread"))
                )
            else:
                text = payload.get("text", "")
                # 群聊里回复要 @ 提问者：飞书的"引用回复"在群里不显眼，
                # 别人看不出这句话是回给谁的。私聊不需要 @（就两个人）。
                mention = (payload.get("mention_open_id") or "").strip()
                if mention and not payload.get("is_p2p"):
                    text = f'<at user_id="{mention}"></at> {text}'
                # 数字分身：send_identity 由发送前身份冻结决定（user=已批准本人
                # 身份且 token 可用；bot=明确机器人身份，含获批 fallback）。
                # 传输层对 user 意图缺 token 明确报错，不再静默降级（防御纵深）。
                result = transport.reply_text(
                    target, text, idem, bool(payload.get("reply_in_thread")),
                    user_access_token=token_for_user if send_identity == "user" else "",
                    user_chat_id=row_chat_id,
                    send_identity=send_identity,
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
                mark_sent(session, row, token=token, receipt_id=result.receipt_id, now=now,
                          identity=send_identity if kind in {"reply", "correction"} else "")
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
            [OutboxStatus.DELIVERY_UNKNOWN, OutboxStatus.FAILED, OutboxStatus.RETRY_WAIT,
             OutboxStatus.WAITING_AUTHORIZATION]
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
