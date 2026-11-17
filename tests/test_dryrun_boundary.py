"""BP-02 dryrun 边界：冻结「允许的本地计算、是否付费、禁止的远端副作用」。

复审附加契约（`dryrun_effect`）：dryrun Run 不是显示标签，边界由执行器与
传输层执行：

- 允许：本地计算与产物（受限工具、检查器；容器网络 ``--network none``）；
- 付费：模型调用只接显式离线端口（``dryrun_safe``/``kind=fake``），真实
  付费 provider 一律拒绝且零预算记录；
- 禁止：真实远端副作用——``delivery_mode="dryrun"`` 的 outbox 意图在调用
  transport 之前被取消并留痕 ``dryrun_intent_blocked``；
- 未声明 dryrun 能力的执行器适配器在该 Run 上不执行（无任何副作用）。

全部离线：假 provider / 假传输 / 合成数据，无网络。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness import outbox as core_outbox
from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.governance import budget
from evoharness.governance.budget_gateway import BudgetGateway
from evoharness.governance.models import BudgetReservation
from evoharness.models import Base, Outbox, OutboxStatus
from evoharness.ports import TransportResult
from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
from evoharness.work.executor import AdapterResult
from evoharness.work.models import ExecutionRun
from evoharness.work.worker import WorkWorker
from evoharness.workflows.code_fix import ScriptTestRunner

ACCEPTANCE = [{"kind": "code", "criteria": "合成验收",
               "required_artifacts": ["diff", "test_report"]}]
CHECK = [sys.executable, "-c", "print('dryrun checker')"]
SCRIPT = [
    {"tool_calls": [{"name": "write_file", "arguments": {
        "path": "src/dryrun_probe.py", "content": "DRYRUN_LOCAL_ONLY = 1\n"}}],
     "content": "本地计算完成", "usage_tokens": 150},
    {"content": "dryrun 收尾", "final": True, "usage_tokens": 90},
]


class SideEffectAdapter:
    """未声明 dryrun 能力的适配器：execute 会写合成副作用标记。"""

    kind = "audit-synthetic"

    def __init__(self, marker: Path):
        self.marker = Path(marker)

    def bind_run(self, **kwargs):
        pass

    def unbind_run(self):
        pass

    def execute(self, workdir, context, allowed_tools, guard):
        self.marker.write_text("synthetic side effect", encoding="utf-8")
        return AdapterResult(ok=True, claimed_done=False)


class PaidProvider:
    """真实付费端口同构：无 dryrun 能力声明，绝不允许在 dryrun 中被调用。"""

    kind = "openai"
    usage_tokens = 0

    def __init__(self):
        self.calls = 0

    def complete(self, *, context, tools):
        self.calls += 1
        raise AssertionError("dryrun Run 不得调用真实付费 provider")


class StubTransport:
    def __init__(self):
        self.calls: list[str] = []

    def send_text(self, receive_id, text, idempotency_key=""):
        self.calls.append("send_text")
        return TransportResult("sent", receipt_id="om_dryrun_1")

    def send_card(self, receive_id, card, idempotency_key=""):
        self.calls.append("send_card")
        return TransportResult("sent", receipt_id="om_dryrun_card")

    def reply_text(self, message_id, text, idempotency_key="", reply_in_thread=False, **kw):
        self.calls.append("reply_text")
        return TransportResult("sent", receipt_id="om_dryrun_reply")

    def reply_card(self, message_id, card, idempotency_key="", reply_in_thread=False):
        self.calls.append("reply_card")
        return TransportResult("sent", receipt_id="om_dryrun_reply_card")


@pytest.fixture()
def app_env(tmp_path):
    application = Application.open("sqlite:///" + (tmp_path / "dryrun.db").as_posix())
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space", limit_tokens=100000)
    yield SimpleNamespace(application=application, api=api, tmp=tmp_path)
    application.engine.dispose()


def _make_dryrun(env, mode="dryrun"):
    created = env.api.create_work_item(
        tenant="tenant", owner="owner", title="dryrun 边界", goal="合成目标",
        org_id="org", space_id="space", workflow_kind="delegated_delivery",
        assignee_id="runner", acceptance=ACCEPTANCE)
    task_id = created["data"]["id"]
    payload = env.api.run_request_payload(task_id)["data"]
    approved = env.api.submit_approval(
        org_id="org", actor_id="owner", target_type="work_item",
        target_id=str(task_id), target_revision=payload["target_revision"],
        payload=payload["payload"], actions=["work.execute"], grantee_id="runner")
    assert approved["ok"] is True, approved
    started = env.api.start_run(task_id, actor_id="runner", mode=mode,
                                output_root=str(env.tmp / "run-out"))
    assert started["ok"] is True, started
    return task_id, started["data"]["operation_id"]


def _worker(env, adapter):
    return WorkWorker(env.application.session_factory, adapter,
                      env.api._authorize_adapter())


# ── 执行器能力门：未声明 dryrun 能力的适配器不执行 ──────────────────────────


def test_dryrun_refuses_adapter_without_capability(app_env):
    marker = app_env.tmp / "side-effect.txt"
    task_id, run_id = _make_dryrun(app_env)
    result = _worker(app_env, SideEffectAdapter(marker)).process_run(run_id)
    assert result["ok"] is False and result["run_state"] == "failed"
    assert not marker.exists(), "dryrun Run 不得执行未声明能力的适配器"
    with app_env.application.transaction() as session:
        run = session.get(ExecutionRun, run_id)
        assert run.state == "failed"
        assert "dryrun_adapter_not_safe" in (run.checkpoint_json or "")


# ── 允许的本地计算：显式离线 provider + 容器内工具 ──────────────────────────


def test_dryrun_allows_local_computation_with_offline_provider(app_env):
    provider = FakeProvider(list(SCRIPT))
    adapter = RestrictedAgentAdapter(
        provider=provider, gateway=BudgetGateway(app_env.application.session_factory),
        runner=ScriptTestRunner(CHECK), isolate_checks=False)
    task_id, run_id = _make_dryrun(app_env)
    result = _worker(app_env, adapter).process_run(run_id)
    assert result["ok"] is True and result["run_state"] == "waiting_acceptance", result
    assert provider.calls == 2
    workspace = Path(result["workspace"])
    assert (workspace / "src" / "dryrun_probe.py").is_file()
    with app_env.application.transaction() as session:
        calls = [row for row in session.scalars(select(BudgetReservation).where(
            BudgetReservation.run_id == run_id,
            BudgetReservation.category == "model_call"))]
        # 离线剧本用量照常逐笔结算（模拟账目），不触真实 provider
        assert len(calls) == 2 and sum(row.used_tokens for row in calls) == 240


# ── 付费边界：真实 provider 在 dryrun 中零调用、零预算记录 ───────────────────


def test_dryrun_refuses_paid_provider_without_budget_rows(app_env):
    paid = PaidProvider()
    adapter = RestrictedAgentAdapter(
        provider=paid, gateway=BudgetGateway(app_env.application.session_factory),
        runner=ScriptTestRunner(CHECK), isolate_checks=False)
    task_id, run_id = _make_dryrun(app_env)
    result = _worker(app_env, adapter).process_run(run_id)
    assert result["ok"] is False and result["run_state"] == "failed"
    assert paid.calls == 0
    with app_env.application.transaction() as session:
        run = session.get(ExecutionRun, run_id)
        assert "model_dryrun_no_provider" in (run.checkpoint_json or "")
        rows = list(session.scalars(select(BudgetReservation).where(
            BudgetReservation.run_id == run_id,
            BudgetReservation.category == "model_call")))
        assert rows == []


# ── 传输层：dryrun 意图不调 transport，live 意图照常投递 ─────────────────────

POLICY = SimpleNamespace(
    deployment_mode="offline",
    writes_enabled=True,
    business_reply_enabled=True,
    auto_reply_enabled=False,
    reply_allowed_for=lambda chat: True,
)


@pytest.fixture()
def outbox_env():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    transport = StubTransport()
    try:
        yield SimpleNamespace(factory=factory, transport=transport, engine=engine)
    finally:
        engine.dispose()


def test_dryrun_outbox_intent_blocked_at_transport(outbox_env):
    with outbox_env.factory.begin() as session:
        row = core_outbox.enqueue(
            session, intent="notice|dryrun|1", tenant="tenant", kind="notice",
            target_type="chat", target_id="oc_chat", payload={"text": "合成通知"},
            delivery_mode="dryrun")
        outbox_id = row.id
    results = core_outbox.dispatch_outbox(
        outbox_env.factory, transport=outbox_env.transport, policy=POLICY)
    assert outbox_env.transport.calls == [], "dryrun 意图绝不能被传输层发送"
    assert [r.status for r in results] == [OutboxStatus.CANCELLED]
    assert results[0].error == "dryrun_intent_blocked"
    with outbox_env.factory() as session:
        row = session.get(Outbox, outbox_id)
        assert row.status == OutboxStatus.CANCELLED
        assert row.last_error == "dryrun_intent_blocked"
        assert row.receipt_message_id is None and row.sent_at is None


def test_live_outbox_intent_still_delivers(outbox_env):
    with outbox_env.factory.begin() as session:
        core_outbox.enqueue(
            session, intent="notice|live|1", tenant="tenant", kind="notice",
            target_type="chat", target_id="oc_chat", payload={"text": "合成通知"})
    results = core_outbox.dispatch_outbox(
        outbox_env.factory, transport=outbox_env.transport, policy=POLICY)
    assert outbox_env.transport.calls == ["send_text"]
    assert [r.status for r in results] == [OutboxStatus.SENT]
    assert results[0].receipt_id == "om_dryrun_1"


def test_enqueue_rejects_invalid_delivery_mode(outbox_env):
    with outbox_env.factory.begin() as session:
        with pytest.raises(ValueError, match="invalid_delivery_mode"):
            core_outbox.enqueue(
                session, intent="notice|bad|1", tenant="tenant", kind="notice",
                target_type="chat", target_id="oc_chat", payload={"text": "x"},
                delivery_mode="simulate")
