"""持久接收、消息修订与历史补采。

实时事件与历史补采走同一个 `ingest_message`；事件在确认前已经落库，
处理失败只影响后续派生对象，不会丢消息。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.ids import sha256_json, sha256_text
from app.feishu.messages import IncomingMessage
from app.models import (
    CoverageStatus,
    InboxEvent,
    Message,
    ReplyDraft,
    DraftStatus,
    Source,
    SourceSync,
    Task,
    TaskEvent,
    TaskStatus,
)


@dataclass
class IntakeResult:
    message: Message
    created: bool
    edited: bool
    recalled: bool


@dataclass
class Page:
    items: list
    next_token: str = ""
    has_more: bool = False


class HistoryFetcher(Protocol):
    def fetch_page(
        self, chat_id: str, thread_id: str, start_ms: int, end_ms: int, page_token: str
    ) -> Page: ...


@dataclass
class SyncResult:
    status: str
    pages: int
    ingested: int
    error: str = ""


def record_event(
    session: Session,
    *,
    tenant: str,
    event_id: str,
    event_type: str,
    message_id: str,
    payload: object,
) -> bool:
    """写入事件去重记录；返回 True 表示首次收到。"""
    if event_id:
        existing = session.scalar(
            select(InboxEvent).where(InboxEvent.event_id == event_id)
        )
        if existing is not None:
            return False
    payload_hash = sha256_json(payload)
    session.add(
        InboxEvent(
            tenant=tenant,
            event_id=event_id or sha256_text(payload_hash),
            event_type=event_type,
            message_id=message_id,
            payload_hash=payload_hash,
        )
    )
    session.flush()
    return True


def _find_message(session: Session, tenant: str, message_id: str) -> Message | None:
    return session.scalar(
        select(Message).where(Message.tenant == tenant, Message.message_id == message_id)
    )


def ingest_message(
    session: Session,
    incoming: IncomingMessage,
    *,
    source: Source | None = None,
    tenant: str | None = None,
    raw_payload: str = "",
    raw_retention_days: int | None = None,
) -> IntakeResult:
    settings = get_settings()
    tenant = tenant or settings.tenant_id
    existing = _find_message(session, tenant, incoming.message_id)
    content_hash = sha256_text(incoming.text or "")
    if existing is not None:
        if existing.recalled:
            return IntakeResult(existing, False, False, True)
        if content_hash and content_hash != existing.content_hash:
            _apply_edit(session, existing, incoming, content_hash)
            return IntakeResult(existing, False, True, False)
        return IntakeResult(existing, False, False, False)

    retention = raw_retention_days
    if retention is None:
        retention = settings.raw_payload_retention_days
    now = utcnow()
    message = Message(
        message_id=incoming.message_id,
        tenant=tenant,
        source_id=source.id if source else None,
        chat_id=incoming.chat_id,
        thread_id=incoming.thread_id,
        sender_id=incoming.sender_id,
        sender_name=incoming.sender_name,
        msg_type=incoming.msg_type,
        content_text=incoming.text,
        content_hash=content_hash,
        create_time=incoming.create_time,
        event_time_ms=incoming.event_time_ms or incoming.create_time,
        is_from_bot=incoming.is_from_bot,
        is_mention=incoming.mentions_bot,
        raw_json=raw_payload or incoming.raw,
        raw_retention_until=(now + timedelta(days=retention)) if retention else None,
        created_at=now,
        updated_at=now,
    )
    session.add(message)
    session.flush()
    return IntakeResult(message, True, False, False)


def _apply_edit(
    session: Session, message: Message, incoming: IncomingMessage, content_hash: str
) -> None:
    message.content_text = incoming.text
    message.content_hash = content_hash
    message.revision = int(message.revision or 1) + 1
    message.edited = True
    message.event_time_ms = incoming.event_time_ms or incoming.create_time
    message.updated_at = utcnow()
    _invalidate_for_message(session, message, reason="message_edited")


def mark_recalled(
    session: Session, *, tenant: str, message_id: str, event_time_ms: int = 0
) -> Message | None:
    message = _find_message(session, tenant, message_id)
    if message is None:
        return None
    message.recalled = True
    message.content_text = "[recalled]"
    message.event_time_ms = event_time_ms or message.event_time_ms
    message.updated_at = utcnow()
    _invalidate_for_message(session, message, reason="message_recalled")
    return message


def _invalidate_for_message(session: Session, message: Message, *, reason: str) -> None:
    # 编辑/撤回后必须重新判定；清掉旧决策与 handled，否则问题会被永久跳过。
    message.handled = False
    from app.models import ReplyDecision

    for decision in session.scalars(
        select(ReplyDecision).where(
            ReplyDecision.tenant == message.tenant,
            ReplyDecision.message_id == message.message_id,
        )
    ):
        session.delete(decision)
    # 旧草稿作废；已发回执保留，不覆盖 sent_message_id。
    for draft in session.scalars(
        select(ReplyDraft).where(
            ReplyDraft.message_id == message.message_id,
            ReplyDraft.status.in_([DraftStatus.PENDING, DraftStatus.APPROVED]),
        )
    ):
        draft.status = DraftStatus.SUPERSEDED
        draft.note = reason
    # 待确认任务需要重新核对证据。
    for task in session.scalars(
        select(Task).where(Task.tenant == message.tenant, Task.status != TaskStatus.DONE)
    ):
        if message.message_id in task.source_ids():
            task.needs_confirmation = True
            if task.status == TaskStatus.OPEN:
                task.status = TaskStatus.NEEDS_CONFIRMATION
            session.add(
                TaskEvent(
                    task_id=task.id,
                    event_type=reason,
                    actor="system",
                    detail="来源消息变更，任务需重新确认",
                    evidence_message_ids=json.dumps([message.message_id], ensure_ascii=False),
                )
            )
    # 经验影响分析由经验模块订阅；这里避免循环导入，调用可选钩子。
    try:
        from app.memory.experience import invalidate_experience_for_message

        invalidate_experience_for_message(session, message, reason=reason)
    except ImportError:
        pass
    session.flush()


def _trim(items: Iterable[IncomingMessage], start_ms: int, end_ms: int):
    for item in items:
        if start_ms <= int(item.create_time) < end_ms:
            yield item


class SyncStateError(RuntimeError):
    pass


def sync_source_window(
    session: Session,
    source: Source,
    *,
    start_ms: int,
    end_ms: int,
    fetcher: HistoryFetcher,
    overlap_ms: int = 0,
    page_limit: int = 1000,
) -> SyncResult:
    """按页持久化补采；整窗成功后才封口并推进水位。"""
    state = session.scalar(
        select(SourceSync).where(
            SourceSync.source_id == source.id,
            SourceSync.window_start_ms == start_ms,
            SourceSync.window_end_ms == end_ms,
        )
    )
    if state is None:
        state = SourceSync(
            source_id=source.id,
            window_start_ms=start_ms,
            window_end_ms=end_ms,
            status=CoverageStatus.PARTIAL,
        )
        session.add(state)
        session.flush()
    if state.status == CoverageStatus.COMPLETE:
        return SyncResult(CoverageStatus.COMPLETE, state.pages_saved, 0)

    token = state.cursor or ""
    pages = 0
    ingested = 0
    effective_start = start_ms
    if not token and overlap_ms:
        effective_start = max(0, start_ms - overlap_ms)
    try:
        while True:
            page: Page = fetcher.fetch_page(
                source.chat_id, source.thread_id, effective_start, end_ms, token
            )
            for incoming in _trim(page.items, start_ms, end_ms):
                result = ingest_message(session, incoming, source=source, tenant=source.tenant)
                if result.created:
                    ingested += 1
            pages += 1
            state.pages_saved = int(state.pages_saved or 0) + 1
            state.cursor = page.next_token or ""
            token = state.cursor
            state.updated_at = utcnow()
            session.flush()
            if not page.has_more:
                break
            if not page.next_token:
                raise SyncStateError("分页缺失 next_token，补采窗口未封口")
            if pages > page_limit:
                raise SyncStateError("补采分页超过安全上限")
    except Exception as exc:  # noqa: BLE001 - 失败必须落盘为 partial
        state.status = CoverageStatus.PARTIAL
        state.error_code = type(exc).__name__
        state.error_detail = str(exc)[:1000]
        state.updated_at = utcnow()
        source.coverage_status = CoverageStatus.PARTIAL
        session.flush()
        return SyncResult(CoverageStatus.PARTIAL, pages, ingested, str(exc))

    state.status = CoverageStatus.COMPLETE
    state.error_code = ""
    state.error_detail = ""
    state.completed_at = utcnow()
    state.updated_at = utcnow()
    source.coverage_status = CoverageStatus.COMPLETE
    source.last_success_watermark_ms = max(
        int(source.last_success_watermark_ms or 0), end_ms - 1
    )
    source.last_success_at = utcnow()
    session.flush()
    return SyncResult(CoverageStatus.COMPLETE, pages, ingested)
