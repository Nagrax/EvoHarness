"""确定性准入：实时事件与历史补采共用同一套规则。

硬约束：
- 来源必须显式登记且启用；空白名单/未知身份/撤权按关闭处理。
- 机器人身份未知时，群聊 @ 不能被当作“@机器人”放行。
- 读许可不能推导写许可；回复许可单独存在 Source.reply_allowed。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    """登记/获取某使用者在某会话的来源。

    查找键包含 owner：同一个群里每个分身各有自己的一条来源，
    不能复用别人的（否则批准权会串到别人身上）。
    """
    owner = (owner or "").strip()
    source = session.scalar(
        select(Source).where(
            Source.tenant == tenant,
            Source.chat_id == chat_id,
            Source.thread_id == thread_id,
            Source.owner == owner,
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
        # 用 SAVEPOINT 包住插入：并发登记撞唯一约束时只回滚这一条，
        # 不能 session.rollback()——那会把外层已写入的 inbox 事件一起回滚。
        # 注意顺序：begin_nested 的 _take_snapshot 会 flush 当前挂起对象，
        # 所以必须先建 SAVEPOINT 再 add——否则待插入的 Source 在进入 try 之前
        # 就被 flush，IntegrityError 绕过 savepoint 保护直接逃逸（并发首条消息
        # 偶发抛 UNIQUE 约束错误的根因）。
        savepoint = session.begin_nested()
        try:
            session.add(source)
            session.flush()
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            source = session.scalar(
                select(Source).where(
                    Source.tenant == tenant,
                    Source.chat_id == chat_id,
                    Source.thread_id == thread_id,
                    Source.owner == owner,
                )
            )
            if source is None:
                raise
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


def _is_owner_chat(settings, chat_id: str) -> bool:
    owner_chat = (settings.feishu_owner_chat_id or "").strip()
    return bool(owner_chat) and owner_chat == (chat_id or "").strip()


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
    if source is None and _is_owner_chat(settings, incoming.chat_id):
        # 显式配置的 owner 会话（FEISHU_OWNER_CHAT_ID）本身就是授权声明：
        # 首条消息到达时即可登记来源，否则实时链路会因 source_missing 静默丢消息。
        source = ensure_source(
            session,
            tenant=tenant,
            owner=settings.feishu_owner_open_id,
            chat_id=incoming.chat_id,
            thread_id=incoming.thread_id,
            source_type=incoming.chat_type or "p2p",
            discovery="owner_config",
            reply_allowed=settings.business_reply_enabled,
        )
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
    if source is None and settings.feishu_allow_all_chats and settings.is_whitelisted(
        incoming.chat_id
    ):
        # 全放行模式下，机器人被拉进的新群/新私聊在首次发言时自动登记来源。
        # 否则该群要等到下一次日报 bootstrap（最多一天）才会出现在 sources 里，
        # 期间消息会以 source_missing 被静默丢弃——"拉进群就能用"会变成
        # "拉进群要等一天"。仍受黑名单约束（is_whitelisted 已判）。
        source = ensure_source(
            session,
            tenant=tenant,
            owner=settings.feishu_owner_open_id,
            chat_id=incoming.chat_id,
            thread_id=incoming.thread_id,
            source_type=incoming.chat_type or "p2p",
            discovery="allow_all",
            reply_allowed=settings.reply_allowed_for(incoming.chat_id),
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
            # 旧行为：群聊必须 @ 才处理（这是"没有审批环节时代"的防刷屏手段）。
            if not incoming.bot_open_id:
                return Admission(False, "bot_identity_unknown", source)
            if not incoming.mentions_bot:
                return Admission(True, "group_no_mention_ingest_only", source, False)
            return Admission(True, "group_mention_ok", source, True)
        # 免 @ 模式：群消息照常处理，是否"出声"由下游决定。
        # 关键：没命中经验时下游不发言（见 reply_engine 的 silent 分支），
        # 所以免 @ 不等于在群里刷屏。
        return Admission(True, "group_no_mention_mode", source, True)

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
