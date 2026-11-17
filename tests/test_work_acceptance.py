"""DE-13：交付物登记、证据核对与独立验收。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.models import Base, Task, TaskEvent, TaskStatus
from evoharness.work import acceptance, execution, service
from evoharness.work.acceptance import AcceptanceError


def _allow(s, c, a):
    return True


@pytest.fixture()
def env(tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    out_root = tmp_path / "run-out"
    out_root.mkdir()
    with factory.begin() as s:
        task = Task(task_key="k1", tenant="t", owner="alice", chat_id="chat", title="修复",
                    action="fix", status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                    goal="修复缺陷", acceptance_json='[{"kind": "code", "criteria": "回归通过"}]',
                    revision=1, assignee_id="bob")
        s.add(task)
        s.flush()
        run = execution.request_run(s, task_id=task.id, context={"g": 1}, actor_id="runner-1",
                                    output_root=str(out_root), idempotency_key="i1",
                                    validate_context=_allow)
        s.flush()
        yield s, task, run, out_root
    engine.dispose()


def _run_to_review(s, task, run, actor="bob"):
    service.transition_to(s, task.id, to_state="queued", actor=actor)
    claimed = execution.claim(s, run.id, worker="w1", validate_context=_allow)
    service.transition_to(s, task.id, to_state="running", actor=actor)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_allow)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="waiting_acceptance", validate_context=_allow)
    acceptance.request_acceptance(s, task.id, run_id=run.id, requester=actor)


def _artifact(env, name, content="data"):
    s, task, run, out_root = env
    path = out_root / name
    path.write_text(content, encoding="utf-8")
    return acceptance.register_artifact(s, run, kind=name.rsplit(".", 1)[0], path=str(path))


def test_artifact_registration_rejects_path_traversal_and_missing(env):
    s, task, run, out_root = env
    outside = out_root.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(AcceptanceError, match="artifact_path_outside_output_root"):
        acceptance.register_artifact(s, run, kind="diff", path=str(outside))
    with pytest.raises(AcceptanceError, match="artifact_file_missing"):
        acceptance.register_artifact(s, run, kind="diff", path=str(out_root / "nope.txt"))


def test_exit_zero_without_evidence_cannot_pass(env):
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    # 没有任何产物：即使 verdict=pass 也拒绝（exit 0 / 模型自述不算数）
    with pytest.raises(AcceptanceError, match="acceptance_evidence_missing"):
        acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")


def test_stale_revision_evidence_cannot_prove_new_code(env):
    s, task, run, out_root = env
    # 登记旧 revision 的产物（修订前）
    _artifact(env, "diff.patch")
    _artifact(env, "test_report.txt")
    # 合同修订：revision+1，旧 Run 因 work_revision_changed fencing 失效
    service.revise_contract(s, task.id, actor="alice", goal="修复缺陷（补充场景）",
                            expected_revision=task.revision, reason="补充验收场景")
    with pytest.raises(ValueError, match="work_revision_changed"):
        execution.claim(s, run.id, worker="w2", validate_context=_allow)
    # 新 revision 的 Run 推进到审阅；拿旧 Run 的产物验收新代码被拒
    new_run = execution.request_run(s, task_id=task.id, context={"g": 2}, actor_id="runner-2",
                                    output_root=str(out_root), idempotency_key="i2",
                                    validate_context=_allow)
    _run_to_review(s, task, new_run)
    with pytest.raises(AcceptanceError, match="acceptance_evidence_stale_revision"):
        acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")


def test_review_requires_authorized_and_independent_reviewer(env):
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _artifact(env, "diff.patch")
    _artifact(env, "test_report.txt")
    # 陌生审阅者
    with pytest.raises(AcceptanceError, match="reviewer_not_authorized"):
        acceptance.review(s, task.id, run_id=run.id, reviewer="stranger", verdict="pass")
    # 审阅者不能是执行者本人：assignee 自己执行时不能自评（自评分不冒充独立验收）
    self_run = execution.request_run(s, task_id=task.id, context={"g": 9}, actor_id="bob",
                                     output_root=str(out_root), idempotency_key="i-self",
                                     validate_context=_allow)
    claimed = execution.claim(s, self_run.id, worker="w9", validate_context=_allow)
    execution.transition(s, self_run.id, worker="w9", generation=claimed.generation,
                         state="running", validate_context=_allow)
    execution.transition(s, self_run.id, worker="w9", generation=claimed.generation,
                         state="waiting_acceptance", validate_context=_allow)
    # task 已处于 in_review，直接对自执行 Run 验收
    with pytest.raises(AcceptanceError, match="reviewer_must_differ_from_executor"):
        acceptance.review(s, task.id, run_id=self_run.id, reviewer="bob", verdict="pass")


def test_pass_is_the_only_done_path_and_fail_reopens_rework(env):
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _artifact(env, "diff.patch")
    _artifact(env, "test_report.txt")
    # 聊天完成信号不能 done（唯一入口在验收服务）
    done = acceptance.review(s, task.id, run_id=run.id, reviewer="alice",
                             verdict="pass", notes="回归与差异齐备")
    assert done.status == TaskStatus.DONE
    events = [e.event_type for e in s.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id))]
    assert "acceptance_passed" in events

    # 重开后走返工路径：fail → running + 拒绝记录
    service.transition_to(s, task.id, to_state="queued", actor="alice",
                          reason="线上复现", evidence_message_ids=["m-re"])
    service.transition_to(s, task.id, to_state="running", actor="bob")
    task.status = "in_review"
    s.flush()
    rejected = acceptance.review(s, task.id, run_id=run.id, reviewer="alice",
                                 verdict="fail", notes="补边界用例")
    assert rejected.status == "running"
    events = [e.event_type for e in s.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id))]
    assert "acceptance_rejected" in events


def test_plain_todo_keeps_legacy_completion(env):
    s, task, run, out_root = env
    plain = Task(task_key="k2", tenant="t", owner="alice", chat_id="chat", title="待办",
                 action="todo", status=TaskStatus.OPEN, workflow_kind="legacy_todo")
    s.add(plain)
    s.flush()
    # 普通待办不走验收：owner 直接完成仍按既定行为
    service.set_status(s, plain.id, status=TaskStatus.DONE, actor="alice")
    assert plain.status == TaskStatus.DONE
