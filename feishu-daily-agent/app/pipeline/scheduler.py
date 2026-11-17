"""APScheduler：日报定时、outbox 重试与心跳。"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal, session_scope
from app.domain import health
from app.domain import outbox as outbox_module
from app.pipeline.daily import run_daily_pipeline

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _worker_tick() -> None:
    try:
        outbox_module.dispatch_outbox(SessionLocal, limit=20)
        health.heartbeat("worker", {"status": "ok"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("outbox worker tick 失败")
        health.heartbeat("worker", {"status": "error", "error": str(exc)})


def _daily_job() -> None:
    try:
        summary = run_daily_pipeline()
        health.heartbeat("daily", {"status": "ok", "summary": summary})
    except Exception as exc:  # noqa: BLE001
        logger.exception("日报任务失败")
        health.heartbeat("daily", {"status": "error", "error": str(exc)})


def _poll_job() -> None:
    """历史轮询兜底：事件订阅未生效时，闭环依然靠这里推进。"""
    from app.pipeline import poller

    try:
        stats = poller.poll_once()
        if stats["new"]:
            logger.info("轮询新增 %s 条消息", stats["new"])
        health.heartbeat("poller", {"status": "ok", "stats": stats})
    except Exception as exc:  # noqa: BLE001
        logger.exception("历史轮询失败")
        health.heartbeat("poller", {"status": "error", "error": str(exc)})


def _refresh_twin_tokens() -> None:
    """主动续期数字分身的 user_access_token。

    为什么需要主动跑：`refresh_token` 有效期 7 天，且**每次续期会轮换**。
    如果只依赖"发消息时按需刷新"，某个使用者连续 7 天不用，refresh_token
    就会失效，只能重新授权。这里每 12 小时刷一次，保证在册分身不过期。

    失败不抛出：续期失败只更新 `last_error`，等人工或下次重试；
    抛异常会让 APScheduler 记录错误栈但对业务毫无帮助。
    """
    from app.core.database import session_scope
    from app.services import identity
    from app.models import TwinIdentity

    refreshed = failed = 0
    try:
        with session_scope() as session:
            open_ids = [
                row.open_id
                for row in session.scalars(
                    select(TwinIdentity).where(TwinIdentity.authorized.is_(True))
                )
            ]
        for open_id in open_ids:
            with session_scope() as session:
                if identity.usable_access_token(session, open_id):
                    refreshed += 1
                else:
                    failed += 1
        if open_ids:
            logger.info(
                "分身 token 续期：成功 %s，失败 %s（共 %s）",
                refreshed, failed, len(open_ids),
            )
        health.heartbeat(
            "twin_refresh",
            {"status": "ok", "total": len(open_ids),
             "refreshed": refreshed, "failed": failed},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("分身 token 续期任务失败")
        health.heartbeat("twin_refresh", {"status": "error", "error": str(exc)})


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    settings = get_settings()
    hour, minute = settings.daily_report_time.split(":")
    scheduler = BackgroundScheduler(timezone=settings.daily_report_timezone)
    scheduler.add_job(
        func=_daily_job,
        trigger=CronTrigger(hour=int(hour), minute=int(minute)),
        id="daily_pipeline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=6 * 3600,
    )
    scheduler.add_job(
        func=_worker_tick,
        trigger=IntervalTrigger(seconds=30),
        id="outbox_worker",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    if settings.poll_enabled and settings.deployment_mode != "offline":
        scheduler.add_job(
            func=_poll_job,
            trigger=IntervalTrigger(seconds=max(15, settings.poll_interval_seconds)),
            id="history_poller",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
        )
    # 分身 token 主动续期：refresh_token 有效期 7 天且每次续期轮换，
    # 若只依赖"发消息时按需刷新"，连续 7 天不用的分身会静默失效。
    # 每 12 小时主动刷一次，保证任何在册分身都不会过期。
    scheduler.add_job(
        func=_refresh_twin_tokens,
        trigger=IntervalTrigger(hours=12),
        id="twin_token_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    health.heartbeat("scheduler", {"status": "started", "daily": settings.daily_report_time})
    logger.info(
        "定时调度已启动，每日 %s (%s) 生成日报",
        settings.daily_report_time,
        settings.daily_report_timezone,
    )
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
