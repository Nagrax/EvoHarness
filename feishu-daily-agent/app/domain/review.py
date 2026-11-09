"""Owner 批示：一次性动作令牌、版本绑定与同事务入队。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.ids import new_action_id, sha256_text
from app.domain import outbox as outbox_module
from app.memory import experience as experience_module
from app.models import (
    ActionToken,
    DraftStatus,
    Experience,
    ExperienceStatus,
    Message,
    Outbox,
    OutboxStatus,
    ReplyDecision,
    ReplyDraft,
    ReviewEvent,
    Task,
    TaskEvent,
    TaskStatus,
)


class ReviewError(ValueError):
    pass


def expire_stale_drafts(
    session: Session, *, now: datetime | None = None, ttl_hours: int | None = None
) -> int:
    """过期待批草稿按 DRAFT_TTL_HOURS 置为 superseded，避免待批无限堆积。"""
    settings = get_settings()
    now = now or utcnow()
    ttl = settings.draft_ttl_hours if ttl_hours is None else ttl_hours
    cutoff = now - timedelta(hours=max(1, int(ttl)))
    drafts = session.scalars(
        select(ReplyDraft).where(
            ReplyDraft.status == DraftStatus.PENDING,
            ReplyDraft.created_at < cutoff,
        )
    ).all()
    for draft in drafts:
        draft.status = DraftStatus.SUPERSEDED
        draft.reviewed_at = now
        note = f"ttl_expired:{ttl}h"
        draft.note = f"{draft.note}；{note}" if draft.note else note
    session.flush()
    return len(drafts)


@dataclass
class ReviewOutcome:
    action: str
    status: str
    target_id: int
    outbox_id: int | None = None
    detail: str = ""


def create_action_token(
    session: Session,
    *,
    owner: str,
    action: str,
    target_type: str,
    target_id: int | str,
    target_version: int,
    card_id: str = "",
    payload: dict | None = None,
    ttl_minutes: int = 72,
    tenant: str | None = None,
) -> ActionToken:
    settings = get_settings()
    token = ActionToken(
        action_id=new_action_id(),
        tenant=tenant or settings.tenant_id,
        owner=owner,
        card_id=card_id,
        target_type=target_type,
        target_id=str(target_id),
        target_version=int(target_version),
        action=action,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        expires_at=utcnow() + timedelta(minutes=ttl_minutes),
    )
    session.add(token)
    session.flush()
    return token


def _consume(session: Session, token: ActionToken, operator_id: str, now: datetime) -> None:
    if token.used_at is not None:
        raise ReviewError("action_already_used")
    if token.expires_at <= now:
        raise ReviewError("action_expired")
    if operator_id != token.owner:
        raise ReviewError("operator_mismatch")
    result = session.execute(
        update(ActionToken)
        .where(ActionToken.action_id == token.action_id, ActionToken.used_at.is_(None))
        .values(used_at=now)
    )
    if result.rowcount != 1:
        raise ReviewError("action_already_used")


def _log(
    session: Session,
    *,
    token: ActionToken,
    action: str,
    operator_id: str,
    detail: str,
    draft_id: int | None = None,
) -> None:
    session.add(
        ReviewEvent(
            tenant=token.tenant,
            draft_id=draft_id,
            action_id=token.action_id,
            target_type=token.target_type,
            target_id=token.target_id,
            target_version=token.target_version,
            action=action,
            operator_id=operator_id,
            detail=detail,
        )
    )


def _sediment_approved_answer(
    session: Session,
    *,
    draft: ReplyDraft,
    message: Message | None,
    content: str,
    operator_id: str,
    edited: bool,
) -> None:
    """一次回复审批与经验发布是两个动作：这里只沉淀候选，不自动发布。"""
    if message is None:
        return
    if draft.experience_id is None:
        experience_module.create_candidate(
            session,
            question=message.content_text,
            answer=content,
            source_message_id=message.message_id,
            audience={"chat_ids": [draft.chat_id]},
            visibility="private",
            category="faq",
            created_by=operator_id,
            reason="owner 批准回复后沉淀候选",
        )
        return
    previous = session.get(Experience, draft.experience_id)
    if previous is None:
        return
    experience_module.record_hit(session, previous)
    if edited:
        experience_module.create_candidate(
            session,
            question=message.content_text,
            answer=content,
            source_message_id=message.message_id,
            intent_id=previous.intent_id,
            previous_id=previous.id,
            patterns=previous.patterns(),
            negative_examples=previous.negative_examples(),
            audience=previous.audience(),
            preconditions=previous.preconditions,
            steps=previous.steps,
            allowed_links=previous.allowed_links,
            sensitive_level=previous.sensitive_level,
            visibility=previous.visibility,
            category=previous.category,
            created_by=operator_id,
            reason="人工修改已发回复生成候选版本",
        )


def _approve_draft(
    session: Session,
    *,
    draft: ReplyDraft,
    operator_id: str,
    edited_content: str | None,
    token: ActionToken | None,
    action: str,
    now: datetime,
) -> ReviewOutcome:
    settings = get_settings()
    if draft.status != DraftStatus.PENDING:
        raise ReviewError(f"draft_not_pending:{draft.status}")
    content = (edited_content or draft.draft_content or "").strip()
    if not content:
        raise ReviewError("empty_reply")
    if action == "edit" and not (edited_content or "").strip():
        raise ReviewError("empty_edit")
    content_changed = bool(edited_content and edited_content.strip() != draft.draft_content)
    if draft.shadow:
        # 影子草稿只记录决策，不生成发送意图。
        _log(
            session,
            token=token or ActionToken(
                action_id="shadow", tenant=draft.tenant, owner=operator_id,
                target_type="draft", target_id=str(draft.id), target_version=draft.version,
                action=action, payload_json="{}", expires_at=now + timedelta(minutes=1),
            ),
            action=action,
            operator_id=operator_id,
            detail="shadow_mode_no_send",
            draft_id=draft.id,
        )
        return ReviewOutcome(action, "shadow_recorded", draft.id, detail="shadow_mode")
    if content_changed:
        # 修改后发送：原子抢占 PENDING，再创建独立版本；并发/旧卡不得二次发信。
        claimed = session.execute(
            update(ReplyDraft)
            .where(
                ReplyDraft.id == draft.id,
                ReplyDraft.status == DraftStatus.PENDING,
                ReplyDraft.version == draft.version,
            )
            .values(status=DraftStatus.SUPERSEDED, note="修改后发送")
        )
        if claimed.rowcount != 1:
            raise ReviewError("draft_already_reviewed")
        session.refresh(draft)
        new_draft = ReplyDraft(
            tenant=draft.tenant,
            message_id=draft.message_id,
            chat_id=draft.chat_id,
            experience_id=draft.experience_id,
            draft_content=content,
            content_hash=sha256_text(content),
            version=int(draft.version or 1) + 1,
            score=draft.score,
            confidence=draft.confidence,
            mode=draft.mode,
            status=DraftStatus.APPROVED,
            shadow=draft.shadow,
            approved_by=operator_id,
            approved_at=now,
        )
        session.add(new_draft)
        session.flush()
        draft.superseded_by = new_draft.id
        draft = new_draft
    else:
        claimed = session.execute(
            update(ReplyDraft)
            .where(
                ReplyDraft.id == draft.id,
                ReplyDraft.status == DraftStatus.PENDING,
                ReplyDraft.version == draft.version,
            )
            .values(
                status=DraftStatus.APPROVED,
                approved_by=operator_id,
                approved_at=now,
            )
        )
        if claimed.rowcount != 1:
            raise ReviewError("draft_already_reviewed")
        session.refresh(draft)
    message = session.scalar(select(Message).where(Message.message_id == draft.message_id))
    source_id = message.source_id if message else None
    _sediment_approved_answer(
        session,
        draft=draft,
        message=message,
        content=content,
        operator_id=operator_id,
        edited=content_changed,
    )
    intent = outbox_module.intent_key(
        draft.tenant, draft.message_id, "reply", f"draft:{draft.id}", f"v{draft.version}"
    )
    row = outbox_module.enqueue(
        session,
        intent=intent,
        tenant=draft.tenant,
        kind="reply",
        chat_id=draft.chat_id,
        target_type="message",
        target_id=draft.message_id,
        payload={
            "text": content,
            "draft_id": draft.id,
            "reply_in_thread": bool(message.thread_id) if message else False,
        },
        policy_version=settings.policy_version,
        experience_id=draft.experience_id,
        source_id=source_id,
    )
    _log(
        session,
        token=token or ActionToken(
            action_id="direct", tenant=draft.tenant, owner=operator_id,
            target_type="draft", target_id=str(draft.id), target_version=draft.version,
            action=action, payload_json="{}", expires_at=now + timedelta(minutes=1),
        ),
        action=action,
        operator_id=operator_id,
        detail=f"outbox:{row.id}",
        draft_id=draft.id,
    )
    return ReviewOutcome(action, "queued", draft.id, row.id)


def approve_draft_direct(
    session: Session,
    draft_id: int,
    *,
    actor: str,
    edited_content: str | None = None,
    now: datetime | None = None,
) -> ReviewOutcome:
    """受信调用方（owner 指令 / 测试）直接批准；仍需在事务内入队。"""
    now = now or utcnow()
    draft = session.get(ReplyDraft, draft_id)
    if draft is None:
        raise ReviewError("draft_not_found")
    return _approve_draft(
        session,
        draft=draft,
        operator_id=actor,
        edited_content=edited_content,
        token=None,
        action="approve",
        now=now,
    )


def reject_draft_direct(
    session: Session, draft_id: int, *, actor: str, reason: str = "", now: datetime | None = None
) -> ReplyDraft:
    now = now or utcnow()
    draft = session.get(ReplyDraft, draft_id)
    if draft is None:
        raise ReviewError("draft_not_found")
    claimed = session.execute(
        update(ReplyDraft)
        .where(
            ReplyDraft.id == draft.id,
            ReplyDraft.status == DraftStatus.PENDING,
            ReplyDraft.version == draft.version,
        )
        .values(status=DraftStatus.REJECTED, reviewed_at=now, note=reason)
    )
    if claimed.rowcount != 1:
        raise ReviewError("draft_already_reviewed")
    session.refresh(draft)
    # 若已有排队/发送中的意图，取消未领取的；SENDING 由 worker 完成或进对账。
    for outbox_row in session.scalars(
        select(Outbox).where(
            Outbox.kind.in_(["reply", "correction"]),
            Outbox.status.in_([OutboxStatus.QUEUED, OutboxStatus.RETRY_WAIT, OutboxStatus.SENDING]),
        )
    ):
        try:
            payload = json.loads(outbox_row.payload_json or "{}")
        except ValueError:
            payload = {}
        if int(payload.get("draft_id") or 0) == draft.id and outbox_row.status != OutboxStatus.SENDING:
            outbox_row.status = OutboxStatus.CANCELLED
            outbox_row.last_error = "draft_rejected"
    experience = session.get(Experience, draft.experience_id) if draft.experience_id else None
    experience_module.record_reject(session, experience)
    session.add(
        ReviewEvent(
            tenant=draft.tenant,
            draft_id=draft.id,
            target_type="draft",
            target_id=str(draft.id),
            target_version=draft.version,
            action="reject",
            operator_id=actor,
            detail=reason,
        )
    )
    return draft


def handle_card_action(
    session: Session,
    *,
    action_id: str,
    operator_id: str,
    now: datetime | None = None,
    edited_content: str | None = None,
    reason: str = "",
) -> ReviewOutcome:
    now = now or utcnow()
    token = session.get(ActionToken, action_id)
    if token is None:
        raise ReviewError("action_not_found")
    _consume(session, token, operator_id, now)
    action = token.action
    if action in {"approve", "edit"}:
        draft = session.get(ReplyDraft, int(token.target_id))
        if draft is None:
            raise ReviewError("draft_not_found")
        if int(draft.version or 1) != int(token.target_version):
            raise ReviewError("stale_target_version")
        return _approve_draft(
            session,
            draft=draft,
            operator_id=operator_id,
            edited_content=edited_content,
            token=token,
            action=action,
            now=now,
        )
    if action == "reject":
        draft = session.get(ReplyDraft, int(token.target_id))
        if draft is None:
            raise ReviewError("draft_not_found")
        if int(draft.version or 1) != int(token.target_version):
            raise ReviewError("stale_target_version")
        reject_draft_direct(session, draft.id, actor=operator_id, reason=reason, now=now)
        _log(session, token=token, action="reject", operator_id=operator_id, detail=reason, draft_id=draft.id)
        return ReviewOutcome(action, "rejected", draft.id)
    if action == "verify_experience":
        experience = session.get(Experience, int(token.target_id))
        if experience is None:
            raise ReviewError("experience_not_found")
        if int(experience.version or 1) != int(token.target_version):
            raise ReviewError("stale_target_version")
        if experience.status not in {ExperienceStatus.CANDIDATE, ExperienceStatus.ASSIST}:
            raise ReviewError(f"experience_not_publishable:{experience.status}")
        experience = experience_module.publish(
            session, int(token.target_id), actor=operator_id, reason=reason or "owner 验证"
        )
        _log(session, token=token, action=action, operator_id=operator_id, detail=f"v{experience.version}")
        return ReviewOutcome(action, "published", experience.id)
    if action == "pause_experience":
        experience = experience_module.pause(
            session, int(token.target_id), actor=operator_id, reason=reason or "owner 暂停"
        )
        _log(session, token=token, action=action, operator_id=operator_id, detail=reason)
        return ReviewOutcome(action, "paused", experience.id)
    if action == "rollback_experience":
        payload = json.loads(token.payload_json or "{}")
        intent_id = payload.get("intent_id", "")
        current = session.get(Experience, int(token.target_id))
        if current is None or int(current.version or 1) != int(token.target_version):
            raise ReviewError("stale_target_version")
        head = session.get(experience_module.ExperienceHead, intent_id)
        if head is None or head.experience_id != current.id:
            raise ReviewError("rollback_not_current_head")
        target = experience_module.rollback(
            session, intent_id, actor=operator_id, reason=reason or "owner 回滚",
            expected_experience_id=current.id,
            expected_version=int(current.version or 1),
        )
        _log(session, token=token, action=action, operator_id=operator_id, detail=reason)
        return ReviewOutcome(
            action, "rolled_back" if target else "no_previous_version",
            target.id if target else current.id,
        )
    if action in {"complete_task", "reopen_task", "cancel_task"}:
        task = session.get(Task, int(token.target_id))
        if task is None:
            raise ReviewError("task_not_found")
        if action == "complete_task":
            task.status = TaskStatus.DONE
        elif action == "cancel_task":
            task.status = TaskStatus.CANCELLED
        else:
            task.status = TaskStatus.OPEN
        task.needs_confirmation = False
        session.add(
            TaskEvent(
                task_id=task.id,
                event_type=action,
                actor=operator_id,
                detail=reason or "owner 操作",
                evidence_message_ids="[]",
            )
        )
        _log(session, token=token, action=action, operator_id=operator_id, detail=reason)
        return ReviewOutcome(action, task.status, task.id)
    if action == "correct_sent_reply":
        payload = json.loads(token.payload_json or "{}")
        corrected = (edited_content or payload.get("corrected_answer", "") or "").strip()
        if not corrected:
            raise ReviewError("empty_correction")
        draft = session.get(ReplyDraft, int(token.target_id))
        if draft is None or draft.status != DraftStatus.SENT:
            raise ReviewError("correction_requires_sent_reply")
        candidate = experience_module.correct_sent_reply(
            session,
            draft_id=int(token.target_id),
            corrected_answer=corrected,
            actor=operator_id,
            negative_examples=payload.get("negative_examples"),
        )
        _log(session, token=token, action=action, operator_id=operator_id, detail=reason)
        return ReviewOutcome(action, "candidate_created", candidate.id if candidate else 0)
    raise ReviewError(f"unknown_action:{action}")


def decide_draft_for_message(
    session: Session, message_id: str
) -> ReplyDraft:
    draft = session.scalar(
        select(ReplyDraft)
        .where(ReplyDraft.message_id == message_id)
        .order_by(ReplyDraft.id.desc())
    )
    if draft is None:
        raise ReviewError("draft_not_found")
    return draft
