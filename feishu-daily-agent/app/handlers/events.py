"""飞书消息事件：先短事务持久化并确认，再后台处理派生对象。"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.core.config import get_settings
from app.core.database import session_scope
from app.domain import admission, intake
from app.feishu.messages import IncomingMessage, from_event
from app.handlers.commands import try_handle_command
from app.services.reply_engine import process_incoming

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu-msg")


def _tenant() -> str:
    return get_settings().tenant_id


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


def _safe_process(incoming: IncomingMessage) -> None:
    try:
        with session_scope() as session:
            process_incoming(session, incoming, allow_unregistered=False)
    except Exception as exc:  # noqa: BLE001
        logger.error("消息 %s 自动处理失败: %s", incoming.message_id, exc)


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
        return
    if incoming.is_from_bot or not should_process:
        return
    try:
        if try_handle_command(incoming):
            return
    except Exception as exc:  # noqa: BLE001
        logger.error("指令处理失败: %s", exc)
    _executor.submit(_safe_process, incoming)
