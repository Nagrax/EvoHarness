"""DE-10 模型侧协调提案：只生成事件，不改状态/合同/权限。

覆盖：围栏 JSON 解析、可信主体（模型自报 actor 被忽略）、证据必填、重复提案
幂等、过期 revision 拒绝、坏 JSON 不崩溃，以及适配器端到端落账后 Task 状态
与 revision 不变。全部离线。
"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.governance import budget
from evoharness.governance.budget_gateway import BudgetGateway
from evoharness.models import Base, Task, TaskEvent, TaskStatus
from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
from evoharness.work.models import ExecutionRun
from evoharness.work.proposals import CoordinationProposalSink, extract_proposal
from evoharness.work.worker import WorkWorker
from evoharness.workflows.code_fix import ScriptTestRunner

PROPOSAL = {
    "kind": "clarify",
    "payload": {"question": "需要确认验收口径"},
    "evidence_refs": ["message:m-1@r1"],
    "expected_effect": "发起一次澄清，不改变任务状态",
    "actor": "mallory",  # 模型自报身份必须被忽略
}

BLOCK = "```coordination_proposal\n" + json.dumps(PROPOSAL, ensure_ascii=False) + "\n```"


def _allow(s, c, a):
    return True


@pytest.fixture()
def db(tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        task = Task(task_key="k1", tenant="t", owner="alice", chat_id="chat",
                    title="提案", action="fix", status="running",
                    workflow_kind="delegated_delivery", goal="目标",
                    acceptance_json='[{"kind":"code"}]', revision=1,
                    assignee_id="bob")
        session.add(task)
        session.flush()
        run = ExecutionRun(id="run-1", task_id=task.id, tenant="t",
                           task_revision=1, idempotency_key="i1",
                           actor_id="runner", context_json="{}",
                           context_hash="0" * 64, output_root=str(tmp_path),
                           state="running", generation=1, lease_owner="w1")
        session.add(run)
        session.flush()
        task_id = task.id
    try:
        yield factory, task_id
    finally:
        engine.dispose()


def _count(factory) -> int:
    with factory() as session:
        return int(session.scalar(select(func.count(TaskEvent.id)).where(
            TaskEvent.event_type == "proposal")) or 0)


def test_extract_proposal_block():
    assert extract_proposal("说明\n" + BLOCK + "\n收尾")["kind"] == "clarify"
    assert extract_proposal("无提案") is None
    assert extract_proposal("```proposal\n{bad json}\n```") == {"_malformed": True}


def test_sink_records_with_trusted_actor_and_evidence(db):
    factory, task_id = db
    sink = CoordinationProposalSink(factory)
    outcome = sink.submit(run_id="run-1", task_id=task_id, actor="trusted-runner",
                          proposal=PROPOSAL)
    assert outcome["ok"] is True and outcome["reason"] == "proposal_recorded"
    with factory() as session:
        event = session.scalar(select(TaskEvent).where(
            TaskEvent.event_type == "proposal"))
        assert event.actor == "trusted-runner"          # 不是模型自报 mallory
        body = json.loads(event.evidence_message_ids)
        assert body["evidence_refs"] == ["message:m-1@r1"]
        assert body["expected_effect"].startswith("发起一次澄清")
        assert body["_key"]


def test_sink_deduplicates_identical_proposal(db):
    factory, task_id = db
    sink = CoordinationProposalSink(factory)
    first = sink.submit(run_id="run-1", task_id=task_id, actor="runner", proposal=PROPOSAL)
    second = sink.submit(run_id="run-1", task_id=task_id, actor="runner", proposal=PROPOSAL)
    assert first["reason"] == "proposal_recorded"
    assert second["reason"] == "proposal_duplicate"
    assert second["event_id"] == first["event_id"]
    assert _count(factory) == 1


def test_sink_rejects_stale_revision(db):
    factory, task_id = db
    with factory.begin() as session:
        task = session.get(Task, task_id)
        task.revision = 2
    outcome = CoordinationProposalSink(factory).submit(
        run_id="run-1", task_id=task_id, actor="runner", proposal=PROPOSAL)
    assert outcome == {"ok": False, "reason": "stale_task_revision"}
    assert _count(factory) == 0


def test_sink_rejects_invalid_kind_and_missing_evidence(db):
    factory, task_id = db
    sink = CoordinationProposalSink(factory)
    assert sink.submit(run_id="run-1", task_id=task_id, actor="runner",
                       proposal=dict(PROPOSAL, kind="execute"))["reason"] \
        == "invalid_proposal_kind:execute"
    assert sink.submit(run_id="run-1", task_id=task_id, actor="runner",
                       proposal=dict(PROPOSAL, evidence_refs=[]))["reason"] \
        == "proposal_evidence_required"
    assert sink.submit(run_id="run-1", task_id=task_id, actor="runner",
                       proposal={"_malformed": True})["reason"] \
        == "malformed_proposal"
    assert _count(factory) == 0


def test_adapter_records_proposal_without_changing_task(tmp_path):
    application = Application.open("sqlite:///" + (tmp_path / "proposal.db").as_posix())
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space", limit_tokens=100000)
    acceptance = [{"kind": "code", "criteria": "合成验收",
                   "required_artifacts": ["diff", "test_report"]}]
    created = api.create_work_item(tenant="tenant", owner="owner", title="提案链",
                                   goal="合成目标", org_id="org", space_id="space",
                                   workflow_kind="delegated_delivery",
                                   assignee_id="runner", acceptance=acceptance)
    task_id = created["data"]["id"]
    payload = api.run_request_payload(task_id)["data"]
    approved = api.submit_approval(
        org_id="org", actor_id="owner", target_type="work_item",
        target_id=str(task_id), target_revision=payload["target_revision"],
        payload=payload["payload"], actions=["work.execute"], grantee_id="runner")
    assert approved["ok"] is True
    started = api.start_run(task_id, actor_id="runner",
                            output_root=str(tmp_path / "run-out"))
    run_id = started["data"]["operation_id"]

    provider = FakeProvider([
        {"tool_calls": [{"name": "write_file", "arguments": {
            "path": "src/deliver.py", "content": "DONE = 1\n"}}],
         "content": "写入交付", "usage_tokens": 100},
        {"content": "交付完成\n" + BLOCK, "final": True, "usage_tokens": 40},
    ])
    adapter = RestrictedAgentAdapter(
        provider=provider, gateway=BudgetGateway(application.session_factory),
        runner=ScriptTestRunner([sys.executable, "-c", "print('ok')"]),
        isolate_checks=False,
        proposal_sink=CoordinationProposalSink(application.session_factory))
    worker = WorkWorker(application.session_factory, adapter, api._authorize_adapter())
    try:
        result = worker.process_run(run_id)
        assert result["ok"] is True and result["run_state"] == "waiting_acceptance"
        with application.transaction() as session:
            task = session.get(Task, task_id)
            assert task.status == "waiting_acceptance"   # 提案不推进完成
            assert task.revision == 1
            events = list(session.scalars(select(TaskEvent).where(
                TaskEvent.task_id == task_id, TaskEvent.event_type == "proposal")))
            assert len(events) == 1 and events[0].actor == "runner"
            run = session.get(ExecutionRun, run_id)
            assert "proposal_recorded" in (run.checkpoint_json or "")
    finally:
        application.engine.dispose()
