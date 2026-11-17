"""DE-10：工作项状态机、澄清流、合同修订与协调提案。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.models import Base, Message, Task, TaskEvent, TaskStatus
from evoharness.work import service
from evoharness.work.state import TRANSITIONS, legacy_status


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


def _task(session, **overrides) -> Task:
    values = dict(task_key="k1", tenant="t", owner="alice", chat_id="chat", title="修复",
                  action="fix", status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                  goal="修复缺陷", acceptance_json='["回归通过"]', revision=1)
    values.update(overrides)
    task = Task(**values)
    session.add(task)
    session.flush()
    return task


def test_transition_table_guards_done_and_waiting():
    # done 只能从 in_review 进入
    assert "done" in TRANSITIONS["in_review"]
    assert "done" not in TRANSITIONS["running"]
    assert "done" not in TRANSITIONS["verifying"]
    # 等待不能伪装完成
    for waiting in ("waiting_external", "waiting_budget", "waiting_authorization"):
        assert "done" not in TRANSITIONS[waiting]
        assert "queued" in TRANSITIONS[waiting]  # 依赖恢复后回到队列
    # 终态 cancelled 无出口；done 仅限显式重开
    assert TRANSITIONS["cancelled"] == set()
    assert TRANSITIONS["done"] == {"queued"}


def test_ready_gate_blocks_incomplete_delegated_contract(session):
    empty = _task(session, task_key="k2", goal="", acceptance_json="[]")
    with pytest.raises(ValueError, match="incomplete_work_contract"):
        service.transition_to(session, empty.id, to_state="ready", actor="alice")
    with pytest.raises(ValueError, match="incomplete_work_contract"):
        service.transition_to(session, empty.id, to_state="queued", actor="alice")
    # 普通待办不受 ready 门限制
    plain = _task(session, task_key="k3", workflow_kind="legacy_todo", goal="", acceptance_json="[]")
    moved = service.transition_to(session, plain.id, to_state="queued", actor="alice")
    assert moved.status == "queued"


def test_delegated_done_requires_acceptance_and_reopen_needs_evidence(session):
    task = _task(session)
    task.status = "in_review"
    session.flush()
    with pytest.raises(ValueError, match="delivery_acceptance_required"):
        service.transition_to(session, task.id, to_state=TaskStatus.DONE, actor="alice")
    # 非 in_review 状态直取 done 被状态机拒绝
    task.status = "running"
    session.flush()
    with pytest.raises(ValueError, match="invalid_task_transition"):
        service.transition_to(session, task.id, to_state=TaskStatus.DONE, actor="alice")
    # 显式重开需要新证据
    task.status = TaskStatus.DONE
    session.flush()
    with pytest.raises(ValueError, match="reopen_requires_new_evidence"):
        service.transition_to(session, task.id, to_state="queued", actor="alice",
                              reason="  ")
    reopened = service.transition_to(session, task.id, to_state="queued", actor="alice",
                                     reason="线上复现：同一缺陷在 v2 再现",
                                     evidence_message_ids=["m-re"])
    assert reopened.status == "queued"


def test_waiting_states_require_reason_and_record_wake(session):
    task = _task(session)
    task.status = "running"
    session.flush()
    with pytest.raises(ValueError, match="waiting_reason_required"):
        service.transition_to(session, task.id, to_state="waiting_external", actor="alice")
    waiting = service.transition_to(session, task.id, to_state="waiting_external", actor="alice",
                                    reason="依赖 OSS 凭据", wake_condition="凭据恢复后重排")
    assert waiting.waiting_reason == "依赖 OSS 凭据"
    assert waiting.next_action == "凭据恢复后重排"
    # 恢复回队列后等待原因清空
    queued = service.transition_to(session, waiting.id, to_state="queued", actor="alice")
    assert queued.waiting_reason == ""


def test_append_note_keeps_goal_and_revision_and_dedupes(session):
    task = _task(session)
    before = (task.revision, task.status)
    service.append_note(session, task, message_id="m-add-1", note="补充：也影响导出", actor="bob")
    assert (task.revision, task.status) == before
    dup = service.append_note(session, task, message_id="m-add-1", note="重复事件", actor="bob")
    assert dup.event_type == "note_ignored_duplicate"
    events = [e.event_type for e in session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id))]
    assert events.count("note_appended") == 1


def test_contract_revision_invalidates_old_runs_and_requires_reason(session):
    from evoharness.work import execution
    task = _task(session)

    def allow(s, c, a):
        return True

    run = execution.request_run(session, task_id=task.id, context={"g": 1}, actor_id="r",
                                output_root="out", idempotency_key="i1", validate_context=allow)
    with pytest.raises(ValueError, match="contract_change_reason_required"):
        service.revise_contract(session, task.id, actor="alice", goal="修复缺陷并补回归",
                                expected_revision=task.revision, reason="")
    # 目标变更：revision+1，旧 Run 的 task_revision 失配
    revised = service.revise_contract(session, task.id, actor="alice", goal="修复缺陷并补回归",
                                      expected_revision=task.revision, reason="验收补充",
                                      evidence_message_ids=["m-chg"])
    assert revised.revision == 2
    with pytest.raises(ValueError, match="work_revision_changed"):
        execution.claim(session, run.id, worker="w1", validate_context=allow)
    # 内容未变的修订幂等：revision 不变
    same = service.revise_contract(session, task.id, actor="alice", goal="修复缺陷并补回归",
                                   expected_revision=2, reason="重复提交")
    assert same.revision == 2


def test_clarification_flow_only_blocks_on_real_gaps(session):
    task = _task(session, task_key="kc", goal="", acceptance_json="[]",
                 status=TaskStatus.OPEN)
    # 非阻塞问题不改状态
    kept = service.request_clarification(session, task.id, actor="alice",
                                         question="顺带确认优先级", blocking=False)
    assert kept.status == TaskStatus.OPEN
    # 阻塞缺项转待澄清
    blocked = service.request_clarification(session, task.id, actor="alice",
                                            question="缺少验收标准")
    assert blocked.status == TaskStatus.NEEDS_CONFIRMATION
    # 澄清解决后合同仍不完整 → 保持 open 且不能 ready（不伪装可执行）
    resolved = service.resolve_clarification(session, task.id, actor="bob",
                                             answer_message_id="m-ans")
    assert resolved.status == "open"
    with pytest.raises(ValueError, match="incomplete_work_contract"):
        service.transition_to(session, task.id, to_state="ready", actor="bob")
    # 补齐合同后可进入 ready
    service.revise_contract(session, task.id, actor="bob", goal="修复", acceptance=["回归"],
                            expected_revision=task.revision, reason="澄清后补齐")
    ready = service.transition_to(session, task.id, to_state="ready", actor="bob")
    assert ready.status == "ready"


def test_proposal_cannot_change_state_or_contract(session):
    task = _task(session)
    event = service.propose(session, task.id, actor="coordinator", kind="plan",
                            payload={"steps": ["repro", "fix"], "expected_revision": 1})
    assert event.event_type == "proposal"
    with pytest.raises(ValueError, match="invalid_proposal_kind"):
        service.propose(session, task.id, actor="coordinator", kind="execute",
                        payload={"tool": "shell"})
    # 提案本身没有改变状态、revision 或合同
    session.refresh(task)
    assert task.status == TaskStatus.OPEN and task.revision == 1
    types = [e.event_type for e in session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id))]
    assert "proposal" in types


def test_legacy_projection_keeps_four_states_for_old_consumers(session):
    queued = _task(session, task_key="kl1", status="queued")
    waiting = _task(session, task_key="kl2", status="waiting_budget")
    done = _task(session, task_key="kl3", status=TaskStatus.DONE)
    confirmed = _task(session, task_key="kl4", status=TaskStatus.NEEDS_CONFIRMATION)
    assert legacy_status(queued) == "open"
    assert legacy_status(waiting) == "open"
    assert legacy_status(done) == "done"
    assert legacy_status(confirmed) == "needs_confirmation"
    # 新状态在语义查询集合内，旧 SQL 只查 open 也能看到
    from evoharness.work.state import ACTIVE
    assert "queued" in ACTIVE and "waiting_budget" in ACTIVE
