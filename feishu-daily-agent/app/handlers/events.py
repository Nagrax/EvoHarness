"""飞书消息事件：先短事务持久化并确认，再后台处理派生对象。"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.database import session_scope
from app.domain import admission, intake
from app.feishu.messages import IncomingMessage, from_event
from app.handlers.commands import try_handle_command
from app.models import Chat, InboxEvent, Message, ReplyDecision, Source
from app.services.reply_engine import process_incoming

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu-msg")
_COMMAND_RE = re.compile(r"^(改|验证|停用|驳回|完成|重开|纠正)\s*#?\d+")
_replay_started = False


def _tenant() -> str:
    return get_settings().tenant_id


def _mark_processed(event_id: str, message_id: str = "") -> None:
    try:
        with session_scope() as session:
            intake.mark_event_processed(session, event_id=event_id, message_id=message_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("标记事件已处理失败 %s: %s", event_id or message_id, exc)


def persist_event(incoming: IncomingMessage, *, event_id: str = "") -> tuple[bool, str, bool]:
    """落盘准入与消息本体；返回 (accepted, reason, should_process)。"""
    with session_scope() as session:
        intake.record_event(
            session,
            tenant=_tenant(),
            event_id=event_id or f"msg:{incoming.message_id}",
            event_type="im.message.receive_v1",
            message_id=incoming.message_id,
            payload=incoming.raw or incoming.text,
        )
        result = admission.admit(session, incoming, allow_unregistered=False)
        if not result.accepted:
            return False, result.reason, False
        intake.ingest_message(session, incoming, source=result.source)
        return True, result.reason, result.should_process


def _safe_process(incoming: IncomingMessage, event_id: str = "") -> None:
    try:
        with session_scope() as session:
            process_incoming(session, incoming, allow_unregistered=False)
    except Exception as exc:  # noqa: BLE001
        logger.error("消息 %s 自动处理失败: %s", incoming.message_id, exc)
        return
    _mark_processed(event_id, incoming.message_id)


def replay_recent_questions(*, window_seconds: int = 21600, limit: int = 10) -> int:
    """重放崩溃窗口内已落盘但未产生决策的问题。

    只处理最近 window 内、非机器人、非指令、且没有决策/未 handled 的消息；
    指令一律跳过并标记，避免重放造成重复发送。返回实际重放条数。
    """
    settings = get_settings()
    cutoff = utcnow() - timedelta(seconds=max(60, window_seconds))
    candidates: list[tuple[str, IncomingMessage, int]] = []
    with session_scope() as session:
        events = session.scalars(
            select(InboxEvent)
            .where(InboxEvent.processed_at.is_(None), InboxEvent.received_at >= cutoff)
            .order_by(InboxEvent.id)
            .limit(limit)
        ).all()
        for event in events:
            message = session.scalar(
                select(Message).where(
                    Message.tenant == event.tenant,
                    Message.message_id == event.message_id,
                )
            )
            text = (message.content_text or "").strip() if message else ""
            if message is None or message.is_from_bot or _COMMAND_RE.match(text):
                event.processed_at = utcnow()
                continue
            decision = session.scalar(
                select(ReplyDecision.id).where(
                    ReplyDecision.tenant == message.tenant,
                    ReplyDecision.message_id == message.message_id,
                )
            )
            source = session.scalar(
                select(Source).where(
                    Source.tenant == message.tenant,
                    Source.chat_id == message.chat_id,
                    Source.enabled.is_(True),
                    Source.revoked_at.is_(None),
                )
            )
            if decision is not None or message.handled or source is None:
                event.processed_at = utcnow()
                continue
            chat = session.get(Chat, message.chat_id)
            candidates.append((event.event_id, IncomingMessage(
                message_id=message.message_id,
                chat_id=message.chat_id,
                chat_type=chat.chat_type if chat else "p2p",
                sender_id=message.sender_id,
                sender_type="user",
                msg_type=message.msg_type or "text",
                text=message.content_text or "",
                create_time=message.create_time or 0,
                thread_id=message.thread_id or "",
                sender_name=message.sender_name or "",
                mention_ids=(
                    [settings.feishu_bot_open_id]
                    if message.is_mention and settings.feishu_bot_open_id
                    else []
                ),
                event_time_ms=message.event_time_ms or 0,
            ), source.id))
        session.flush()
    for event_id, incoming, source_id in candidates:
        try:
            with session_scope() as session:
                source = session.get(Source, source_id)
                if source is None:
                    continue
                process_incoming(
                    session, incoming, source=source, allow_unregistered=False
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("重放消息 %s 失败: %s", incoming.message_id, exc)
            continue
        _mark_processed(event_id, incoming.message_id)
    return len(candidates)


def start_replay_worker() -> None:
    """启动崩溃恢复重放线程；重复调用只启动一次。"""
    global _replay_started
    settings = get_settings()
    if _replay_started or not settings.inbox_replay_enabled:
        return
    _replay_started = True

    def loop() -> None:
        while True:
            try:
                replayed = replay_recent_questions(
                    window_seconds=settings.inbox_replay_window_seconds,
                    limit=max(1, settings.inbox_replay_batch),
                )
                if replayed:
                    logger.info("收件箱重放完成: %s 条", replayed)
            except Exception as exc:  # noqa: BLE001
                logger.error("收件箱重放失败: %s", exc)
            time.sleep(max(30, settings.inbox_replay_interval_seconds))

    threading.Thread(target=loop, daemon=True, name="feishu-inbox-replay").start()


def handle_message_event(data) -> None:
    try:
        incoming = from_event(data.event)
    except Exception as exc:  # noqa: BLE001
        logger.error("事件消息解析失败: %s", exc)
        return
    event_id = getattr(getattr(data, "header", None), "event_id", "") or ""
    try:
        accepted, reason, should_process = persist_event(incoming, event_id=event_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("消息 %s 持久化失败（不确认事件）: %s", incoming.message_id, exc)
        raise
    if not accepted:
        logger.info("消息 %s 未准入: %s", incoming.message_id, reason)
        _mark_processed(event_id, incoming.message_id)
        return
    if incoming.is_from_bot or not should_process:
        _mark_processed(event_id, incoming.message_id)
        return
    try:
        if try_handle_command(incoming):
            _mark_processed(event_id, incoming.message_id)
            return
    except Exception as exc:  # noqa: BLE001
        logger.error("指令处理失败: %s", exc)
        _mark_processed(event_id, incoming.message_id)
        return
    _executor.submit(_safe_process, incoming, event_id)
