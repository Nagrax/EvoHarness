"""自动回复硬门与纯策略判定。

模型只提供结构化证据与草稿；是否发送由这里的确定性规则决定。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Integer, cast, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import UTC, in_time_window, logical_date_for, utcnow
from app.core.config import get_settings
from app.models import (
    Category,
    Experience,
    ExperienceStatus,
    Message,
    Outbox,
    OutboxStatus,
    ReplyDraft,
    DraftStatus,
    Source,
    SystemState,
)

_SECRET_RE = re.compile(
    r"(密码|口令|token|secret|api[_ -]?key|身份证|银行卡|信用卡|验证码|手机号|private key)",
    re.I,
)
_PROMISE_RE = re.compile(r"(保证|承诺|一定会|肯定能|绝对|包过|100%|百分百)")
_INJECTION_RE = re.compile(
    r"(忽略(之前|以上|前面)|ignore\s+(all\s+)?(previous|prior)|system\s+prompt|"
    r"开发者模式|developer\s+mode|你现在是|jailbreak|越狱)",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s]+")


@dataclass
class PolicyDecision:
    decision: str  # skip/draft_only/needs_review/auto_eligible
    reasons: list[str] = field(default_factory=list)
    shadow: bool = False
    similarity: float = 0.0
    experience: Experience | None = None


def _daily_auto_count(session: Session, *, tenant: str, chat_id: str, now: datetime) -> int:
    settings = get_settings()
    day = logical_date_for(now, settings.daily_report_timezone)
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(settings.daily_report_timezone)
    local_start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    start_utc = local_start.astimezone(UTC).replace(tzinfo=None)
    return int(
        session.scalar(
            select(func.count(Outbox.id)).where(
                Outbox.tenant == tenant,
                Outbox.chat_id == chat_id,
                Outbox.kind.in_(["reply", "correction"]),
                Outbox.auto.is_(True),
                Outbox.status == OutboxStatus.SENT,
                Outbox.created_at >= start_utc,
            )
        )
        or 0
    )


def _reserve_auto_slot(
    session: Session, *, tenant: str, chat_id: str, now: datetime
) -> bool:
    """原子预留每日自动回复名额；并发进程不能都读到 0 后各发一条。"""
    settings = get_settings()
    day = logical_date_for(now, settings.daily_report_timezone)
    key = f"auto_quota:{tenant}:{chat_id}:{day.isoformat()}"
    if session.get(SystemState, key) is None:
        try:
            with session.begin_nested():
                session.add(SystemState(key=key, value="0"))
                session.flush()
        except IntegrityError:
            pass
    result = session.execute(
        update(SystemState)
        .where(
            SystemState.key == key,
            cast(SystemState.value, Integer) < settings.auto_reply_max_per_chat_per_day,
        )
        .values(value=cast(SystemState.value, Integer) + 1)
    )
    return result.rowcount == 1


def answer_safety(answer: str, *, allowed_links: str = "") -> list[str]:
    reasons: list[str] = []
    if _SECRET_RE.search(answer or ""):
        reasons.append("sensitive_content")
    if _PROMISE_RE.search(answer or ""):
        reasons.append("promise_or_commitment")
    if _INJECTION_RE.search(answer or ""):
        reasons.append("prompt_injection")
    allowed = {link.strip() for link in (allowed_links or "").split(",") if link.strip()}
    for link in _URL_RE.findall(answer or ""):
        if allowed and link not in allowed:
            reasons.append("link_not_allowed")
            break
    return reasons


def evaluate(
    session: Session,
    *,
    message: Message,
    source: Source | None,
    experience: Experience | None,
    similarity: float,
    match_reasons: list[str],
    now: datetime | None = None,
    shadow_override: bool | None = None,
) -> PolicyDecision:
    settings = get_settings()
    now = now or utcnow()
    reasons: list[str] = []
    decision = "needs_review"
    shadow = settings.shadow_mode if shadow_override is None else shadow_override

    if message.is_from_bot:
        return PolicyDecision("skip", ["bot_echo"], shadow, similarity, experience)
    if message.recalled:
        return PolicyDecision("skip", ["message_recalled"], shadow, similarity, experience)
    if source is None or source.revoked_at is not None or not source.enabled:
        return PolicyDecision("skip", ["source_unavailable"], shadow, similarity, experience)
    if not source.reply_allowed:
        return PolicyDecision("draft_only", ["reply_permission_missing"], shadow, similarity, experience)
    if message.category != Category.QUESTION:
        return PolicyDecision("skip", ["not_question"], shadow, similarity, experience)
    if _INJECTION_RE.search(message.content_text or ""):
        return PolicyDecision("needs_review", ["prompt_injection"], shadow, similarity, experience)
    if experience is None:
        return PolicyDecision("draft_only", match_reasons + ["no_experience"], shadow, similarity, None)
    if "negative_example_conflict" in match_reasons:
        return PolicyDecision("needs_review", match_reasons, shadow, similarity, experience)
    if experience.status != ExperienceStatus.APPROVED_AUTO:
        return PolicyDecision("draft_only", match_reasons + ["experience_not_approved"], shadow, similarity, experience)
    if experience.visibility != "public":
        reasons.append("private_experience_manual")
    if similarity < settings.auto_reply_threshold:
        reasons.append("below_auto_threshold")
    if (experience.category or "faq") not in settings.auto_reply_categories:
        reasons.append("category_not_allowed")
    safety = answer_safety(experience.canonical_answer, allowed_links=experience.allowed_links)
    reasons.extend(safety)
    message_time = datetime.fromtimestamp(message.create_time / 1000, tz=UTC).replace(tzinfo=None)
    age_seconds = max(0.0, (now - message_time).total_seconds())
    if age_seconds > settings.source_freshness_seconds:
        reasons.append("stale_trigger")
    if not in_time_window(now, settings.auto_reply_start_hour, settings.auto_reply_end_hour, settings.daily_report_timezone):
        reasons.append("outside_reply_window")
    if experience.valid_until is not None and experience.valid_until <= now:
        reasons.append("experience_expired")
    if not settings.auto_reply_enabled:
        reasons.append("auto_reply_disabled")
    if settings.deployment_mode != "whitelist_auto":
        reasons.append("mode_not_whitelist_auto")
    if not settings.is_whitelisted(message.chat_id):
        reasons.append("chat_not_whitelisted")

    if reasons:
        decision = "draft_only" if shadow else "needs_review"
        return PolicyDecision(decision, match_reasons + reasons, True, similarity, experience)
    if shadow:
        return PolicyDecision("draft_only", match_reasons + ["shadow_mode"], True, similarity, experience)
    # 同主题存在多个高分版本时转人工，避免自动挑选冲突答案。
    close = [
        item
        for item in session.scalars(
            select(Experience).where(
                Experience.tenant == message.tenant,
                Experience.intent_id == experience.intent_id,
                Experience.status == ExperienceStatus.APPROVED_AUTO,
                Experience.id != experience.id,
            )
        )
    ]
    if close:
        return PolicyDecision("needs_review", match_reasons + ["experience_conflict"], shadow, similarity, experience)
    if not _reserve_auto_slot(
        session, tenant=message.tenant, chat_id=message.chat_id, now=now
    ):
        return PolicyDecision(
            "draft_only", match_reasons + ["daily_chat_limit"], shadow, similarity, experience
        )
    return PolicyDecision("auto_eligible", match_reasons + ["all_hard_gates_passed"], False, similarity, experience)
