"""DE-23 健康拆分探针：漏跑/失鲜/投影失败/租约过期/回执未知可发现且带恢复动作。

口径：读取失败显示 unknown，不是健康空态（health.probe 的硬约束）。
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from app.core.database import engine, session_scope
from app.domain import health
from app.domain import outbox as outbox_module
from app.models import DailyReport, OutboxStatus

# 健康探针覆盖治理/组织/知识/Run 维度：注册并补齐产品核心表
# （conftest 的 create_all 只含已导入模块的表；真实部署由迁移保证）。
import evoharness.governance.models  # noqa: F401
import evoharness.knowledge.models  # noqa: F401
import evoharness.organization.models  # noqa: F401
from evoharness.models import Base


@pytest.fixture(autouse=True)
def _ensure_all_product_tables():
    Base.metadata.create_all(engine)
    yield


def _fresh_heartbeats():
    health.heartbeat("worker", {"status": "ok"})
    health.heartbeat("daily", {"status": "ok"})


def _probe():
    with session_scope() as session:
        return health.probe(session, now=utcnow())


def test_empty_db_with_fresh_heartbeats_stays_ok(settings, monkeypatch):
    """基线：心跳新鲜、库为空时不得被新维度误伤成不健康。"""
    monkeypatch.setattr(settings, "daily_report_enabled", False)
    _fresh_heartbeats()
    report = _probe()
    assert report.ok, report.issues
    # 每个维度都有读数，且没有 unknown
    for name in ("eventLatency", "syncWatermark", "outboxReceiptUnknown", "runLeases",
                 "dailyReport", "budget", "knowledgeProjection"):
        assert name in report.details, name
        assert report.details[name].get("status") != "unknown"


def test_receipt_unknown_detected_with_recovery_action():
    _fresh_heartbeats()
    with session_scope() as session:
        outbox_module.enqueue(session, intent="de23|unknown", tenant="default",
                              kind="notice", target_type="open_id",
                              target_id="ou_owner", payload={"text": "hi"})
        row = session.query(outbox_module.Outbox).filter_by(
            intent_key="de23|unknown").one()
        row.status = OutboxStatus.DELIVERY_UNKNOWN
        row.updated_at = utcnow() - timedelta(hours=2)
        session.flush()
    report = _probe()
    assert not report.ok
    assert "outbox_receipt_unknown" in report.issues
    assert report.details["outboxReceiptUnknown"]["count"] == 1
    action = report.action_for("outbox_receipt_unknown")
    assert "对账" in action
    assert "禁止盲重发" in action


def test_run_lease_expiry_detected_with_recovery_action():
    from evoharness.work.models import ExecutionRun

    _fresh_heartbeats()
    now = utcnow()
    with session_scope() as session:
        session.add(ExecutionRun(id="run-expired-1", task_id=1, tenant="default",
                                 task_revision=1, idempotency_key="de23-expired",
                                 actor_id="worker", context_json="{}",
                                 context_hash="0" * 64, output_root="out",
                                 state="claimed", lease_owner="dead-worker",
                                 lease_until=now - timedelta(minutes=5)))
        session.flush()
    report = _probe()
    assert "run_lease_expired" in report.issues
    assert report.details["runLeases"]["expiredLeaseCount"] == 1
    assert "run-expired-1" in report.details["runLeases"]["expiredRunIds"]
    action = report.action_for("run_lease_expired")
    assert "reconcile" in action or "对账" in action


def test_daily_report_missed_detected(settings, monkeypatch):
    _fresh_heartbeats()
    monkeypatch.setattr(settings, "daily_report_enabled", True)
    with session_scope() as session:
        session.add(DailyReport(
            report_key="default|ou_owner|2026-11-01|Asia/Shanghai",
            tenant="default", owner="ou_owner", logical_date="2026-11-01",
            timezone="Asia/Shanghai", window_start_ms=0, window_end_ms=1,
            status="sent"))
        session.flush()
    report = _probe()
    assert "daily_report_missed" in report.issues
    assert report.details["dailyReport"]["status"] == "degraded"
    assert report.details["dailyReport"]["latest"] == "2026-11-01"
    assert report.action_for("daily_report_missed")


def test_event_latency_and_sync_watermark_detected(settings):
    from app.domain import intake
    from app.models import InboxEvent, Source

    _fresh_heartbeats()
    now = utcnow()
    with session_scope() as session:
        assert intake.record_event(
            session, tenant="default", event_id="evt-de23-latency",
            event_type="im.message.receive_v1", message_id="om_de23", payload="x",
        )
        stale_event = session.query(InboxEvent).filter_by(
            event_id="evt-de23-latency").one()
        stale_event.received_at = now - timedelta(hours=2)
        session.add(Source(tenant="default", owner="ou_owner", chat_id="oc_never_synced",
                           source_type="p2p", enabled=True, last_success_at=None))
        session.flush()
    report = _probe()
    assert "event_latency_high" in report.issues
    assert report.details["eventLatency"]["status"] == "degraded"
    assert report.details["eventLatency"]["oldestPendingAgeSeconds"] > 3600
    assert "sync_watermark_missing" in report.issues
    assert "oc_never_synced" in report.details["syncWatermark"]["missing"]


def test_budget_unbounded_detected():
    from evoharness.governance.models import BudgetReservation

    _fresh_heartbeats()
    with session_scope() as session:
        session.add(BudgetReservation(reservation_id="res-de23", org_id="org-1",
                                      category="chat", reserved_tokens=100,
                                      used_tokens=80, status="settled"))
        session.flush()
    report = _probe()
    assert "budget_unbounded" in report.issues
    assert report.details["budget"]["limitsConfigured"] is False
    assert report.details["budget"]["usedTotal"] == 80
    assert report.action_for("budget_unbounded")


def test_projection_pending_detected(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills-root"
    rebuild_dir = skills_root.parent / "knowledge-projections" / "scope-key" / ".projection-rebuild"
    rebuild_dir.mkdir(parents=True)
    pending = rebuild_dir / "queue.json"
    pending.write_text('{"state": "pending"}', encoding="utf-8")
    two_hours_ago = (utcnow() - timedelta(hours=2)).timestamp()
    os.utime(pending, (two_hours_ago, two_hours_ago))
    monkeypatch.setenv("EVOHARNESS_SKILLS_ROOT", str(skills_root))

    _fresh_heartbeats()
    report = _probe()
    assert "knowledge_projection_pending" in report.issues
    detail = report.details["knowledgeProjection"]
    assert detail["pendingQueues"] >= 1
    assert detail["oldestPendingAgeSeconds"] > 3600
    assert report.action_for("knowledge_projection_pending")


def test_read_failure_is_unknown_not_empty(monkeypatch):
    """维度读取失败必须可见：status=unknown + issue，绝不假装空态/正常。"""
    _fresh_heartbeats()

    def boom(session, now):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(health, "_run_leases", boom)
    report = _probe()
    assert not report.ok
    assert "dimension_read_failed:runLeases" in report.issues
    assert report.details["runLeases"]["status"] == "unknown"
    assert "db exploded" in report.details["runLeases"]["error"]


def test_existing_issues_carry_recovery_actions():
    report = _probe()
    assert "worker_heartbeat_missing" in report.issues
    assert "启动" in report.action_for("worker_heartbeat_missing")
    assert "ops/start" in report.action_for("worker_heartbeat_missing")
