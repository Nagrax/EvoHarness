"""独立健康探针：心跳、积压、水位、回执未知、Run 租约、调度漏跑与投影（DE-23）。

口径（与执行指南 §10.4 一致）：健康不是"进程存在"。每个维度独立读取、
独立给恢复动作；**读取失败显示 unknown，不是健康空态**——探针绝不因为
"查不出来"就当维度不存在。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import current_logical_date, utcnow
from app.core.config import get_settings
from app.models import (
    DailyReport,
    InboxEvent,
    Outbox,
    OutboxStatus,
    Source,
    SourceSync,
    SystemState,
)

# 维度告警阈值（模块级常量：config.py 不在本任务文件集，阈值显式可评审）
EVENT_LATENCY_ALERT_SECONDS = 600
PROJECTION_PENDING_ALERT_SECONDS = 3600

# 每个问题的具体恢复动作：面板/CLI 直接展示，不给"看一下日志"这种空话
RECOVERY_ACTIONS: dict[str, str] = {
    "worker_heartbeat_missing": "启动服务：ops/start.ps1（Windows）或 ops/start.sh（Linux），主管会先做迁移与单活检查",
    "worker_heartbeat_stale": "确认服务进程是否存活；未存活用主管重启，存活则排查 outbox 是否卡在 sending",
    "daily_heartbeat_missing": "确认调度器已启动；首次部署等日报时间窗过后复核",
    "daily_heartbeat_stale": "手动补跑一次日报（python main.py daily），并检查调度器是否被停用",
    "outbox_backlog": "检查发送通道与限流；积压超过告警阈值先看 oldestBacklog 的入队原因",
    "inbox_replay_backlog": "重放窗口内未处理事件超限：检查事件处理线程是否异常，必要时按回放窗口重放",
    "event_latency_high": "事件延迟超阈值：确认事件订阅/轮询是否中断，恢复后观察 oldestPendingAgeSeconds 回落",
    "sync_watermark_missing": "启用的来源从未同步成功：检查该会话的可见性与凭据，重新触发一次轮询",
    "outbox_receipt_unknown": "存在回执未知的发送：用网关 message_id 逐条对账（outbox reconcile），禁止盲重发",
    "run_lease_expired": "Run 租约过期进入 waiting_recovery：先对账副作用（resume reconcile），再决定续跑或失败",
    "daily_report_missed": "昨天的日报未生成：检查调度器心跳与日报开关，必要时手动补跑 python main.py daily",
    "budget_unbounded": "已有模型消耗但未配置预算限额：为 org/space 显式配置预算，避免无口径付费",
    "knowledge_projection_pending": "知识投影重建队列有积压：检查投影根目录可写，重建完成后队列文件会被清除",
}


def heartbeat(job: str, payload: dict[str, Any] | None = None) -> None:
    settings = get_settings()
    now = utcnow()
    with __import__("app.core.database", fromlist=["session_scope"]).session_scope() as session:
        key = f"heartbeat:{job}"
        row = session.get(SystemState, key)
        value = json.dumps(
            {"at": now.isoformat(), "payload": payload or {}}, ensure_ascii=False
        )
        if row is None:
            session.add(SystemState(key=key, value=value))
        else:
            row.value = value
    path = Path(settings.heartbeat_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"job": job, "at": now.isoformat(), "payload": payload or {}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _heartbeat_age(session: Session, job: str, now: datetime) -> float | None:
    row = session.get(SystemState, f"heartbeat:{job}")
    if row is None:
        return None
    try:
        value = json.loads(row.value)
        at = datetime.fromisoformat(value["at"])
    except (ValueError, KeyError, TypeError):
        return None
    return max(0.0, (now - at).total_seconds())


@dataclass
class HealthReport:
    ok: bool
    issues: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    # issue → 具体恢复动作（DE-23）；面板与 CLI 直接展示
    actions: dict[str, str] = field(default_factory=dict)

    def action_for(self, issue: str) -> str:
        if issue in self.actions:
            return self.actions[issue]
        base = issue.split(":", 1)[0]
        return RECOVERY_ACTIONS.get(base, "")


def _dimension(
    details: dict[str, Any], issues: list[str], name: str, fn: Callable[[], Any]
) -> None:
    """读取失败必须可见：维度标 unknown 并产生 issue，绝不静默当空态。"""
    try:
        details[name] = fn()
    except Exception as exc:  # noqa: BLE001 — 任何读取失败都要暴露
        details[name] = {"status": "unknown", "error": f"{type(exc).__name__}: {exc}"[:200]}
        issues.append(f"dimension_read_failed:{name}")


# ---------------------------------------------------------------------------
# 维度实现（每个返回 detail dict；告警判定在 probe 里统一做）
# ---------------------------------------------------------------------------

def _event_latency(session: Session, now: datetime) -> dict:
    oldest = session.scalar(
        select(func.min(InboxEvent.received_at)).where(InboxEvent.processed_at.is_(None))
    )
    age = (now - oldest).total_seconds() if oldest else 0.0
    return {
        "status": "ok" if age <= EVENT_LATENCY_ALERT_SECONDS else "degraded",
        "oldestPendingAgeSeconds": round(age, 1),
    }


def _sync_watermark(session: Session, now: datetime) -> dict:
    rows = [s for s in session.scalars(select(Source)) if s.revoked_at is None]
    missing = [s.chat_id for s in rows if s.enabled and s.last_success_at is None]
    ages = [
        (now - s.last_success_at).total_seconds()
        for s in rows
        if s.enabled and s.last_success_at is not None
    ]
    return {
        "status": "degraded" if missing else "ok",
        "oldestSuccessAgeSeconds": round(max(ages), 1) if ages else None,
        "missing": missing,
    }


def _receipt_unknown(session: Session, now: datetime) -> dict:
    oldest = session.scalar(
        select(func.min(Outbox.updated_at)).where(
            Outbox.status == OutboxStatus.DELIVERY_UNKNOWN
        )
    )
    count = int(
        session.scalar(
            select(func.count(Outbox.id)).where(Outbox.status == OutboxStatus.DELIVERY_UNKNOWN)
        )
        or 0
    )
    return {
        "status": "degraded" if count else "ok",
        "count": count,
        "oldestUnknownAgeSeconds": round((now - oldest).total_seconds(), 1) if oldest else None,
    }


def _run_leases(session: Session, now: datetime) -> dict:
    from evoharness.work.models import ExecutionRun

    expired = list(
        session.scalars(
            select(ExecutionRun).where(
                ExecutionRun.state.in_(["claimed", "running"]),
                ExecutionRun.lease_until.is_not(None),
                ExecutionRun.lease_until <= now,
            )
        )
    )
    waiting = int(
        session.scalar(
            select(func.count(ExecutionRun.id)).where(
                ExecutionRun.state == "waiting_recovery"
            )
        )
        or 0
    )
    return {
        "status": "degraded" if expired or waiting else "ok",
        "expiredLeaseCount": len(expired),
        "expiredRunIds": [run.id for run in expired[:5]],
        "waitingRecoveryCount": waiting,
    }


def _daily_report(session: Session, now: datetime, *, enabled: bool) -> dict:
    if not enabled:
        return {"status": "idle", "enabled": False}
    timezone = get_settings().daily_report_timezone
    latest = session.scalar(select(func.max(DailyReport.logical_date)))
    if latest is None:
        return {"status": "idle", "enabled": True, "latest": None}
    expected = current_logical_date(timezone) - timedelta(days=1)
    missed = date.fromisoformat(str(latest)) < expected
    return {
        "status": "degraded" if missed else "ok",
        "enabled": True,
        "latest": str(latest),
        "expected": expected.isoformat(),
    }


def _budget(session: Session) -> dict:
    from evoharness.governance.models import BudgetLimit, BudgetReservation

    limits = int(session.scalar(select(func.count(BudgetLimit.id))) or 0)
    reserved = int(
        session.scalar(
            select(func.coalesce(func.sum(BudgetReservation.reserved_tokens), 0)).where(
                BudgetReservation.status == "reserved"
            )
        )
        or 0
    )
    used = int(
        session.scalar(
            select(func.coalesce(func.sum(BudgetReservation.used_tokens), 0)).where(
                BudgetReservation.status == "settled"
            )
        )
        or 0
    )
    unbounded = limits == 0 and (reserved or used)
    return {
        "status": "degraded" if unbounded else "ok",
        "limitsConfigured": limits > 0,
        "reservedOpen": reserved,
        "usedTotal": used,
    }


def _projection_roots(settings) -> list[Path]:
    roots: list[Path] = []
    explicit = (os.environ.get("EVOHARNESS_SKILLS_ROOT") or "").strip()
    if explicit:
        roots.append(Path(explicit).resolve().parent / "knowledge-projections")
    base = (
        Path(settings.test_db_prefix).resolve()
        if settings.test_mode and settings.test_db_prefix
        else (Path(settings.evoharness_root) if settings.evoharness_root else Path.cwd())
    )
    roots.append(Path(base) / "knowledge-projections")
    return roots


def _knowledge_projection(now: datetime) -> dict:
    settings = get_settings()
    pending: list[Path] = []
    for base in _projection_roots(settings):
        if not base.is_dir():
            continue
        for rebuild_dir in base.glob("*/.projection-rebuild"):
            pending.extend(rebuild_dir.glob("*.json"))
    oldest_age: float | None = None
    for path in pending:
        try:
            age = max(0.0, now.timestamp() - path.stat().st_mtime)
        except OSError:
            continue
        oldest_age = age if oldest_age is None else max(oldest_age, age)
    return {
        "status": "degraded" if pending else "ok",
        "pendingQueues": len(pending),
        "oldestPendingAgeSeconds": round(oldest_age, 1) if oldest_age is not None else None,
    }


# ---------------------------------------------------------------------------
# 汇总探针
# ---------------------------------------------------------------------------

def probe(session: Session, *, now: datetime | None = None) -> HealthReport:
    settings = get_settings()
    now = now or utcnow()
    issues: list[str] = []
    details: dict[str, Any] = {}
    worker_age = _heartbeat_age(session, "worker", now)
    daily_age = _heartbeat_age(session, "daily", now)
    details["workerHeartbeatAge"] = worker_age
    details["dailyHeartbeatAge"] = daily_age
    if worker_age is None:
        issues.append("worker_heartbeat_missing")
    elif worker_age > settings.heartbeat_stale_seconds:
        issues.append("worker_heartbeat_stale")
    if daily_age is None:
        issues.append("daily_heartbeat_missing")
    elif daily_age > 26 * 3600:
        issues.append("daily_heartbeat_stale")

    backlog = session.scalar(
        select(func.min(Outbox.created_at)).where(
            Outbox.status.in_(
                [OutboxStatus.QUEUED, OutboxStatus.RETRY_WAIT, OutboxStatus.DELIVERY_UNKNOWN]
            )
        )
    )
    details["oldestBacklog"] = backlog.isoformat() if backlog else None
    details["backlogTailAgeSeconds"] = (
        round((now - backlog).total_seconds(), 1) if backlog else None
    )
    if backlog is not None and (now - backlog).total_seconds() > settings.outbox_backlog_alert_seconds:
        issues.append("outbox_backlog")

    unprocessed = session.scalar(
        select(func.count(InboxEvent.id)).where(
            InboxEvent.processed_at.is_(None),
            InboxEvent.received_at >= now - timedelta(seconds=settings.inbox_replay_window_seconds),
        )
    )
    inbox_unprocessed = int(unprocessed or 0)
    details["inboxUnprocessedRecent"] = inbox_unprocessed
    if inbox_unprocessed > max(0, settings.inbox_backlog_alert):
        issues.append("inbox_replay_backlog")

    for source in session.scalars(select(Source)):
        if source.revoked_at is not None:
            continue
        if source.last_success_at is None:
            continue
        age = (now - source.last_success_at).total_seconds()
        if age > 26 * 3600:
            issues.append(f"source_stale:{source.chat_id}")

    failed_syncs = session.scalar(
        select(func.count(SourceSync.id)).where(
            SourceSync.status == "partial",
            SourceSync.updated_at >= now - timedelta(hours=26),
        )
    )
    details["partialSyncs24h"] = int(failed_syncs or 0)
    if failed_syncs:
        issues.append("recent_partial_sync")

    # ---- DE-23 维度：读取失败显示 unknown，不是健康空态 ----
    _dimension(details, issues, "eventLatency", lambda: _event_latency(session, now))
    if details["eventLatency"].get("oldestPendingAgeSeconds", 0) > EVENT_LATENCY_ALERT_SECONDS:
        issues.append("event_latency_high")

    _dimension(details, issues, "syncWatermark", lambda: _sync_watermark(session, now))
    if details["syncWatermark"].get("missing"):
        issues.append("sync_watermark_missing")

    _dimension(details, issues, "outboxReceiptUnknown", lambda: _receipt_unknown(session, now))
    if details["outboxReceiptUnknown"].get("count", 0) > 0:
        issues.append("outbox_receipt_unknown")

    _dimension(details, issues, "runLeases", lambda: _run_leases(session, now))
    if (
        details["runLeases"].get("expiredLeaseCount", 0) > 0
        or details["runLeases"].get("waitingRecoveryCount", 0) > 0
    ):
        issues.append("run_lease_expired")

    _dimension(
        details, issues, "dailyReport",
        lambda: _daily_report(session, now, enabled=settings.daily_report_enabled),
    )
    if details["dailyReport"].get("status") == "degraded":
        issues.append("daily_report_missed")

    _dimension(details, issues, "budget", lambda: _budget(session))
    if details["budget"].get("status") == "degraded":
        issues.append("budget_unbounded")

    _dimension(details, issues, "knowledgeProjection", lambda: _knowledge_projection(now))
    projection = details["knowledgeProjection"]
    if (
        projection.get("pendingQueues", 0) > 0
        and (projection.get("oldestPendingAgeSeconds") or 0) > PROJECTION_PENDING_ALERT_SECONDS
    ):
        issues.append("knowledge_projection_pending")

    actions = {issue: RECOVERY_ACTIONS.get(issue.split(":", 1)[0], "")
               for issue in issues}
    actions = {key: value for key, value in actions.items() if value}
    return HealthReport(not issues, issues, details, actions)
