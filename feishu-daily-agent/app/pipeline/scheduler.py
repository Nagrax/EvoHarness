"""APScheduler：日报定时、outbox 重试与心跳。"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

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
