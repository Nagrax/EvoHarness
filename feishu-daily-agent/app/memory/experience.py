"""版本化经验：问题簇、受众、负例、证据与发布/回滚。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.ids import question_hash
from app.memory import retrieval
from app.models import (
    DraftStatus,
    Experience,
    ExperienceEvent,
    ExperienceHead,
    ExperienceStatus,
    Message,
    ReplyDraft,
    Source,
)


@dataclass
class MatchResult:
    experience: Experience | None
    score: float
    engine: str
    reasons: list[str]


def _head(session: Session, tenant: str, intent_id: str) -> Experience | None:
    head = session.get(ExperienceHead, intent_id)
    if head is None or head.tenant != tenant:
        return None
    return session.get(Experience, head.experience_id)


def _next_version(session: Session, tenant: str, intent_id: str) -> int:
    values = [
        int(item.version or 0)
        for item in session.scalars(
            select(Experience).where(
                Experience.tenant == tenant, Experience.intent_id == intent_id
            )
        )
    ]
    return (max(values) + 1) if values else 1


def create_candidate(
    session: Session,
    *,
    question: str,
    answer: str,
    tenant: str | None = None,
    owner: str | None = None,
    source_message_id: str | None = None,
    patterns: list[str] | None = None,
    negative_examples: list[str] | None = None,
    audience: dict | None = None,
    preconditions: str = "",
    steps: str = "",
    allowed_links: str = "",
    sensitive_level: str = "private",
    visibility: str = "private",
    created_by: str = "system",
    reason: str = "",
    category: str = "faq",
    intent_id: str | None = None,
    previous_id: int | None = None,
    valid_until: datetime | None = None,
) -> Experience:
    settings = get_settings()
    tenant = tenant or settings.tenant_id
    owner = owner or settings.feishu_owner_open_id
    intent = intent_id or question_hash(question)
    experience = Experience(
        tenant=tenant,
        owner=owner,
        intent_id=intent,
        version=_next_version(session, tenant, intent),
        question_pattern=question,
        canonical_answer=answer,
        patterns_json=json.dumps(patterns or [question], ensure_ascii=False),
        negative_examples_json=json.dumps(negative_examples or [], ensure_ascii=False),
        audience_json=json.dumps(audience or {}, ensure_ascii=False),
        preconditions=preconditions,
        steps=steps,
        allowed_links=allowed_links,
        source_ids=json.dumps(
            [source_message_id] if source_message_id else [], ensure_ascii=False
        ),
        sensitive_level=sensitive_level,
        visibility=visibility,
        valid_until=valid_until,
        status=ExperienceStatus.CANDIDATE,
        category=category,
        previous_id=previous_id,
        reason=reason,
        created_by=created_by,
        source_message_id=source_message_id,
    )
    session.add(experience)
    session.flush()
    head = session.get(ExperienceHead, intent)
    if head is None:
        session.add(ExperienceHead(intent_id=intent, tenant=tenant, experience_id=experience.id))
    else:
        current = session.get(Experience, head.experience_id)
        # 候选不能把已发布版本从 head 上挤掉；发布/回滚才切换 head。
        if current is None or current.status != ExperienceStatus.APPROVED_AUTO:
            head.experience_id = experience.id
            head.updated_at = utcnow()
    session.add(
        ExperienceEvent(
            experience_id=experience.id,
            action="candidate",
            actor=created_by,
            detail=reason or "从可信问答/更正生成候选",
        )
    )
    session.flush()
    return experience


def create_experience(
    session: Session,
    question_pattern: str,
    canonical_answer: str,
    source_message_id: str | None = None,
    **kwargs,
) -> Experience:
    """兼容原型的便捷入口。"""
    return create_candidate(
        session,
        question=question_pattern,
        answer=canonical_answer,
        source_message_id=source_message_id,
        **kwargs,
    )


def _audience_allows(experience: Experience, source: Source | None) -> tuple[bool, str]:
    if source is None:
        return False, "audience_source_missing"
    audience = experience.audience()
    visibility = experience.visibility or "private"
    if visibility == "private":
        chat_ids = audience.get("chat_ids") or []
        if chat_ids:
            return (source.chat_id in chat_ids, "audience_chat_mismatch")
        if source.source_type != "p2p" or source.owner != experience.owner:
            return False, "audience_private_scope"
        return True, "audience_private_ok"
    if visibility == "public":
        chat_types = audience.get("chat_types") or []
        if chat_types and source.source_type not in chat_types:
            return False, "audience_type_mismatch"
        if experience.sensitive_level not in {"public", "internal"}:
            return False, "audience_sensitive_level"
        return True, "audience_public_ok"
    return False, "audience_visibility_unknown"


def _active_query(session: Session, tenant: str):
    # 候选版本可辅助拟稿，已发布版本可自动回复；两者都参与检索，
    # 但 head 只由 publish/rollback 切换。
    for experience in session.scalars(
        select(Experience).where(
            Experience.tenant == tenant,
            Experience.status.in_(
                [
                    ExperienceStatus.CANDIDATE,
                    ExperienceStatus.ASSIST,
                    ExperienceStatus.APPROVED_AUTO,
                ]
            ),
        )
    ):
        if experience.valid_until is not None and experience.valid_until <= utcnow():
            continue
        yield experience


def find_best_match(
    session: Session,
    query: str,
    *,
    tenant: str | None = None,
    source: Source | None = None,
) -> MatchResult:
    settings = get_settings()
    tenant = tenant or settings.tenant_id
    experiences = [
        exp for exp in _active_query(session, tenant) if _audience_allows(exp, source)[0]
    ]
    if not experiences:
        return MatchResult(None, 0.0, retrieval.LAST_ENGINE, ["no_active_experience"])
    corpus: dict[str, list[str]] = {}
    for exp in experiences:
        text = "\n".join(
            [
                exp.question_pattern,
                *exp.patterns(),
                exp.preconditions,
                exp.steps,
                exp.canonical_answer[:500],
            ]
        )
        corpus[str(exp.id)] = retrieval.tokenize(text)
    ranked, engine = retrieval.rank(query, corpus)
    if not ranked:
        return MatchResult(None, 0.0, engine, ["no_retrieval_hit"])
    # 评分归一化标定（F09 硬前置，自迭代实测发现）：BM25 原始分量纲随语料规模漂移
    # （同窗口 exact=1.574 / 近义=0.978，0.85 固定阈值无法区分近义误触发）。
    # 将分数压到 (0,1)：s/(1+s) 单调保序，阈值语义变为有界分。
    # 精确匹配仍钳制到 exact_match_score（0.95），因此只有精确重复能过 0.85 自动阈值；
    # 近义要过 0.85 需原始分 ≥5.67（当前语料不可能），误发通道被标定关闭。
    ranked = [(key, float(raw) / (1.0 + float(raw))) for key, raw in ranked]
    # 同分确定性（自迭代实测发现）：同 question_pattern 的候选与已发布 head 并列时，
    # BM25 平分的排序取决于语料迭代顺序，候选可能压过 head，导致自动窗口取错版本。
    # 规则：分数优先；同分（含浮点近邻）时已发布优先、高版本优先、id 大者优先。
    def _tie_rank(item: tuple[str, float]) -> tuple[float, int, int, int]:
        exp = session.get(Experience, int(item[0]))
        is_head = 1 if exp is not None and exp.status == ExperienceStatus.APPROVED_AUTO else 0
        version = exp.version if exp is not None else 0
        return (float(item[1]), is_head, version, int(item[0]))

    ranked = sorted(ranked, key=_tie_rank, reverse=True)
    best_id, best_score = ranked[0]
    best = session.get(Experience, int(best_id))
    reasons = [f"retrieval:{engine}"]
    # 同意图已发布 head 优先（自迭代实测发现）：候选的变体文本可能原始分更高，
    # 但对外应答与自动回复必须使用发布版本；候选仅用于辅助拟稿。
    if best is not None and best.status != ExperienceStatus.APPROVED_AUTO:
        head = session.get(ExperienceHead, best.intent_id)
        if head is not None and head.experience_id != best.id:
            head_exp = session.get(Experience, head.experience_id)
            if (
                head_exp is not None
                and head_exp.status == ExperienceStatus.APPROVED_AUTO
            ):
                best = head_exp
                best_id = str(head_exp.id)
                reasons.append("intent_head_preferred")
    positives = retrieval.tokenize(
        "\n".join([best.question_pattern, *best.patterns(), best.preconditions, best.steps])
    )
    for negative in best.negative_examples():
        neg_rank, _ = retrieval.rank(
            query, {"positive": positives, "negative": retrieval.tokenize(negative)}
        )
        scores = dict(neg_rank)
        neg_score = scores.get("negative", 0.0)
        pos_score = scores.get("positive", 0.0)
        if (
            neg_score >= settings.negative_example_threshold
            and neg_score > pos_score
        ):
            return MatchResult(best, best_score, engine, ["negative_example_conflict"])
    normalized_query = retrieval.normalize_question(query)
    if normalized_query and any(
        normalized_query == retrieval.normalize_question(pattern)
        for pattern in [best.question_pattern, *best.patterns()]
    ):
        best_score = max(float(best_score), settings.exact_match_score)
        reasons.append("exact_pattern_match")
    if best_score < settings.draft_min_threshold:
        reasons.append("below_draft_threshold")
    return MatchResult(best, float(best_score), engine, reasons)


def publish(
    session: Session, experience_id: int, *, actor: str, reason: str = ""
) -> Experience:
    experience = session.get(Experience, experience_id)
    if experience is None:
        raise ValueError("experience_not_found")
    if experience.valid_until is not None and experience.valid_until <= utcnow():
        raise ValueError("experience_expired")
    # 同一 intent 只保留一个 approved_auto 头。
    for other in session.scalars(
        select(Experience).where(
            Experience.tenant == experience.tenant,
            Experience.intent_id == experience.intent_id,
            Experience.id != experience.id,
            Experience.status == ExperienceStatus.APPROVED_AUTO,
        )
    ):
        other.status = ExperienceStatus.SUPERSEDED
        session.add(
            ExperienceEvent(
                experience_id=other.id,
                action="superseded",
                actor=actor,
                detail=f"由版本 v{experience.version} 取代",
            )
        )
    experience.status = ExperienceStatus.APPROVED_AUTO
    head = session.get(ExperienceHead, experience.intent_id)
    if head is None:
        session.add(
            ExperienceHead(
                intent_id=experience.intent_id,
                tenant=experience.tenant,
                experience_id=experience.id,
            )
        )
    else:
        head.experience_id = experience.id
        head.updated_at = utcnow()
    session.add(
        ExperienceEvent(
            experience_id=experience.id,
            action="publish",
            actor=actor,
            detail=reason or f"发布 v{experience.version}",
        )
    )
    session.flush()
    return experience


def verify_experience(session: Session, experience_id: int, actor: str = "owner") -> Experience:
    return publish(session, experience_id, actor=actor)


def pause(session: Session, experience_id: int, *, actor: str, reason: str = "") -> Experience:
    experience = session.get(Experience, experience_id)
    if experience is None:
        raise ValueError("experience_not_found")
    experience.status = ExperienceStatus.PAUSED
    session.add(
        ExperienceEvent(
            experience_id=experience.id,
            action="pause",
            actor=actor,
            detail=reason or "手动暂停",
        )
    )
    session.flush()
    return experience


def rollback(
    session: Session,
    intent_id: str,
    *,
    actor: str,
    reason: str = "",
    expected_experience_id: int | None = None,
    expected_version: int | None = None,
) -> Experience | None:
    head = session.get(ExperienceHead, intent_id)
    if head is None:
        return None
    current = session.get(Experience, head.experience_id)
    if expected_experience_id is not None and (
        current is None or current.id != expected_experience_id
    ):
        return None
    if expected_version is not None and (
        current is None or int(current.version or 1) != expected_version
    ):
        return None
    target = session.get(Experience, current.previous_id) if current and current.previous_id else None
    if target is None:
        # 没有上一版时幂等 no-op；不能把当前已发布版本悄悄暂停。
        return None
    if current is not None and current.status == ExperienceStatus.APPROVED_AUTO:
        current.status = ExperienceStatus.PAUSED
    target.status = ExperienceStatus.APPROVED_AUTO
    head.experience_id = target.id
    head.updated_at = utcnow()
    session.add(
        ExperienceEvent(
            experience_id=target.id,
            action="rollback",
            actor=actor,
            detail=reason or f"回滚到 v{target.version}",
        )
    )
    session.flush()
    return target


def record_hit(
    session: Session, experience: Experience | None, *, auto: bool = False
) -> None:
    if experience is None:
        return
    experience.hit_count = int(experience.hit_count or 0) + 1
    if auto:
        experience.auto_reply_count = int(experience.auto_reply_count or 0) + 1
    session.flush()


def record_reject(session: Session, experience: Experience | None) -> None:
    if experience is None:
        return
    experience.reject_count = int(experience.reject_count or 0) + 1
    settings = get_settings()
    if int(experience.reject_count or 0) >= settings.experience_dismiss_limit:
        experience.status = ExperienceStatus.PAUSED
        session.add(
            ExperienceEvent(
                experience_id=experience.id,
                action="auto_pause",
                actor="system",
                detail=f"连续驳回达到 {settings.experience_dismiss_limit} 次",
            )
        )
    session.flush()


def invalidate_experience_for_message(
    session: Session, message: Message, *, reason: str
) -> list[Experience]:
    affected: list[Experience] = []
    for experience in session.scalars(
        select(Experience).where(Experience.tenant == message.tenant)
    ):
        if message.message_id not in experience.source_message_ids():
            continue
        if experience.status in {
            ExperienceStatus.CANDIDATE,
            ExperienceStatus.ASSIST,
        }:
            experience.status = ExperienceStatus.SUPERSEDED
        elif experience.status == ExperienceStatus.APPROVED_AUTO:
            experience.status = ExperienceStatus.PAUSED
        else:
            continue
        affected.append(experience)
        session.add(
            ExperienceEvent(
                experience_id=experience.id,
                action="invalidate",
                actor="system",
                detail=f"{reason}: {message.message_id}",
            )
        )
        for draft in session.scalars(
            select(ReplyDraft).where(
                ReplyDraft.experience_id == experience.id,
                ReplyDraft.status.in_([DraftStatus.PENDING, DraftStatus.APPROVED]),
            )
        ):
            draft.status = DraftStatus.SUPERSEDED
            draft.note = reason
    if affected:
        session.flush()
    return affected


def correct_sent_reply(
    session: Session,
    *,
    draft_id: int,
    corrected_answer: str,
    actor: str,
    reason: str = "",
    negative_examples: Iterable[str] | None = None,
) -> Experience | None:
    """对已发回复做人工纠正：旧经验暂停，生成新候选，不覆盖历史回执。"""
    draft = session.get(ReplyDraft, draft_id)
    if draft is None:
        return None
    message = session.scalar(
        select(Message).where(Message.message_id == draft.message_id)
    ) if draft.message_id else None
    previous = session.get(Experience, draft.experience_id) if draft.experience_id else None
    if previous is not None:
        pause(session, previous.id, actor=actor, reason=reason or "已发回复被纠正")
    candidate = create_candidate(
        session,
        question=message.content_text if message else (previous.question_pattern if previous else ""),
        answer=corrected_answer,
        source_message_id=message.message_id if message else None,
        intent_id=previous.intent_id if previous else None,
        previous_id=previous.id if previous else None,
        patterns=previous.patterns() if previous else None,
        negative_examples=list(negative_examples or (previous.negative_examples() if previous else [])),
        audience=previous.audience() if previous else None,
        preconditions=previous.preconditions if previous else "",
        steps=previous.steps if previous else "",
        allowed_links=previous.allowed_links if previous else "",
        sensitive_level=previous.sensitive_level if previous else "private",
        visibility=previous.visibility if previous else "private",
        category=previous.category if previous else "faq",
        created_by=actor,
        reason=reason or "已发回复纠正生成候选",
    )
    return candidate
