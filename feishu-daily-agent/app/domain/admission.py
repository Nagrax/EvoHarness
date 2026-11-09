"""确定性准入：实时事件与历史补采共用同一套规则。

硬约束：
- 来源必须显式登记且启用；空白名单/未知身份/撤权按关闭处理。
- 机器人身份未知时，群聊 @ 不能被当作“@机器人”放行。
- 读许可不能推导写许可；回复许可单独存在 Source.reply_allowed。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.feishu.messages import IncomingMessage
from app.models import CoverageStatus, Source


@dataclass
class Admission:
    accepted: bool
    reason: str
    source: Source | None = None
    should_process: bool = False


def ensure_source(
    session: Session,
    *,
    tenant: str,
    owner: str,
    chat_id: str,
    thread_id: str = "",
    source_type: str = "p2p",
    discovery: str = "bot_list",
    read_identity: str = "bot",
    reply_identity: str = "bot",
    reply_allowed: bool = False,
    allowed_content_types: str = "text,post",
) -> Source:
    source = session.scalar(
        select(Source).where(
            Source.tenant == tenant,
            Source.chat_id == chat_id,
            Source.thread_id == thread_id,
        )
    )
    if source is None:
        source = Source(
            tenant=tenant,
            owner=owner,
            chat_id=chat_id,
            thread_id=thread_id,
            source_type=source_type,
            discovery=discovery,
            read_identity=read_identity,
            reply_identity=reply_identity,
            reply_allowed=reply_allowed,
            allowed_content_types=allowed_content_types,
        )
        session.add(source)
        session.flush()
    return source


def _find_source(session: Session, tenant: str, chat_id: str, thread_id: str) -> Source | None:
    if thread_id:
        exact = session.scalar(
            select(Source).where(
                Source.tenant == tenant,
                Source.chat_id == chat_id,
                Source.thread_id == thread_id,
            )
        )
        if exact is not None:
            return exact
    return session.scalar(
        select(Source).where(
            Source.tenant == tenant,
            Source.chat_id == chat_id,
            Source.thread_id == "",
        )
    )


def admit(
    session: Session,
    incoming: IncomingMessage,
    *,
    tenant: str | None = None,
    allow_unregistered: bool = False,
) -> Admission:
    """返回是否接收以及是否应触发回复处理。"""
    settings = get_settings()
    tenant = tenant or settings.tenant_id
    source = _find_source(session, tenant, incoming.chat_id, incoming.thread_id)
    if source is None and allow_unregistered:
        source = ensure_source(
            session,
            tenant=tenant,
            owner=settings.feishu_owner_open_id,
            chat_id=incoming.chat_id,
            thread_id=incoming.thread_id,
            source_type=incoming.chat_type or "p2p",
            reply_allowed=False,
        )
    if source is None:
        return Admission(False, "source_missing")
    if source.revoked_at is not None:
        return Admission(False, "source_revoked", source)
    if not source.enabled:
        return Admission(False, "source_disabled", source)
    if not settings.is_whitelisted(source.chat_id):
        return Admission(False, "chat_not_whitelisted", source)
    if incoming.msg_type not in source.allowed_type_set:
        return Admission(False, "content_type_not_allowed", source)
    if not incoming.sender_id:
        return Admission(False, "sender_unknown", source)
    if incoming.is_from_bot:
        return Admission(False, "bot_echo", source)

    if incoming.chat_type == "group":
        if settings.feishu_group_reply_only_mention:
            if not incoming.bot_open_id:
                return Admission(False, "bot_identity_unknown", source)
            if not incoming.mentions_bot:
                return Admission(True, "group_no_mention_ingest_only", source, False)
        return Admission(True, "group_mention_ok", source, True)

    return Admission(True, "p2p_ok", source, True)


def mark_source_coverage(
    session: Session, source: Source, *, status: str, watermark_ms: int | None = None
) -> None:
    source.coverage_status = status
    if watermark_ms is not None:
        source.last_success_watermark_ms = max(
            int(source.last_success_watermark_ms or 0), int(watermark_ms)
        )
    if status == CoverageStatus.COMPLETE:
        from app.core.clock import utcnow

        source.last_success_at = utcnow()
    session.flush()
