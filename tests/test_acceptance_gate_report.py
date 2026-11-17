"""DE-13：release_gate 结果作为显式验收证据（gate_report，离线确定性）。

任务把 gate_report 列入必需产物时：缺证据保持 in_review；JSON 不合格
（解析失败/缺冻结门禁摘要）拒绝；ok=false 拒绝；只有 passed + 门禁摘要
绑定才允许 done。不做「没写结论=通过」的静默放行。
"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.models import Base, Task, TaskEvent, TaskStatus
from evoharness.work import acceptance, execution, service

ACCEPTANCE = json.dumps([{
    "kind": "code",
    "criteria": "发布门通过",
    "required_artifacts": ["diff", "test_report", "gate_report"],
}])


def _allow(s, c, a):
    return True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(acceptance.RECEIPT_ENFORCE_ENV, raising=False)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    out_root = tmp_path / "run-out"
    out_root.mkdir()
    with factory.begin() as s:
        task = Task(task_key="k1", tenant="t", owner="alice", chat_id="chat",
                    title="发布", action="fix", status=TaskStatus.OPEN,
                    workflow_kind="delegated_delivery", goal="发布技能",
                    acceptance_json=ACCEPTANCE, revision=1, assignee_id="bob")
        s.add(task)
        s.flush()
        run = execution.request_run(s, task_id=task.id, context={"g": 1},
                                    actor_id="runner-1", output_root=str(out_root),
                                    idempotency_key="i1", validate_context=_allow)
        s.flush()
        s.flush()
        yield s, task, run, out_root
    engine.dispose()


def _to_review(s, task, run):
    service.transition_to(s, task.id, to_state="queued", actor="bob")
    claimed = execution.claim(s, run.id, worker="w1", validate_context=_allow)
    service.transition_to(s, task.id, to_state="running", actor="bob")
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_allow)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="waiting_acceptance", validate_context=_allow)
    acceptance.request_acceptance(s, task.id, run_id=run.id, requester="bob")


def _register(s, run, out_root, name, content, kind=None):
    path = out_root / name
    path.write_text(content, encoding="utf-8")
    return acceptance.register_artifact(
        s, run, kind=kind or name.rsplit(".", 1)[0], path=str(path))


def _gate_payload(**overrides):
    payload = {"version": "collab-gates.v2", "ok": True, "gatesSha256": "c" * 64,
               "overallRate": 1.0}
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _blocked_detail(s, task_id):
    rows = [e for e in s.scalars(select(TaskEvent).where(
        TaskEvent.task_id == task_id,
        TaskEvent.event_type == "acceptance_blocked"))]
    assert rows, "拒绝通过必须留下 acceptance_blocked 事件"
    return rows[-1].detail


def test_gate_report_passed_allows_done(env):
    s, task, run, out_root = env
    _to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    _register(s, run, out_root, "gate.json", _gate_payload(),
              kind="gate_report")
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert task.status == TaskStatus.DONE


def test_gate_report_failed_blocks_done(env):
    s, task, run, out_root = env
    _to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    _register(s, run, out_root, "gate.json",
              _gate_payload(ok=False, overallRate=0.5), kind="gate_report")
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert "gate_not_passed:gate_report:gate.json" in _blocked_detail(s, task.id)
    assert task.status != TaskStatus.DONE


def test_gate_report_without_gates_digest_is_invalid(env):
    s, task, run, out_root = env
    _to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    _register(s, run, out_root, "gate.json", json.dumps({"ok": True}),
              kind="gate_report")
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert "gate_report_invalid:gate_report:gate.json" in _blocked_detail(s, task.id)
    assert task.status != TaskStatus.DONE


def test_gate_report_malformed_json_is_invalid(env):
    s, task, run, out_root = env
    _to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    _register(s, run, out_root, "gate.json", "{not json",
              kind="gate_report")
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert "gate_report_invalid:gate_report:gate.json" in _blocked_detail(s, task.id)


def test_missing_gate_report_keeps_in_review(env):
    s, task, run, out_root = env
    _to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    with pytest.raises(acceptance.AcceptanceError, match="gate_report"):
        acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert task.status == "in_review"
