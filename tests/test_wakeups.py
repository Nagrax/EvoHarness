"""DE-21 回归：事件唤醒、去重催办与责任路由（有界规则引擎）。

覆盖验收场景：
1. 空态/执行中不误催；真实依赖完成后只启动一次 Run；自己的消息不无限唤醒。
2. task+reason+revision 去重与冷却窗；状态变化才重新通知；空检查只留本地记录。
3. 无授权对象不运行/不提醒；离开组织后旧定时委派被拒绝并给出明确语义。
4. 来源读取失败 → unknown/stale 标记 + 告警，不显示为空闲；积压尾部年龄上报。
5. 重复触发（同 task+reason+revision 多次）不产生重复发信意图。
6. 协调提案：带在途变更的下一步建议可生成并经核验执行；无授权提案被拒绝。

全部离线：内存 SQLite + 注入时钟 + fake 授权/路由端口。
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "integration"))

from evoharness.clock import utcnow
from evoharness.models import Base, Outbox, Task, TaskEvent
from evoharness.work import wakeups
from evoharness.work.models import ExecutionRun
from evoharness.work.service import transition_to

import evoharness.organization.models  # noqa: F401 — 注册组织表（membership guard 用）


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


def _delegated(session, **overrides) -> Task:
    values = dict(task_key="k1", tenant="t", owner="alice", chat_id="chat",
                  thread_id="", title="fix bug", action="fix",
                  status="waiting_dependency", workflow_kind="delegated_delivery",
                  goal="fix the bug", acceptance_json='["回归通过"]',
                  dependencies_json='["k0"]')
    values.update(overrides)
    task = Task(**values)
    session.add(task)
    session.flush()
    return task


def _allow(session_value, context, action):
    return True


def _deny(session_value, context, action):
    return False


def _event(kind: str, task_key: str = "k1", **overrides) -> wakeups.WakeEvent:
    values = dict(kind=kind, task_key=task_key, tenant="t")
    values.update(overrides)
    return wakeups.WakeEvent(**values)


def _run_row(session, task: Task, state: str = "claimed") -> ExecutionRun:
    run = ExecutionRun(id=uuid.uuid4().hex, task_id=task.id, tenant=task.tenant,
                       task_revision=task.revision, idempotency_key=f"seed-{uuid.uuid4().hex[:8]}",
                       actor_id="w1", context_json="{}", context_hash="seed",
                       output_root="out", state=state,
                       lease_until=utcnow() + timedelta(seconds=120))
    session.add(run)
    session.flush()
    return run


def _outbox_count(session) -> int:
    return int(session.scalar(select(func.count()).select_from(Outbox)))


def _run_count(session) -> int:
    return int(session.scalar(select(func.count()).select_from(ExecutionRun)))


def _event_types(session, task_id: int) -> list[str]:
    return [e.event_type for e in session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task_id))]


# ── 场景 1：空态/执行中不误催；依赖完成只启动一次 Run；自己的消息不唤醒 ───────

def test_idle_round_leaves_no_trace(session):
    """空态：无事件 → 一次空检查，只留本地统计，无发信意图、无事件记录。"""
    _delegated(session)
    report = wakeups.evaluate(session, events=[], now=utcnow())
    assert report["idleChecks"] == 1
    assert report["notifications"] == 0
    assert _outbox_count(session) == 0
    assert _run_count(session) == 0
    assert session.scalars(select(TaskEvent)).all() == []


def test_unknown_wake_kind_is_not_a_rule(session):
    """有界规则集：未知事件类型被拒绝，不触发任何动作。"""
    _delegated(session, status="open")
    report = wakeups.evaluate(session, events=[_event("hallucinated_event")], now=utcnow())
    assert report["skipped"] == [{"kind": "hallucinated_event", "taskKey": "k1",
                                  "reason": "unknown_wake_kind"}]
    assert _outbox_count(session) == 0


def test_running_task_is_not_nudged_again(session):
    """执行中（状态 running 或 Run 在途）不催同一动作：只留本地合并记录。"""
    task = _delegated(session, status="running")
    _run_row(session, task, state="running")
    report = wakeups.evaluate(session, events=[_event("new_reply")], now=utcnow())
    assert report["coalesced"] == [{"taskKey": "k1", "kind": "new_reply",
                                    "reason": "run_in_progress_no_duplicate_nudge"}]
    assert _outbox_count(session) == 0
    assert "wakeup_coalesced" in _event_types(session, task.id)


def test_active_run_blocks_nudge_even_when_status_open(session):
    task = _delegated(session, status="open")
    _run_row(session, task, state="claimed")
    report = wakeups.evaluate(session, events=[_event("new_reply")], now=utcnow())
    assert report["coalesced"] and not report["wakeups"]
    assert _outbox_count(session) == 0


def test_dependency_resolved_starts_run_exactly_once(session):
    """真实依赖完成后：状态经唯一入口放行 + 幂等 Run；重放事件不重复起 Run。"""
    dep = _delegated(session, task_key="k0", status="done", dependencies_json="[]")
    task = _delegated(session, status="waiting_dependency")
    assert dep.status == "done"
    first = wakeups.evaluate(session, events=[_event("dependency_resolved", message_id="m-dep")],
                             now=utcnow(), validate_context=_allow)
    assert first["notifications"] == 1
    run_info = first["wakeups"][0]["run"]
    assert run_info["alreadyExisted"] is False
    assert task.status == "queued"
    runs = list(session.scalars(select(ExecutionRun)))
    assert len(runs) == 1 and runs[0].id == run_info["runId"]
    # 重放同一依赖恢复事件：Run 已在途（queued 等待认领）→ 合并不催，Run 不重复。
    second = wakeups.evaluate(session, events=[_event("dependency_resolved", message_id="m-dep-2")],
                              now=utcnow() + timedelta(hours=2), validate_context=_allow)
    assert second["wakeups"] == []
    assert second["coalesced"][0]["reason"] == "run_in_progress_no_duplicate_nudge"
    assert _outbox_count(session) == 1
    assert _run_count(session) == 1


def test_self_message_does_not_wake(session):
    """自己的消息（责任人本人发出）不无限唤醒。"""
    task = _delegated(session, status="open")
    for _ in range(3):
        report = wakeups.evaluate(session, events=[_event("new_reply", actor="alice")],
                                  now=utcnow())
        assert report["skipped"][0]["reason"] == "self_message"
    assert _outbox_count(session) == 0
    assert _run_count(session) == 0
    assert "wakeup_notified" not in _event_types(session, task.id)


def test_known_evidence_message_does_not_wake(session):
    task = _delegated(session, status="open",
                      source_message_ids=json.dumps(["m-already"]))
    report = wakeups.evaluate(session, events=[_event("new_reply", message_id="m-already")],
                              now=utcnow())
    assert report["skipped"][0]["reason"] == "known_evidence"
    assert _outbox_count(session) == 0


# ── 场景 2：去重冷却；状态变化才重新通知；空检查只留本地记录 ─────────────────

def test_cooldown_merges_repeated_wakeups(session):
    """冷却窗内重复评估：合并为本地记录，不追加发信意图。"""
    task = _delegated(session, status="open")
    t0 = utcnow() - timedelta(hours=2)
    first = wakeups.evaluate(session, events=[_event("new_reply")], now=t0)
    assert first["notifications"] == 1
    second = wakeups.evaluate(session, events=[_event("new_reply")],
                              now=t0 + timedelta(seconds=60))
    assert second["notifications"] == 0
    assert second["coalesced"][0]["reason"] == "cooldown"
    assert _outbox_count(session) == 1
    notified = [e for e in session.scalars(select(TaskEvent).where(
        TaskEvent.task_id == task.id)) if e.event_type == "wakeup_notified"]
    assert len(notified) == 1


def test_state_change_bypasses_cooldown_and_renotifies_once(session):
    """只有状态变化（有效进展）才重新通知；重放无进展仍被去重。"""
    task = _delegated(session, status="waiting_external")
    t0 = utcnow() - timedelta(hours=6)
    first = wakeups.evaluate(session, events=[_event("ci_finished")], now=t0)
    assert first["notifications"] == 1
    # 有效进展：状态经唯一入口转换 → updated_at 变化 → 绕过冷却重新通知。
    transition_to(session, task.id, to_state="queued", actor="alice", reason="ci ok")
    second = wakeups.evaluate(session, events=[_event("ci_finished")],
                              now=t0 + timedelta(seconds=30))
    assert second["notifications"] == 1
    assert _outbox_count(session) == 2  # notifySeq=0 与 1 两条不同意图
    notified = [e for e in session.scalars(select(TaskEvent).where(
        TaskEvent.task_id == task.id)) if e.event_type == "wakeup_notified"]
    assert len(notified) == 2
    # 之后再重放（无新进展）：冷却窗内合并，不重复发信。
    third = wakeups.evaluate(session, events=[_event("ci_finished")],
                             now=t0 + timedelta(seconds=60))
    assert third["notifications"] == 0
    assert third["coalesced"] and third["coalesced"][0]["reason"] == "cooldown"
    assert _outbox_count(session) == 2


def test_expired_cooldown_without_progress_stays_deduped(session):
    """冷却过期但无进展：不重发（避免噪音），意图幂等命中。"""
    _delegated(session, status="open")
    t0 = utcnow() - timedelta(hours=8)
    assert wakeups.evaluate(session, events=[_event("new_reply")], now=t0)["notifications"] == 1
    later = wakeups.evaluate(session, events=[_event("new_reply")],
                             now=t0 + timedelta(hours=2))
    assert later["notifications"] == 0
    assert later["wakeups"][0]["deduped"] is True
    assert later["wakeups"][0]["reminderCreated"] is False
    assert _outbox_count(session) == 1


def test_revision_change_starts_new_dedup_window(session):
    """revision 变化 = 新的去重窗口（task+reason+revision 维度）。"""
    task = _delegated(session, status="open")
    t0 = utcnow() - timedelta(hours=2)
    assert wakeups.evaluate(session, events=[_event("new_reply")], now=t0)["notifications"] == 1
    task.revision += 1  # 合同修订：旧窗口失效
    session.flush()
    second = wakeups.evaluate(session, events=[_event("new_reply")],
                              now=t0 + timedelta(seconds=10))
    assert second["notifications"] == 1
    assert _outbox_count(session) == 2


def test_deadline_events_scan_active_due_tasks(session):
    _delegated(session, status="open", due_at=utcnow() + timedelta(hours=10))
    _delegated(session, task_key="k2", status="open", due_at=utcnow() + timedelta(hours=100))
    _delegated(session, task_key="k3", status="done", due_at=utcnow() + timedelta(hours=1))
    events = wakeups.deadline_events(session, now=utcnow())
    assert [e.task_key for e in events] == ["k1"]
    assert all(e.kind == "deadline_near" for e in events)


def test_reminder_carries_owner_reason_due_and_entry(session):
    """提醒必须带负责人、原因、截止时间、处理入口。"""
    task = _delegated(session, status="open", due_text="周五", thread_id="th-1")
    report = wakeups.evaluate(session, events=[_event("new_reply")], now=utcnow())
    payload = json.loads(session.scalar(select(Outbox)).payload_json)
    assert payload["owner"] == "alice"
    assert payload["reason"] == "new_reply"
    assert payload["due"] == "周五"
    assert payload["entry"] == "task:k1 thread:th-1"
    assert "alice" in payload["text"] and wakeups.REASON_TEXT["new_reply"] in payload["text"]
    assert report["wakeups"][0]["recipient"] == "alice"


def test_each_owner_gets_their_own_items(session):
    """各责任人收各自事项：路由按任务责任人，不全部推给部署 owner。"""
    _delegated(session, task_key="ka", owner="alice", status="open")
    _delegated(session, task_key="kb", owner="bob", status="open")
    report = wakeups.evaluate(
        session, events=[_event("new_reply", task_key="ka"), _event("new_reply", task_key="kb")],
        now=utcnow())
    assert {w["recipient"] for w in report["wakeups"]} == {"alice", "bob"}
    intents = {row.intent_key for row in session.scalars(select(Outbox))}
    assert any("|alice|" in i for i in intents)
    assert any("|bob|" in i for i in intents)


# ── 场景 3：无授权对象不运行/不提醒；离开组织后旧定时委派被拒绝 ───────────────

def test_no_responsible_owner_gets_nothing(session):
    """无责任人：不提醒、不运行，也不推给部署 owner。"""
    _delegated(session, status="open", owner="", assignee_id="")
    report = wakeups.evaluate(session, events=[_event("new_reply")], now=utcnow())
    assert report["skipped"][0]["reason"] == "no_responsible_owner"
    assert _outbox_count(session) == 0


def test_left_organization_rejects_scheduled_delegation(session):
    """离开组织：旧定时委派被拒绝且语义明确，提醒也不发给已离开的人。"""
    task = _delegated(session, status="waiting_dependency")
    checked: list[str] = []

    def gone(recipient: str) -> bool:
        checked.append(recipient)
        return False

    report = wakeups.evaluate(session, events=[_event("dependency_resolved")],
                              now=utcnow(), validate_context=_allow, recipient_ok=gone)
    assert checked == ["alice"]
    assert report["skipped"][0]["reason"] == "recipient_inactive"
    assert _run_count(session) == 0
    assert _outbox_count(session) == 0
    assert task.status == "waiting_dependency"  # 未被偷偷放行


def test_unauthorized_action_blocks_run_but_not_reminder(session):
    """动作授权失效：Run 不启动（明确原因），提醒仍路由给责任人请求补授权。"""
    _delegated(session, status="waiting_dependency")
    report = wakeups.evaluate(session, events=[_event("dependency_resolved")],
                              now=utcnow(), validate_context=_deny)
    assert any(r["reason"] == "run_not_started:authorization_invalid"
               for r in report["skipped"])
    assert _run_count(session) == 0
    assert report["notifications"] == 1


def test_missing_authorization_port_never_starts_run(session):
    """无授权端口：宁可不起 Run，也不能未核验就执行。"""
    _delegated(session, status="waiting_dependency")
    report = wakeups.evaluate(session, events=[_event("dependency_resolved")], now=utcnow())
    assert any(r["reason"].startswith("run_not_started:")
               for r in report["skipped"])
    assert _run_count(session) == 0


def test_membership_active_guard(session):
    """组织成员资格 guard：active 且在有效期内才算在组织内。"""
    from evoharness.organization.models import Membership

    session.add(Membership(membership_id="m1", org_id="org1", principal_id="alice",
                           status="active", valid_from=datetime(2026, 1, 1),
                           valid_until=None))
    session.add(Membership(membership_id="m2", org_id="org1", principal_id="carol",
                           status="revoked", valid_from=datetime(2026, 1, 1),
                           valid_until=None))
    session.add(Membership(membership_id="m3", org_id="org1", principal_id="dan",
                           status="active", valid_from=datetime(2026, 1, 1),
                           valid_until=datetime(2026, 3, 1)))  # 已过期
    session.flush()
    now = datetime(2026, 5, 1)
    assert wakeups.membership_active(session, org_id="org1", principal_id="alice", now=now)
    assert not wakeups.membership_active(session, org_id="org1", principal_id="carol", now=now)
    assert not wakeups.membership_active(session, org_id="org1", principal_id="dan", now=now)
    assert not wakeups.membership_active(session, org_id="org1", principal_id="ghost", now=now)


# ── 场景 4：来源健康：读取失败 unknown/stale + 告警；积压尾部年龄可见 ─────────

def test_source_read_failure_is_unknown_not_idle():
    report = wakeups.source_health(reads=[
        {"source": "backlog", "ok": False, "error": "json broken", "tail_age_seconds": None}])
    assert report["healthy"] is False
    assert report["sources"][0]["state"] == "unknown"
    assert report["sources"][0]["error"] == "json broken"
    assert report["alerts"] and report["alerts"][0]["state"] == "unknown"


def test_stale_backlog_tail_age_is_reported():
    report = wakeups.source_health(
        reads=[{"source": "replyDrafts", "ok": True, "error": "",
                "tail_age_seconds": 90 * 3600}],
        stale_after_seconds=48 * 3600)
    assert report["sources"][0]["state"] == "stale"
    assert report["sources"][0]["tailAgeSeconds"] == 324000.0
    assert report["tailAgeSeconds"] == 324000.0
    assert report["healthy"] is False


def test_clean_empty_state_distinct_from_broken():
    clean = wakeups.source_health(reads=[
        {"source": "backlog", "ok": True, "error": "", "tail_age_seconds": None}])
    assert clean["healthy"] is True and clean["alerts"] == []
    assert clean["sources"][0]["state"] == "clean"


def test_duplicate_health_alert_not_resent():
    """同一来源同一状态重复触发：告警意图幂等，不重复发信。"""
    sent: dict[str, dict] = {}

    def alert(*, intent: str, payload: dict) -> bool:
        if intent in sent:
            return False
        sent[intent] = payload
        return True

    read = {"source": "replyDrafts", "ok": False, "error": "db locked",
            "tail_age_seconds": None}
    first = wakeups.source_health(reads=[read], alert=alert)
    second = wakeups.source_health(reads=[read], alert=alert)
    assert len(first["alerts"]) == 1
    assert second["alerts"] == []
    assert list(sent) == ["wakeup|source|replyDrafts|unknown"]


def test_nudge_collect_marks_missing_db_unknown(monkeypatch, tmp_path):
    """integration 接线：agent.db 缺失时来源标 unknown，不与空态混同。"""
    import nudge as nudge_mod

    monkeypatch.setattr(nudge_mod, "FEISHU_AGENT", tmp_path)
    payload = nudge_mod.collect()
    states = {s["source"]: s["state"] for s in payload["sourceHealth"]["sources"]}
    assert states["replyDrafts"] == "unknown"
    assert payload["sourceHealth"]["healthy"] is False
    assert payload["draftsStale"] == []  # 业务空态保持为空，两者不混同


def test_nudge_compose_reports_unhealthy_source_not_idle():
    """来源失联必须亮告警（即使没有业务卡点），不显示为空闲。"""
    import nudge as nudge_mod

    text = nudge_mod.compose({
        "highOpen": [], "stale": [], "draftsStale": [],
        "sourceHealth": {"sources": [{"source": "replyDrafts", "state": "unknown",
                                      "error": "agent.db-missing", "tailAgeSeconds": None}],
                         "healthy": False, "tailAgeSeconds": None, "alerts": []}})
    assert text is not None
    assert "replyDrafts" in text and "unknown" in text


def test_nudge_compose_empty_still_returns_none():
    """既有断言保持：业务空且来源健康 → 不推送（不弱化）。"""
    import nudge as nudge_mod

    assert nudge_mod.compose({}) is None
    assert nudge_mod.compose({"highOpen": [], "stale": [], "draftsStale": []}) is None


def test_backlog_summary_reports_tail_age(tmp_path, monkeypatch):
    """loop_report 接线：积压尾部年龄（最老未闭合项）可见。"""
    from integration import loop_report

    backlog = {"rounds": [], "items": [
        {"id": "L1", "status": "pending", "severity": "low", "title": "a",
         "firstSeenRound": "evo-v14-20261101"},
        {"id": "L2", "status": "done", "severity": "low", "title": "b"},
    ]}
    path = tmp_path / "BACKLOG.json"
    path.write_text(json.dumps(backlog), encoding="utf-8")
    monkeypatch.setattr(loop_report, "BACKLOG", path)
    summary = loop_report._backlog_summary()
    expected = float((date.today() - date(2026, 11, 1)).days)
    assert summary["tailAgeDays"] == expected


# ── 场景 5：重复触发不产生重复发信意图（outbox 幂等） ─────────────────────────

def test_repeated_trigger_produces_single_outbox_intent(session):
    """同 task+reason+revision 多次触发：outbox 意图只建一条。"""
    _delegated(session, status="open")
    t0 = utcnow() - timedelta(hours=6)
    r1 = wakeups.evaluate(session, events=[_event("new_reply")], now=t0)
    r2 = wakeups.evaluate(session, events=[_event("new_reply")], now=t0 + timedelta(minutes=10))
    r3 = wakeups.evaluate(session, events=[_event("new_reply")], now=t0 + timedelta(hours=2))
    assert r1["notifications"] == 1
    assert r2["notifications"] == 0 and r2["coalesced"]
    assert r3["wakeups"][0]["reminderCreated"] is False
    rows = list(session.scalars(select(Outbox)))
    assert len(rows) == 1
    assert rows[0].intent_key.startswith("wakeup|k1|new_reply|")


# ── 场景 6：协调提案：在途变更 → 有效下一步 → 核验执行；无授权拒绝 ────────────

def test_next_steps_from_inflight_changes_execute_run(session):
    """依赖完成 + 在途取消 → 结构化提案经核验执行（不只输出提醒）。"""
    _delegated(session, task_key="k0", status="done", dependencies_json="[]")
    task = _delegated(session, status="waiting_dependency")
    from evoharness.work import supplements

    supplements.enqueue(session, task.id, event_id="evt-1", event_kind="cancellation")
    results = wakeups.propose_next_steps(session, actor="coordinator",
                                         validate_context=_allow)
    by_key = {(r["taskKey"], r["kind"]): r for r in results}
    assert by_key[("k1", "request_run")]["executed"] is True
    assert by_key[("k1", "reconcile_progress")]["executed"] is True
    runs = list(session.scalars(select(ExecutionRun)))
    assert len(runs) == 1 and runs[0].task_id == task.id
    assert "proposal" in _event_types(session, task.id)


def test_clarify_proposal_records_clarification(session):
    task = _delegated(session, status="needs_confirmation", waiting_reason="缺少验收口径")
    result = wakeups.coordinate(session, task.id, actor="coordinator", kind="clarify_goal",
                                based_on_revision=task.revision,
                                payload={"question": "请给出验收口径"},
                                validate_context=_allow)
    assert result["executed"] is True
    assert "clarification_requested" in _event_types(session, task.id)
    assert task.status == "needs_confirmation"  # 非阻塞澄清不改状态


def test_coordinate_rejects_stale_revision(session):
    task = _delegated(session, status="open")
    import pytest

    with pytest.raises(ValueError, match="stale_task_revision"):
        wakeups.coordinate(session, task.id, actor="coordinator", kind="link_work",
                           based_on_revision=task.revision + 1, payload={},
                           validate_context=_allow)


def test_coordinate_replay_is_idempotent(session):
    _delegated(session, task_key="k0", status="done", dependencies_json="[]")
    task = _delegated(session, status="waiting_dependency")
    first = wakeups.coordinate(session, task.id, actor="coordinator", kind="request_run",
                               based_on_revision=task.revision,
                               payload={"proposalId": "p-fixed", "trigger": "dependency_resolved"},
                               validate_context=_allow)
    assert first["executed"] is True and first["runId"]
    second = wakeups.coordinate(session, task.id, actor="coordinator", kind="request_run",
                                based_on_revision=task.revision,
                                payload={"proposalId": "p-fixed", "trigger": "dependency_resolved"},
                                validate_context=_allow)
    assert second["duplicate"] is True and second["executed"] is False
    assert _run_count(session) == 1


def test_proposal_without_membership_is_rejected_before_record(session):
    """无授权主体：提案被拒绝（明确语义），不记录、不执行。"""
    task = _delegated(session, status="open")
    import pytest

    with pytest.raises(ValueError, match="proposal_not_authorized"):
        wakeups.coordinate(session, task.id, actor="intruder", kind="clarify_goal",
                           based_on_revision=task.revision, payload={"question": "?"},
                           recipient_ok=lambda who: False, validate_context=None)
    assert session.scalars(select(TaskEvent)).all() == []


def test_proposal_without_any_authorization_port_is_rejected(session):
    task = _delegated(session, status="open")
    import pytest

    with pytest.raises(ValueError, match="authorization_adapter_required"):
        wakeups.coordinate(session, task.id, actor="coordinator", kind="clarify_goal",
                           based_on_revision=task.revision, payload={"question": "?"})
    assert _run_count(session) == 0


def test_request_run_proposal_requires_work_authorization(session):
    """request_run 提案：Run 服务复核失败 → 提案保留但执行被拒、无 Run。"""
    _delegated(session, task_key="k0", status="done", dependencies_json="[]")
    task = _delegated(session, status="waiting_dependency")
    import pytest

    with pytest.raises(ValueError, match="authorization_invalid"):
        wakeups.coordinate(session, task.id, actor="coordinator", kind="request_run",
                           based_on_revision=task.revision,
                           payload={"proposalId": "p-deny"},
                           validate_context=_deny)
    assert _run_count(session) == 0
    assert "proposal" in _event_types(session, task.id)  # 提案在案，执行未发生


def test_next_steps_without_authorization_are_rejected(session):
    """propose_next_steps 全链路无授权：提案逐一被拒，无 Run、无事件。"""
    _delegated(session, task_key="k0", status="done", dependencies_json="[]")
    _delegated(session, status="waiting_dependency")
    results = wakeups.propose_next_steps(session, actor="coordinator",
                                         recipient_ok=lambda who: False)
    assert results and all(r["rejected"] and r["reason"] == "proposal_not_authorized"
                           for r in results)
    assert _run_count(session) == 0
    assert session.scalars(select(TaskEvent)).all() == []


def test_next_steps_blocker_for_pending_dependency(session):
    """依赖未完成：升级 blocker 而不是起 Run。"""
    _delegated(session, task_key="k0", status="open")
    task = _delegated(session, status="waiting_dependency")
    results = wakeups.propose_next_steps(session, actor="coordinator",
                                         validate_context=_allow)
    kinds = {r["kind"]: r for r in results}
    assert "request_run" not in kinds
    assert kinds["raise_blocker"]["executed"] is True
    proposals = [json.loads(e.evidence_message_ids) for e in session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task.id,
                                TaskEvent.event_type == "proposal"))]
    assert any("dependency_pending:k0" in json.dumps(p) for p in proposals)
    assert _run_count(session) == 0
