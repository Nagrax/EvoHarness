"""回复决策引擎：实时事件与每日补采共用的统一入口。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.ids import sha256_json, sha256_text
from app.domain import admission, intake, policy, review, tasks
from app.domain import outbox as outbox_module
from app.feishu.messages import IncomingMessage
from app.llm.service import (
    classify_message,
    compose_reply,
    generate_fallback_reply,
    prompt_versions,
)
from app.memory import experience as experience_module
from app.models import (
    Category,
    Chat,
    DraftMode,
    DraftStatus,
    Experience,
    Message,
    ReplyDecision,
    ReplyDraft,
    Source,
)


def classify_message(text: str) -> dict:  # re-export for monkeypatching in callers/tests
    from app.llm.service import classify_message as _classify

    return _classify(text)


def compose_reply(question: str, experience) -> str:
    from app.llm.service import compose_reply as _compose

    return _compose(question, experience)


def generate_fallback_reply(question: str) -> str:
    from app.llm.service import generate_fallback_reply as _fallback

    return _fallback(question)


def upsert_chat(session: Session, chat_id: str, chat_type: str, name: str) -> Chat:
    settings = get_settings()
    chat = session.get(Chat, chat_id)
    if chat is None:
        chat = Chat(chat_id=chat_id, tenant=settings.tenant_id, chat_type=chat_type, name=name)
        session.add(chat)
    elif name:
        chat.name = name
    session.flush()
    return chat


def ingest_message(
    session: Session,
    incoming: IncomingMessage,
    *,
    source: Source | None = None,
    allow_unregistered: bool = True,
) -> Message:
    """幂等入库并分类；不触发回复。"""
    settings = get_settings()
    if source is None:
        result = admission.admit(session, incoming, allow_unregistered=allow_unregistered)
        if not result.accepted:
            raise PermissionError(result.reason)
        source = result.source
    intake_result = intake.ingest_message(session, incoming, source=source)
    message = intake_result.message
    # 历史补采只入库不分类；处理阶段必须补上分类，否则补采问题会永久停在 unknown。
    needs_classify = (
        intake_result.created
        or intake_result.edited
        or (not message.handled and message.category == Category.UNKNOWN)
    )
    if needs_classify:
        classification = classify_message(message.content_text)
        message.category = classification["category"]
        message.summary = str(classification.get("summary") or "")[:255]
        message.due = str(classification.get("due") or "")[:64]
        session.flush()
    return message


def _latest_draft(session: Session, message_id: str) -> ReplyDraft | None:
    return session.scalar(
        select(ReplyDraft)
        .where(ReplyDraft.message_id == message_id)
        .order_by(ReplyDraft.id.desc())
    )


def _snapshot_id(session: Session, tenant: str) -> str:
    rows = []
    for head in session.scalars(
        select(experience_module.ExperienceHead).where(
            experience_module.ExperienceHead.tenant == tenant
        )
    ):
        rows.append((head.intent_id, head.experience_id))
    return sha256_json(sorted(rows))[:32]


def process_incoming(
    session: Session,
    incoming: IncomingMessage,
    *,
    source: Source | None = None,
    allow_unregistered: bool = True,
    now: datetime | None = None,
    shadow_override: bool | None = None,
) -> Optional[ReplyDraft]:
    """处理一条消息：分类、任务抽取、经验匹配、硬门、草稿/发送意图。"""
    settings = get_settings()
    now = now or utcnow()
    if source is None:
        admission_result = admission.admit(
            session, incoming, allow_unregistered=allow_unregistered
        )
        if not admission_result.accepted:
            return None
        source = admission_result.source
    message = ingest_message(session, incoming, source=source)
    if message.recalled:
        return None

    existing_decision = session.scalar(
        select(ReplyDecision).where(
            ReplyDecision.tenant == message.tenant,
            ReplyDecision.message_id == message.message_id,
        )
    )
    if existing_decision is not None or message.handled:
        message.handled = True
        return _latest_draft(session, message.message_id)

    tasks.extract_and_upsert(session, message, source=source)
    if message.category != Category.QUESTION:
        tasks.apply_completion_signal(session, message)
        message.handled = True
        session.flush()
        return None

    match = experience_module.find_best_match(
        session, message.content_text, tenant=message.tenant, source=source
    )
    decision = policy.evaluate(
        session,
        message=message,
        source=source,
        experience=match.experience,
        similarity=match.score,
        match_reasons=match.reasons,
        now=now,
        shadow_override=shadow_override,
    )
    versions = prompt_versions()
    session.add(
        ReplyDecision(
            tenant=message.tenant,
            message_id=message.message_id,
            decision=decision.decision,
            reason_codes=json.dumps(decision.reasons, ensure_ascii=False),
            experience_id=decision.experience.id if decision.experience else None,
            similarity=decision.similarity,
            rule_pass=decision.decision == "auto_eligible",
            policy_version=settings.policy_version,
            model_version=settings.llm_model,
            prompt_version=versions["classify"],
            data_snapshot_id=_snapshot_id(session, message.tenant),
            shadow=decision.shadow,
        )
    )
    message.handled = True

    if decision.decision == "skip":
        session.flush()
        return None

    experience = decision.experience
    if experience is not None:
        content = compose_reply(message.content_text, experience)
        experience_id = experience.id
    else:
        content = generate_fallback_reply(message.content_text)
        experience_id = None

    if decision.decision == "auto_eligible":
        draft = ReplyDraft(
            tenant=message.tenant,
            message_id=message.message_id,
            chat_id=message.chat_id,
            experience_id=experience_id,
            draft_content=content,
            content_hash=sha256_text(content),
            version=1,
            score=decision.similarity,
            confidence=decision.similarity,
            mode=DraftMode.AUTO,
            status=DraftStatus.APPROVED,
            approved_by="policy",
            approved_at=now,
        )
        session.add(draft)
        session.flush()
        intent = outbox_module.intent_key(
            message.tenant, message.message_id, "reply", f"draft:{draft.id}", "v1"
        )
        outbox_module.enqueue(
            session,
            intent=intent,
            tenant=message.tenant,
            kind="reply",
            chat_id=message.chat_id,
            target_type="message",
            target_id=message.message_id,
            payload={
                "text": content,
                "draft_id": draft.id,
                "reply_in_thread": bool(message.thread_id),
                "auto": True,
            },
            policy_version=settings.policy_version,
            experience_id=experience_id,
            source_id=source.id if source else None,
        )
        experience_module.record_hit(session, experience, auto=True)
        session.flush()
        return draft

    draft = ReplyDraft(
        tenant=message.tenant,
        message_id=message.message_id,
        chat_id=message.chat_id,
        experience_id=experience_id,
        draft_content=content,
        content_hash=sha256_text(content),
        version=1,
        score=decision.similarity,
        confidence=decision.similarity,
        mode=DraftMode.SHADOW if decision.shadow else DraftMode.DRAFT,
        status=DraftStatus.PENDING,
        shadow=decision.shadow,
        note=",".join(decision.reasons),
    )
    session.add(draft)
    if experience is not None:
        experience_module.record_hit(session, experience)
    session.flush()
    return draft


def approve_draft(
    session: Session, draft_id: int, edited_content: Optional[str] = None
) -> Optional[ReplyDraft]:
    settings = get_settings()
    draft = session.get(ReplyDraft, draft_id)
    if draft is None:
        return None
    try:
        outcome = review.approve_draft_direct(
            session, draft_id, actor=settings.feishu_owner_open_id,
            edited_content=edited_content,
        )
    except review.ReviewError:
        return draft
    if outcome.outbox_id:
        draft.status = DraftStatus.APPROVED
    return draft


def reject_draft(
    session: Session, draft_id: int, reason: str = ""
) -> Optional[ReplyDraft]:
    settings = get_settings()
    draft = session.get(ReplyDraft, draft_id)
    if draft is None:
        return None
    try:
        return review.reject_draft_direct(
            session, draft_id, actor=settings.feishu_owner_open_id, reason=reason
        )
    except review.ReviewError:
        return draft


def dispatch_pending(*, limit: int = 20, transport=None):
    """提交后发送的便捷入口；测试可注入假传输。"""
    from app.core.database import SessionLocal

    return outbox_module.dispatch_outbox(SessionLocal, transport=transport, limit=limit)
