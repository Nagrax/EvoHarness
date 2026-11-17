"""历史轮询兜底：长连接事件不可用时仍能形成闭环。

为什么需要它
------------
事件订阅（`im.message.receive_v1`）与卡片回调的配置变更，只有**发布新版本
并通过企业管理员审核后**才会生效。在审核窗口里，长连接虽然连上了，但收不到
任何推送——"连上了"与"收到了"是两件事。

而 `im/v1/messages` 历史接口在 `im:message` 权限下即可用。本模块按秒级
轮询授权会话的最近消息，把**尚未入库的用户消息**走与实时事件完全相同的
准入/分类/经验匹配/草稿链路，因此闭环不依赖审核结果。

边界：
- 只轮询已授权来源（白名单 / owner 会话），不扫描未知会话。
- 幂等：已入库的消息按 message_id 去重，重复轮询不会重复出草稿。
- 机器人自己的回声由 `admission.admit` 的 bot_echo 规则挡掉。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import epoch_ms, utcnow
from app.core.config import get_settings
from app.core.database import session_scope
from app.domain import admission, intake
from app.feishu.messages import IncomingMessage
from app.feishu.transport import get_transport
from app.models import Message, Source
from app.services.reply_engine import process_incoming

logger = logging.getLogger(__name__)


def authorized_sources(session: Session) -> list[Source]:
    settings = get_settings()
    sources = list(
        session.scalars(
            select(Source).where(
                Source.tenant == settings.tenant_id,
                Source.enabled.is_(True),
                Source.revoked_at.is_(None),
            )
        )
    )
    return [s for s in sources if settings.is_whitelisted(s.chat_id)]


def ensure_owner_source(session: Session) -> Source | None:
    """轮询前保证 owner 会话已登记来源（否则首轮会无来源可轮询）。"""
    settings = get_settings()
    chat_id = (settings.feishu_owner_chat_id or "").strip()
    if not chat_id:
        return None
    return admission.ensure_source(
        session,
        tenant=settings.tenant_id,
        owner=settings.feishu_owner_open_id,
        chat_id=chat_id,
        source_type="p2p",
        discovery="owner_config",
        read_identity="bot",
        reply_identity=settings.feishu_reply_identity,
        reply_allowed=settings.reply_allowed_for(chat_id),
    )


def poll_once(*, transport=None, window_seconds: int | None = None) -> dict:
    """拉取授权会话最近消息并处理增量；返回统计。"""
    settings = get_settings()
    window_seconds = window_seconds or settings.poll_window_seconds
    transport = transport or get_transport()
    # utcnow() 是 naive UTC：直接 .timestamp() 会被当成本地时间，差 8 小时，
    # 窗口整体偏到 8 小时前，抓回 0 条（2026-11-10 实机踩到）。统一走 epoch_ms。
    now_ms = epoch_ms()
    start_ms = epoch_ms(utcnow() - timedelta(seconds=max(60, window_seconds)))
    stats = {"chats": 0, "fetched": 0, "new": 0, "processed": 0, "errors": []}

    with session_scope() as session:
        ensure_owner_source(session)
        sources = authorized_sources(session)
        chat_ids = [(s.chat_id, s.thread_id, s.id) for s in sources]

    for chat_id, thread_id, source_id in chat_ids:
        stats["chats"] += 1
        token = ""
        while True:
            try:
                page = transport.fetch_page(chat_id, thread_id, start_ms, now_ms, token)
            except Exception as exc:  # noqa: BLE001 — 单个会话失败不拖垮整轮
                stats["errors"].append(f"{chat_id[-6:]}: {type(exc).__name__}: {exc}"[:160])
                break
            for incoming in page.items:
                stats["fetched"] += 1
                if _handle(incoming, source_id, settings):
                    stats["new"] += 1
            if not page.has_more or not page.next_token:
                break
            token = page.next_token
    stats["processed"] = stats["new"]
    return stats


def _handle(incoming: IncomingMessage, source_id: int, settings) -> bool:
    """入库并处理一条历史消息；已存在或不该处理时返回 False。"""
    with session_scope() as session:
        existing = session.scalar(
            select(Message).where(
                Message.tenant == settings.tenant_id,
                Message.message_id == incoming.message_id,
            )
        )
        if existing is not None:
            return False
        source = session.get(Source, source_id)
        if source is None:
            return False
        verdict = admission.admit(session, incoming)
        if not verdict.accepted:
            # 未准入的消息也要入库（可审计），但不触发回复。
            try:
                intake.ingest_message(session, incoming, source=source)
            except Exception:  # noqa: BLE001
                pass
            logger.info("轮询消息 %s 未准入: %s", incoming.message_id[-6:], verdict.reason)
            return False
        if not verdict.should_process:
            intake.ingest_message(session, incoming, source=source)
            return True
        process_incoming(session, incoming, source=source)
        return True


def poll_summary_lines(stats: dict) -> list[str]:
    lines = [
        f"轮询：{stats['chats']} 个会话，抓取 {stats['fetched']} 条，新增 {stats['new']} 条"
    ]
    for err in stats.get("errors") or []:
        lines.append(f"  ⚠️ {err}")
    return lines


def recent_window_bounds(window_seconds: int) -> tuple[int, int]:
    now = utcnow()
    return (
        epoch_ms(now - timedelta(seconds=window_seconds)),
        epoch_ms(now),
    )
