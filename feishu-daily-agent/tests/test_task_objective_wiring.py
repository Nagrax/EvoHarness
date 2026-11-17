"""DE-39 最小接线：通道层 tasks 适配器暴露目标/里程碑只读投影与证据回写入口。

- task_objective_refs：任务覆盖的里程碑/目标引用（只读，不改四态语义）；
- record_acceptance_evidence：只在 DE-13 验收 done 后可用；聊天完成信号
  不能经它回写（task 未验收 done 即拒绝），更不能关闭上层目标。
全部离线：内存 SQLite + 假授权适配器。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.domain import tasks as tasks_module
from evoharness.governance import authorization as gov
from evoharness.ids import sha256_json
from evoharness.models import Base, Message, Task, TaskStatus
from evoharness.organization import service as org
from evoharness.organization.service import AuthenticatedPrincipal
from evoharness.work import acceptance, execution, objectives
from evoharness.work.objectives import Milestone, ObjectiveError
from evoharness.work.service import apply_completion_signal, transition_to

_WIRE_OWNER = AuthenticatedPrincipal(principal_id="alice", tenant_id="tn", app_id="app",
                                     authenticated=True,
                                     authorities=frozenset({"organization.bootstrap",
                                                            "organization.manage"}))


@pytest.fixture()
def session(tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    out_root = tmp_path / "run-out"
    out_root.mkdir()
    with factory.begin() as s:
        org.bootstrap_organization(s, _WIRE_OWNER, org_id="org", title="接线组织",
                                   membership_id="m0", budget_limit=10)
        task = Task(task_key="k-wire", tenant="t", owner="alice", chat_id="chat",
                    title="接线任务", action="fix", status=TaskStatus.OPEN,
                    workflow_kind="delegated_delivery", goal="交付补丁",
                    acceptance_json=json.dumps([{"kind": "code", "criteria": "回归通过"}],
                                               ensure_ascii=False),
                    revision=1, org_id="org", space_id="support", assignee_id="bob")
        s.add(task)
        s.flush()
        yield s, task, out_root
    engine.dispose()


def _make_objective(s):
    payload = objectives.creation_payload(title="接线目标",
                                          acceptance=[{"criteria": "回归全绿"}],
                                          owner="alice")
    gov.approve(s, "alice", org_id="org", target_type="objective",
                target_id="obj-wire", target_revision=1,
                payload_hash=sha256_json(payload), actions={"objective.manage"})
    return objectives.create_objective(s, org_id="org", objective_id="obj-wire",
                                       title="接线目标",
                                       acceptance=[{"criteria": "回归全绿"}],
                                       owner="alice", actor_id="alice")


def _run_to_done(s, task, out_root):
    allow = lambda *args, **kwargs: True  # noqa: E731
    run = execution.request_run(s, task_id=task.id, context={"k": "wire"},
                                actor_id="bob", output_root=str(out_root),
                                idempotency_key="i-wire", validate_context=allow)
    transition_to(s, task.id, to_state="queued", actor="bob")
    claimed = execution.claim(s, run.id, worker="w1", validate_context=allow)
    transition_to(s, task.id, to_state="running", actor="bob")
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=allow)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="waiting_acceptance", validate_context=allow)
    acceptance.request_acceptance(s, task.id, run_id=run.id, requester="bob")
    for name in ("diff.patch", "test_report.txt"):
        path = out_root / name
        path.write_text("data", encoding="utf-8")
        acceptance.register_artifact(s, run, kind=name.rsplit(".", 1)[0], path=str(path))
    return acceptance.review(s, task.id, run_id=run.id, reviewer="alice",
                             verdict="pass", notes="ok")


def test_task_objective_refs_and_evidence_wiring(session, tmp_path):
    s, task, out_root = session
    _make_objective(s)
    objectives.add_milestone(s, objective_id="obj-wire", milestone_id="m-wire",
                             title="交付里程碑", deliverable_condition="补丁与回归报告",
                             actor_id="alice", covered_by_task_ids=[task.id])
    # 只读投影：任务 → 里程碑/目标引用
    refs = tasks_module.task_objective_refs(s, task)
    assert refs == [{"milestone_id": "m-wire", "objective_id": "obj-wire",
                     "milestone_status": "pending"}]
    progress = tasks_module.objective_progress(s, objective_id="obj-wire")
    assert progress["percent_complete"] is None
    # 聊天完成信号不能回写：任务未验收 done
    message = Message(message_id="m-wire-chat", tenant="t", chat_id="chat",
                      thread_id="", sender_id="bob", sender_name="bob",
                      content_text="已完成", create_time=0)
    s.add(message)
    s.flush()
    assert apply_completion_signal(s, message) == []
    with pytest.raises(ObjectiveError, match="milestone_evidence_requires_accepted_delivery"):
        tasks_module.record_acceptance_evidence(s, task=task, actor_id="alice")
    # 验收 done 之后才允许回写；只更新覆盖的里程碑，不关闭目标
    done = _run_to_done(s, task, out_root)
    assert done.status == TaskStatus.DONE
    milestones = tasks_module.record_acceptance_evidence(s, task=task, actor_id="alice",
                                                         artifact_refs=["art-9"])
    assert [m.milestone_id for m in milestones] == ["m-wire"]
    assert s.get(Milestone, "m-wire").status == "evidenced"
    assert objectives.objective_progress(s, objective_id="obj-wire")["status"] == "active"
    refs = tasks_module.task_objective_refs(s, task)
    assert refs[0]["milestone_status"] == "evidenced"
    # 计划视图入口可导入可用（无计划时不写任何状态）
    assert callable(tasks_module.plan_overview)
    assert callable(tasks_module.plan_staleness)
    assert list(s.scalars(select(Task).where(Task.tenant == "t")))[0].id == task.id
