"""每日流水线：授权会话同步、历史补采、任务/经验更新与日报。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import current_logical_date, previous_day_window, utcnow
from app.core.config import get_settings
from app.core.database import session_scope
from app.domain import admission, intake, reports
from app.domain import outbox as outbox_module
from app.domain import tasks as tasks_module
from app.feishu.transport import get_transport
from app.models import Message, Source, SystemState
from app.services.reply_engine import process_incoming, upsert_chat

logger = logging.getLogger(__name__)


def bootstrap_sources(session: Session, *, transport=None) -> list[Source]:
    settings = get_settings()
    transport = transport or get_transport()
    discovered: list[dict] = []
    discovery_ok = False
    try:
        discovered = transport.list_chats()
        discovery_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("列举会话失败，使用白名单登记来源: %s", exc)
    by_chat = {item["chat_id"]: item for item in discovered}
    chat_ids = set(settings.whitelist) | set(settings.reply_allowed_chats)
    chat_ids |= set(by_chat)
    sources: list[Source] = []
    for chat_id in sorted(chat_ids):
        if not settings.is_whitelisted(chat_id):
            continue
        info = by_chat.get(chat_id, {})
        chat_type = info.get("chat_type") or ("group" if chat_id.startswith("oc_") else "p2p")
        upsert_chat(session, chat_id, chat_type, info.get("name", ""))
        source = admission.ensure_source(
            session,
            tenant=settings.tenant_id,
            owner=settings.feishu_owner_open_id,
            chat_id=chat_id,
            source_type=chat_type,
            discovery="bot_list" if chat_id in by_chat else "manual",
            read_identity="bot",
            reply_identity=settings.feishu_reply_identity,
            reply_allowed=settings.reply_allowed_for(chat_id),
        )
        # 配置移除会话后，已有 Source 也必须同步撤权。
        source.reply_allowed = settings.reply_allowed_for(chat_id)
        source.reply_identity = settings.feishu_reply_identity
        sources.append(source)
    _reconcile_group_revocations(session, by_chat, sources, discovery_ok=discovery_ok)
    session.flush()
    return sources


def _reconcile_group_revocations(
    session: Session, by_chat: dict, sources: list[Source], *, discovery_ok: bool
) -> None:
    """群来源撤权对账（F01 实机发现的缺口）。

    机器人被移出群或群解散后，list_chats 不再返回该群；此前系统没有任何代码
    路径写入 revoked_at，来源会永久保持 enabled 并继续出现在日报与预检里。
    - 发现成功且群不再可见 → 记 revoked_at（admission/outbox/健康/日报据此阻断）；
    - 群重新可见 → 清除 revoked_at（重新入群恢复）；
    - 列举失败（discovery_ok=False）时不做批量撤权，避免抖动误杀。
    p2p 来源不在 bot_list 里，不参与本对账。
    """
    if not discovery_ok:
        return
    seen = set(sources)
    existing = list(
        session.scalars(
            select(Source).where(
                Source.tenant == get_settings().tenant_id, Source.source_type == "group"
            )
        )
    )
    for source in existing:
        if source.thread_id:
            continue
        if source.chat_id in by_chat:
            if source.revoked_at is not None:
                source.revoked_at = None
        elif source.revoked_at is None:
            source.revoked_at = utcnow()


def _process_historical_messages(
    session: Session, source: Source, start_ms: int, end_ms: int
) -> int:
    messages = list(
        session.scalars(
            select(Message)
            .where(
                Message.tenant == source.tenant,
                Message.chat_id == source.chat_id,
                Message.create_time >= start_ms,
                Message.create_time < end_ms,
                Message.recalled.is_(False),
                Message.is_from_bot.is_(False),
            )
            .order_by(Message.create_time)
        )
    )
    processed = 0
    for message in messages:
        if message.handled:
            continue
        incoming = type(
            "HistoricalIncoming",
            (),
            {
                "message_id": message.message_id,
                "chat_id": message.chat_id,
                "chat_type": source.source_type,
                "sender_id": message.sender_id,
                "sender_type": "user",
                "msg_type": message.msg_type,
                "text": message.content_text,
                "create_time": message.create_time,
                "thread_id": message.thread_id,
                "sender_name": message.sender_name,
                "mention_ids": [],
                "mention_names": [],
                "raw": message.raw_json,
                "event_time_ms": message.event_time_ms,
                "is_from_bot": False,
                "mentions_bot": False,
            },
        )()
        process_incoming(
            session, incoming, source=source, shadow_override=True, now=utcnow()
        )
        processed += 1
    return processed


def sync_and_process(session: Session, *, transport=None, target_date: date | None = None) -> dict:
    settings = get_settings()
    transport = transport or get_transport()
    target_date = target_date or (current_logical_date(settings.daily_report_timezone) - timedelta(days=1))
    start_ms, end_ms = previous_day_window(target_date, settings.daily_report_timezone)
    sources = [source for source in bootstrap_sources(session, transport=transport)]
    result = {"sources": len(sources), "synced": 0, "partial": 0, "processed": 0}
    for source in sources:
        if source.revoked_at is not None or not source.enabled:
            continue
        sync = intake.sync_source_window(
            session,
            source,
            start_ms=start_ms,
            end_ms=end_ms,
            fetcher=transport,
            overlap_ms=300_000,
        )
        result["synced"] += 1
        if sync.status != "complete":
            result["partial"] += 1
            logger.warning("来源 %s 补采未完成: %s", source.chat_id, sync.error)
            continue
        result["processed"] += _process_historical_messages(session, source, start_ms, end_ms)
    return result


def _last_report_date(session: Session, tenant: str, owner: str) -> date | None:
    rows = list(
        session.scalars(
            select(reports.DailyReport)
            .where(reports.DailyReport.tenant == tenant, reports.DailyReport.owner == owner)
            .order_by(reports.DailyReport.logical_date.desc())
            .limit(1)
        )
    )
    if not rows:
        return None
    return date.fromisoformat(rows[0].logical_date)


def run_daily_pipeline(
    *,
    now: datetime | None = None,
    transport=None,
    report_date: date | None = None,
) -> dict:
    settings = get_settings()
    now = now or utcnow()
    transport = transport or get_transport()
    report_date = report_date or current_logical_date(settings.daily_report_timezone)
    summary = {"reports": [], "outbox": [], "sync": {}}
    try:
        with session_scope() as session:
            last = _last_report_date(session, settings.tenant_id, settings.feishu_owner_open_id)
            start = (last + timedelta(days=1)) if last else report_date
            # 已生成当天日报后仍要重算最近一天，才能吸收迟到消息/回执。
            if start > report_date:
                start = report_date
            if start < report_date - timedelta(days=7):
                start = report_date - timedelta(days=7)
            sync_results = []
            sync_cursor = start
            while sync_cursor <= report_date:
                sync_results.append(
                    sync_and_process(
                        session, transport=transport,
                        target_date=sync_cursor - timedelta(days=1),
                    )
                )
                sync_cursor += timedelta(days=1)
            summary["sync"] = {
                "runs": sync_results,
                "sources": max((item["sources"] for item in sync_results), default=0),
                "processed": sum(item["processed"] for item in sync_results),
                "partial": sum(item["partial"] for item in sync_results),
            }
            cursor = start
            while cursor <= report_date:
                generated = reports.generate_report(
                    session,
                    logical_date=cursor,
                    tenant=settings.tenant_id,
                    owner=settings.feishu_owner_open_id,
                    timezone=settings.daily_report_timezone,
                )
                summary["reports"].append(
                    {
                        "date": cursor.isoformat(),
                        "created": generated.created,
                        "revised": generated.revised,
                        "outbox_id": generated.outbox_id,
                    }
                )
                cursor += timedelta(days=1)
    except Exception as exc:  # noqa: BLE001 - 调度器不能中断
        logger.exception("每日流水线失败")
        summary["error"] = str(exc)
        return summary
    try:
        result = outbox_module.dispatch_outbox(
            __import__("app.core.database", fromlist=["SessionLocal"]).SessionLocal,
            transport=transport,
        )
        summary["outbox"] = [
            {"id": item.outbox_id, "status": item.status, "error": item.error}
            for item in result
        ]
    except Exception as exc:  # noqa: BLE001
        logger.exception("日报投递失败")
        summary["error"] = str(exc)
    return summary


def _day_window() -> tuple[int, int]:
    """兼容旧调用：昨天自然日窗口。"""
    settings = get_settings()
    today = current_logical_date(settings.daily_report_timezone)
    return previous_day_window(today - timedelta(days=1), settings.daily_report_timezone)


def _chat_allowed(chat_id: str) -> bool:
    return get_settings().is_whitelisted(chat_id)


def run_daily_pipeline_compat() -> None:
    run_daily_pipeline()
