"""DE-11/14 前置：委派 Run 的租约、代际与恢复语义。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evoharness.clock import utcnow
from evoharness.models import Base, Task, TaskStatus, TaskEvent
from evoharness.work import execution
from evoharness.work.service import apply_completion_signal, set_status
from evoharness.models import Message


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
                  thread_id="", title="fix bug", action="fix", status=TaskStatus.OPEN,
                  workflow_kind="delegated_delivery", goal="fix the bug", acceptance_json='["回归通过"]')
    values.update(overrides)
    task = Task(**values)
    session.add(task)
    session.flush()
    return task


def _allow(session_value, context, action):
    return context.get("grant") == "ok" and action == "work.execute"


def _context():
    return {"grant": "ok"}


def test_request_run_requires_delegated_work_contract(session):
    plain = _delegated(session, workflow_kind="legacy_todo")
    with pytest.raises(ValueError, match="delegated_task_required"):
        execution.request_run(session, task_id=plain.id, context=_context(),
                               actor_id="runner", output_root="out", idempotency_key="a", validate_context=_allow)
    incomplete = _delegated(session, task_key="k2", goal="", acceptance_json="[]")
    with pytest.raises(ValueError, match="incomplete_work_contract"):
        execution.request_run(session, task_id=incomplete.id, context=_context(),
                               actor_id="runner", output_root="out", idempotency_key="b", validate_context=_allow)


def test_request_run_is_idempotent_and_conflict_safe(session):
    task = _delegated(session)
    first = execution.request_run(session, task_id=task.id, context=_context(),
                                   actor_id="runner", output_root="out", idempotency_key="intent-1",
                                   validate_context=_allow)
    again = execution.request_run(session, task_id=task.id, context=_context(),
                                   actor_id="runner", output_root="out", idempotency_key="intent-1",
                                   validate_context=_allow)
    assert again.id == first.id
    with pytest.raises(ValueError, match="run_intent_conflict"):
        execution.request_run(session, task_id=task.id, context={"grant": "ok", "extra": 1},
                              actor_id="runner", output_root="out", idempotency_key="intent-1",
                              validate_context=_allow)


def test_request_run_rejects_tampered_or_unauthorized_context(session):
    task = _delegated(session)
    denied = lambda s, c, a: False
    with pytest.raises(ValueError, match="authorization_invalid"):
        execution.request_run(session, task_id=task.id, context={"grant": "no"},
                              actor_id="runner", output_root="out", idempotency_key="x",
                              validate_context=denied)
    with pytest.raises(ValueError, match="authorization_adapter_required"):
        execution.request_run(session, task_id=task.id, context=_context(),
                              actor_id="runner", output_root="out", idempotency_key="y",
                              validate_context=None)


def test_claim_is_single_active_and_fenced_by_context(session):
    task = _delegated(session)
    other = _delegated(session, task_key="k3")
    run = execution.request_run(session, task_id=task.id, context=_context(),
                                 actor_id="runner", output_root="out", idempotency_key="i1",
                                 validate_context=_allow)
    other_run = execution.request_run(session, task_id=other.id, context=_context(),
                                      actor_id="runner", output_root="out", idempotency_key="i2",
                                      validate_context=_allow)
    claimed = execution.claim(session, run.id, worker="w1", validate_context=_allow)
    assert claimed.state == "claimed" and claimed.generation == 1
    # 并发单活：另一条就绪 Run 在租约期内不能同时认领
    assert execution.claim(session, other_run.id, worker="w2", validate_context=_allow) is None
    # 非队列状态不可重复认领
    assert execution.claim(session, run.id, worker="w2", validate_context=_allow) is None
    # 授权失效后同样拒绝认领（显式失败，不静默重试）
    other_run.state = "queued"
    session.flush()
    task.status = TaskStatus.OPEN
    denied = lambda s, c, a: False
    with pytest.raises(ValueError, match="authorization_invalid"):
        execution.claim(session, other_run.id, worker="w3", validate_context=denied)


def test_transition_fenced_by_generation_and_lease(session):
    task = _delegated(session)
    run = execution.request_run(session, task_id=task.id, context=_context(),
                                 actor_id="runner", output_root="out", idempotency_key="i",
                                 validate_context=_allow)
    claimed = execution.claim(session, run.id, worker="w1", validate_context=_allow)
    moved = execution.transition(session, run.id, worker="w1", generation=claimed.generation,
                                 state="running", validate_context=_allow,
                                 checkpoint={"done": ["repro"], "next": "patch"})
    assert moved.state == "running" and moved.attempts == 1
    assert task.status == "running"
    assert moved.checkpoint_json != "{}"
    # 旧代际不能回写
    with pytest.raises(ValueError, match="stale_run_lease"):
        execution.transition(session, run.id, worker="w1", generation=claimed.generation - 1,
                             state="waiting_acceptance", validate_context=_allow)
    # 无租约的 worker 不能回写
    with pytest.raises(ValueError, match="stale_run_lease"):
        execution.transition(session, run.id, worker="w2", generation=moved.generation,
                             state="waiting_acceptance", validate_context=_allow)
    finished = execution.transition(session, run.id, worker="w1", generation=moved.generation,
                                    state="waiting_acceptance", validate_context=_allow)
    assert finished.state == "waiting_acceptance"


def test_expired_lease_waits_for_reconciliation_instead_of_rerun(session):
    task = _delegated(session)
    run = execution.request_run(session, task_id=task.id, context=_context(),
                                 actor_id="runner", output_root="out", idempotency_key="i",
                                 validate_context=_allow)
    claimed = execution.claim(session, run.id, worker="w1", validate_context=_allow,
                              lease_seconds=1, now=utcnow() - timedelta(seconds=60))
    assert claimed.lease_until <= utcnow()
    recovered = execution.recover_expired(session, now=utcnow())
    assert run.id in recovered
    assert run.state == "waiting_recovery" and run.lease_owner == ""
    assert "reconcile" in run.error
    # 恢复后不得被直接重新认领为执行态（需先对账）
    assert execution.claim(session, run.id, worker="w2", validate_context=_allow) is None


def test_work_revision_change_invalidates_run(session):
    task = _delegated(session)
    run = execution.request_run(session, task_id=task.id, context=_context(),
                                 actor_id="runner", output_root="out", idempotency_key="i",
                                 validate_context=_allow)
    task.revision += 1
    session.flush()
    with pytest.raises(ValueError, match="work_revision_changed"):
        execution.claim(session, run.id, worker="w1", validate_context=_allow)


def test_delegated_done_requires_acceptance_service(session):
    task = _delegated(session)
    # 直接 done 写入口被收敛拒绝
    with pytest.raises(ValueError, match="delivery_acceptance_required"):
        set_status(session, task.id, status=TaskStatus.DONE, actor="alice")
    # 聊天完成信号只产生验收请求，不关闭委派任务
    message = Message(message_id="m1", tenant="t", chat_id="chat", source_id=1,
                      content_text="搞定了", create_time=1, sender_id="alice")
    session.add(message)
    session.flush()
    completed = apply_completion_signal(session, message)
    assert completed == []
    assert task.status == TaskStatus.OPEN
    events = [e.event_type for e in session.scalars(
        __import__("sqlalchemy").select(TaskEvent).where(TaskEvent.task_id == task.id))]
    assert "acceptance_requested" in events
    # 普通待办不受影响：完成信号照旧关闭
    plain = _delegated(session, task_key="k4", workflow_kind="legacy_todo")
    message2 = Message(message_id="m2", tenant="t", chat_id="chat", source_id=1,
                       content_text="已完成", create_time=2, sender_id="alice")
    session.add(message2)
    session.flush()
    done = apply_completion_signal(session, message2)
    assert plain.id in [t.id for t in done]
    assert plain.status == TaskStatus.DONE
