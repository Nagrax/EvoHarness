"""F08 前置：独立健康探针。"""
from __future__ import annotations

from datetime import timedelta

from app.core.clock import utcnow
from app.core.database import session_scope
from app.domain import health
from app.domain import outbox as outbox_module
from app.models import OutboxStatus


def test_health_reports_missing_heartbeat():
    with session_scope() as session:
        report = health.probe(session)
        assert not report.ok
        assert "worker_heartbeat_missing" in report.issues
        assert "daily_heartbeat_missing" in report.issues


def test_health_ok_with_heartbeats_and_no_backlog():
    health.heartbeat("worker", {"status": "ok"})
    health.heartbeat("daily", {"status": "ok"})
    with session_scope() as session:
        report = health.probe(session)
        assert report.ok, report.issues


def test_health_detects_backlog():
    health.heartbeat("worker", {"status": "ok"})
    health.heartbeat("daily", {"status": "ok"})
    with session_scope() as session:
        row = outbox_module.enqueue(
            session, intent="health|backlog", tenant="default", kind="notice",
            target_type="open_id", target_id="ou_owner", payload={"text": "hi"},
        )
        row.created_at = utcnow() - timedelta(hours=2)
        session.flush()
    with session_scope() as session:
        report = health.probe(session)
        assert not report.ok
        assert "outbox_backlog" in report.issues
