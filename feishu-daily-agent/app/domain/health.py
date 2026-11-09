"""独立健康探针：心跳、积压、水位与漏跑。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import current_logical_date, utcnow
from app.core.config import get_settings
from app.models import Outbox, OutboxStatus, Source, SourceSync, SystemState


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
    if backlog is not None and (now - backlog).total_seconds() > settings.outbox_backlog_alert_seconds:
        issues.append("outbox_backlog")

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
    return HealthReport(not issues, issues, details)
