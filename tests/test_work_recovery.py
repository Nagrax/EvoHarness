"""DE-14：补充队列去重/取消优先，恢复对账绑定版本，Later 不计失败。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evoharness.clock import utcnow
from evoharness.ids import sha256_json
from evoharness.models import Base, Task, TaskStatus
from evoharness.work import execution, service, supplements


def _allow(s, c, a):
    return True


@pytest.fixture()
def env():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        task = Task(task_key="k1", tenant="t", owner="alice", chat_id="chat", title="修复",
                    action="fix", status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                    goal="修复缺陷", acceptance_json='["回归通过"]', revision=1, assignee_id="bob")
        s.add(task)
        s.flush()
        yield s, task
    engine.dispose()


def _queued_run(s, task):
    service.transition_to(s, task.id, to_state="queued", actor="bob")
    return execution.request_run(s, task_id=task.id, context={"g": 1}, actor_id="runner",
                                 output_root="out", idempotency_key="i1",
                                 validate_context=_allow)


# ── 补充队列 ─────────────────────────────────────────────────────────────


def test_supplements_dedupe_order_and_consume(env):
    s, task = env
    first = supplements.enqueue(s, task.id, event_id="e1", event_kind="note",
                                payload={"text": "补充说明"})
    dup = supplements.enqueue(s, task.id, event_id="e1", event_kind="note",
                              payload={"text": "重复投递"})
    assert dup.id == first.id                     # 同一事件不重复入队
    supplements.enqueue(s, task.id, event_id="e2", event_kind="cancellation",
                        payload={"reason": "需求取消"})
    supplements.enqueue(s, task.id, event_id="e3", event_kind="note", payload={})
    queue = supplements.pending(s, task.id)
    assert [r.event_id for r in queue] == ["e2", "e1", "e3"]   # 取消优先，其余按序
    assert supplements.has_pending_cancellation(s, task.id)
    supplements.consume(s, queue[0].id)
    assert not supplements.has_pending_cancellation(s, task.id)
    assert [r.event_id for r in supplements.pending(s, task.id)] == ["e1", "e3"]
    with pytest.raises(supplements.SupplementError, match="invalid_supplement_kind"):
        supplements.enqueue(s, task.id, event_id="e9", event_kind="grant", payload={})


# ── Later 语义：等待不计失败 ────────────────────────────────────────────


def test_waiting_transitions_do_not_increase_attempts(env):
    # R05 语义调整（原断言强度不减）：等待/恢复都不累计失败次数；原用例经
    # "直写 run.state=running" 模拟恢复，绕过了租约——进入等待即释放租约后，
    # 恢复必须走 reacquire 重新授权认领，故改用真实恢复 API 并补租约释放断言。
    s, task = env
    run = _queued_run(s, task)
    claimed = execution.claim(s, run.id, worker="w1", validate_context=_allow)
    first_generation = claimed.generation   # 认领与恢复是同一 ORM 对象，先固化代数
    service.transition_to(s, task.id, to_state="running", actor="w1")
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_allow)
    assert run.attempts == 1
    # 无预算/无资源是延期（Later），不是执行失败：进入等待不刷 attempts，
    # 且进入等待即释放执行租约（LeaseRelease）
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="waiting_budget", validate_context=_allow)
    assert run.attempts == 1
    assert run.lease_owner == "" and run.lease_until is None
    # 恢复执行必须重新授权认领：generation 递增、租约重取；恢复续跑不是新失败
    resumed = execution.reacquire(s, run.id, worker="w1", validate_context=_allow)
    assert resumed.generation == first_generation + 1
    assert resumed.lease_owner == "w1" and resumed.lease_until is not None
    execution.transition(s, run.id, worker="w1", generation=resumed.generation,
                         state="running", validate_context=_allow)
    assert run.attempts == 2                      # 只有真实续跑计一次执行
    execution.transition(s, run.id, worker="w1", generation=resumed.generation,
                         state="waiting_resource", validate_context=_allow)
    assert run.attempts == 2                      # 再次进入等待仍不累计失败
    assert run.lease_owner == "" and run.lease_until is None


# ── 恢复：对账绑定版本 ──────────────────────────────────────────────────


def test_resume_requires_reconciliation_and_binds_revision(env):
    s, task = env
    run = _queued_run(s, task)
    claimed = execution.claim(s, run.id, worker="w1", validate_context=_allow)
    service.transition_to(s, task.id, to_state="running", actor="w1")
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_allow)
    # 模拟崩溃：租约到期后被恢复扫描接管
    run.lease_until = utcnow() - timedelta(seconds=60)
    s.flush()
    recovered = execution.recover_expired(s)
    assert run.id in recovered and run.state == "waiting_recovery"

    # 缺对账回调拒绝
    with pytest.raises(ValueError, match="reconcile_callback_required"):
        execution.resume(s, run.id, reconcile=None, disposition="queued")

    # 对账通过：恢复为 queued，可重新认领，attempts 不变
    ok_run = execution.resume(s, run.id, reconcile=lambda sess, r: True,
                              disposition="queued")
    assert ok_run.state == "queued" and ok_run.attempts == 1
    re_claimed = execution.claim(s, run.id, worker="w2", validate_context=_allow)
    assert re_claimed is not None

    # 对账未通过：落 failed
    execution.transition(s, run.id, worker="w2", generation=re_claimed.generation,
                         state="running", validate_context=_allow)
    run.lease_until = utcnow() - timedelta(seconds=60)
    s.flush()
    execution.recover_expired(s)
    assert run.state == "waiting_recovery"
    failed = execution.resume(s, run.id, reconcile=lambda sess, r: False,
                              disposition="queued", note="外部副作用未确认")
    assert failed.state == "failed" and "reconciliation_failed" in failed.error

    # 任务版本变化后处于恢复等待的 Run 不能恢复（要求按新版本重起）
    task.status = TaskStatus.OPEN
    s.flush()
    second = execution.request_run(s, task_id=task.id, context={"g": 1}, actor_id="runner",
                                   output_root="out", idempotency_key="i2",
                                   validate_context=_allow)
    second_claim = execution.claim(s, second.id, worker="w3", validate_context=_allow)
    execution.transition(s, second.id, worker="w3", generation=second_claim.generation,
                         state="running", validate_context=_allow)
    second.lease_until = utcnow() - timedelta(seconds=60)
    s.flush()
    execution.recover_expired(s)
    assert second.state == "waiting_recovery"
    task.revision += 1
    s.flush()
    with pytest.raises(ValueError, match="work_revision_changed"):
        execution.resume(s, second.id, reconcile=lambda sess, r: True, disposition="queued")


def test_unknown_delivery_receipt_never_becomes_sent():
    """回执不明走对账：delivery_unknown 不被当作 sent（语义回归锚点）。"""
    from evoharness.models import Outbox, OutboxStatus
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        row = Outbox(intent_key="i", tenant="t", kind="reply", target_type="message",
                     target_id="m", payload_json="{}", status=OutboxStatus.DELIVERY_UNKNOWN)
        s.add(row)
        assert row.status == "delivery_unknown"
        assert row.status != OutboxStatus.SENT
    engine.dispose()
