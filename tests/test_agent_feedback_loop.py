"""N05 反馈通道与原生委派回归：工具结果必须进入下一轮模型可见历史。

复审探针 1（budget_total_and_feedback）修复后必须失效：
- `provider_received_contexts_identical=false`：每轮 context 不再恒定；
- `tool_result_reaches_later_model=true`：后续轮 context 含第一轮 write 与
  read 的工具结果（provider 注入端口经 context["history"]，原生委派经
  agents.Agent 既有消息历史——同一语义、两条通道都覆盖）；
- 真实 usage 240 逐笔结算（N02/N04 口径不变）。

条件执行回归（不能只按序播放固定脚本）：第 1 轮读文件得 sentinel，第 2 轮
模型必须依据 sentinel 内容决定第 2 步动作——provider 端口（可调用剧本按
context 分叉）与原生委派（假 SDK 按 messages 分叉）各自覆盖。
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness.__main__ import main as root_main
from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.governance import budget, budget_gateway
from evoharness.governance.budget_gateway import BudgetGateway
from evoharness.governance.models import BudgetReservation
from evoharness.work.agent_adapter import (
    FakeProvider,
    GatewayModelPort,
    RestrictedAgentAdapter,
)
from evoharness.work.models import ExecutionRun
from evoharness.work.worker import WorkWorker, WorkerConfig
from evoharness.workflows.code_fix import ScriptTestRunner

try:
    import agents.agent  # noqa: F401  — 原生委派通道需要 agents 包（rich/SDK）
    AGENTS_IMPORTABLE = True
except Exception:  # noqa: BLE001 — 离线最小部署：原生通道测试显式跳过（其余全跑）
    AGENTS_IMPORTABLE = False

native_required = pytest.mark.skipif(not AGENTS_IMPORTABLE,
                                     reason="agents.Agent unavailable in this env")

CHECK_SCRIPT = ("import pathlib, sys; "
                "sys.exit(0 if pathlib.Path('src/gateway_timeout.py').is_file() else 1)")
# 反馈通道测试关注上下文/条件路径：检查器用确定通过的透传 runner
PASS_CHECKER = ["python3", "-c", "print('pass-through checker')"]
CODE_ACCEPTANCE = [{"kind": "code", "criteria": "回归全部通过",
                    "required_artifacts": ["diff", "test_report"]}]


@pytest.fixture()
def env(tmp_path):
    application = Application.open("sqlite:///" + (tmp_path / "fb.db").as_posix())
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    out = io.StringIO()
    code = root_main(["--db", "sqlite:///" + (tmp_path / "fb.db").as_posix(),
                      "--json", "init", "--org-id", "org-demo"], stdout=out)
    assert code == 0, out.getvalue()
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    yield SimpleNamespace(application=application, api=api, tmp=tmp_path)
    application.engine.dispose()


def _approved_run(env, *, provider_script=None, adapter=None, title="反馈通道任务"):
    created = env.api.create_work_item(
        tenant="local", owner="founder", title=title, action="fix",
        goal="交付补丁且回归全部通过", acceptance=CODE_ACCEPTANCE,
        workflow_kind="delegated_delivery", org_id="org-demo",
        space_id="space-support")
    task_id = created["data"]["id"]
    assert env.api.transition_work_item(task_id, to_state="queued",
                                        actor="founder")["ok"]
    payload = env.api.run_request_payload(task_id)["data"]
    assert env.api.submit_approval(
        org_id="org-demo", actor_id="founder", target_type="work_item",
        target_id=str(task_id), target_revision=payload["target_revision"],
        payload=payload["payload"], actions=["work.execute"],
        grantee_id="ai-support")["ok"]
    started = env.api.start_run(task_id, actor_id="ai-support",
                                output_root=str(env.tmp / f"out-{task_id}"))
    run_id = started["data"]["operation_id"]
    # 工作区前置文件（sentinel 只写在文件里，模型从未发送过该值）
    workspace = Path(started["data"].get("output_root") or
                     (env.tmp / f"out-{task_id}")) / f"run-{run_id[:12]}"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "secret.txt").write_text("SENTINEL_VALUE_77=on\n", encoding="utf-8")
    if adapter is None:
        adapter = RestrictedAgentAdapter(
            provider=FakeProvider(provider_script),
            gateway=BudgetGateway(env.application.session_factory),
            runner=ScriptTestRunner([sys.executable, "-c", CHECK_SCRIPT]))
    worker = WorkWorker(env.application.session_factory, adapter,
                        env.api._authorize_adapter(),
                        config=WorkerConfig(worker_id="w-fb"))
    return run_id, adapter, worker


class ObservingProvider(FakeProvider):
    """记录每轮收到的 context（复审探针 1 同构观测点）。"""

    def __init__(self, script):
        super().__init__(script)
        self.contexts: list[dict] = []

    def complete(self, *, context, tools):
        self.contexts.append(json.loads(json.dumps(context, ensure_ascii=False)))
        return super().complete(context=context, tools=tools)


# ── 探针 1 同构：context 逐轮不同 + 工具结果到达后续轮 ────────────────────────


def test_tool_results_reach_later_model_rounds(env):
    script = [
        {"tool_calls": [{"name": "write_file", "arguments": {
            "path": "result.py", "content": "AUDIT_SENTINEL_481=1"}}],
         "content": "write", "usage_tokens": 150},
        {"tool_calls": [{"name": "read_file", "arguments": {
            "path": "result.py"}}], "content": "read", "usage_tokens": 70},
        {"content": "synthetic done", "final": True, "usage_tokens": 20},
    ]
    provider = ObservingProvider(script)
    adapter = RestrictedAgentAdapter(
        provider=provider, gateway=BudgetGateway(env.application.session_factory),
        runner=ScriptTestRunner(PASS_CHECKER))
    run_id, adapter, worker = _approved_run(env, adapter=adapter)
    result = worker.process_run(run_id)
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result

    seen = provider.contexts
    assert len(seen) == 3
    # 修复前：每轮 context 完全相同；修复后：history 随轮累积 → 不再恒定
    assert not all(c == seen[0] for c in seen), "provider_received_contexts_identical"
    # 第一轮无历史；第二轮含 write 工具结果；第三轮含 read 工具结果（哨兵内容）
    assert seen[0].get("history") == []
    round2_blob = json.dumps(seen[1], ensure_ascii=False)
    assert "AUDIT_SENTINEL_481" in round2_blob
    round2_tools = [entry for entry in seen[1]["history"] if entry.get("role") == "tool"]
    assert any(entry["name"] == "write_file" and entry["ok"] for entry in round2_tools)
    round3_blob = json.dumps(seen[2], ensure_ascii=False)
    assert "AUDIT_SENTINEL_481=1" in round3_blob
    round3_tools = [entry for entry in seen[2]["history"] if entry.get("role") == "tool"]
    assert any(entry["name"] == "read_file" and "AUDIT_SENTINEL_481=1" in entry["detail"]
               for entry in round3_tools)
    # 真实 usage 240 逐笔结算（N02/N04 口径不变）
    with env.application.transaction() as session:
        summary = budget.usage_summary(session, org_id="org-demo")
    assert summary["used_total"] == 240
    assert adapter.last_usage_tokens == 240


# ── 条件执行（provider 端口）：第 2 轮模型必须依据第 1 轮工具返回决策 ──────────


def test_conditional_execution_provider_port(env):
    """第 1 轮读文件得 sentinel，第 2 轮按 sentinel 值决定写入哪个决策文件。"""

    def script(context):
        # FakeProvider 可调用剧本约定：每次返回完整剧本，按调用序号取幕；
        # 剧本内容按本轮 context 中的工具结果分叉（不是固定顺序播放）。
        blob = json.dumps(context.get("history") or [], ensure_ascii=False)
        read_turn = {"tool_calls": [{"name": "read_file", "arguments": {
            "path": "secret.txt"}}], "content": "read", "usage_tokens": 60}
        if "SENTINEL_VALUE_77=on" in blob:
            decided = {"tool_calls": [{"name": "write_file", "arguments": {
                "path": "decision-on.txt", "content": "conditional path on"}}],
                "content": "decide-on", "usage_tokens": 60}
        else:
            decided = {"tool_calls": [{"name": "write_file", "arguments": {
                "path": "decision-off.txt", "content": "conditional path off"}}],
                "content": "decide-off", "usage_tokens": 60}
        return [read_turn, decided, {"content": "done", "final": True,
                                     "usage_tokens": 20}]

    provider = ObservingProvider(script)
    adapter = RestrictedAgentAdapter(
        provider=provider, gateway=BudgetGateway(env.application.session_factory),
        runner=ScriptTestRunner(PASS_CHECKER))
    run_id, _adapter, worker = _approved_run(env, adapter=adapter)
    result = worker.process_run(run_id)
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result
    workspace = Path(result["workspace"])
    # 条件路径：按第 1 轮读到的 sentinel 值走 on 分支
    assert (workspace / "decision-on.txt").is_file()
    assert not (workspace / "decision-off.txt").exists()
    # context 逐轮不同，且第 2 轮 context 含第 1 轮读结果（sentinel 值）
    assert not all(c == provider.contexts[0] for c in provider.contexts)
    round2 = json.dumps(provider.contexts[1], ensure_ascii=False)
    assert "SENTINEL_VALUE_77=on" in round2
    assert provider.contexts[0].get("history") == []


# ── 条件执行（原生委派 + 假 SDK）：agents.Agent 消息历史承载工具结果 ───────────


class _SdkResponse:
    """OpenAI 协议非流式假响应（duck-type _call_openai_nostream 的被用面）。"""

    @staticmethod
    def build(content: str, tool_calls: list[tuple[str, dict]], usage: tuple[int, int]):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=[SimpleNamespace(
                        id=f"call_fake_{index}",
                        function=SimpleNamespace(
                            name=name,
                            arguments=json.dumps(args, ensure_ascii=False)))
                        for index, (name, args) in enumerate(tool_calls)] or None),
                finish_reason="tool_calls" if tool_calls else "stop")],
            usage=SimpleNamespace(prompt_tokens=usage[0],
                                  completion_tokens=usage[1]))


class ConditionalFakeSDK:
    """条件执行假 SDK：第 2 轮响应由第 1 轮工具结果决定（非固定剧本）。

    注入口语义：sdk_client 注入 = 委派机制离线回归（不触网），网关记账由
    生产装配的 GatewayModelPort 承担（另行单测覆盖预留/结算契约）。
    """

    kind = "fake-sdk"

    def __init__(self):
        self.messages_seen: list[list[dict]] = []
        self.calls = 0
        # 客户端面：Agent 调用 chat.completions.create → 本对象的 create
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        self.calls += 1
        snapshot = json.loads(json.dumps(messages, ensure_ascii=False, default=str))
        self.messages_seen.append(snapshot)
        blob = json.dumps(snapshot, ensure_ascii=False, default=str)
        if self.calls >= 3:
            # 决策动作已执行：收尾（轮数终止，不是固定剧本——第 2 轮分叉仍是条件的）
            return _SdkResponse.build("conditional delivery complete", [], (20, 0))
        if "SENTINEL_VALUE_77" not in blob:
            return _SdkResponse.build("", [("read_file", {"file_path": "secret.txt"})],
                                      (150, 0))
        if "SENTINEL_VALUE_77=on" in blob:
            return _SdkResponse.build("", [("write_file", {
                "file_path": "decision-on.txt",
                "content": "conditional path on"})], (70, 0))
        return _SdkResponse.build("", [("write_file", {
            "file_path": "decision-off.txt",
            "content": "conditional path off"})], (70, 0))


@native_required
def test_conditional_execution_native_delegation(env, monkeypatch):
    """原生委派：工具结果经 agents.Agent 消息历史进入下一轮；任务按条件路径完成。"""
    monkeypatch.setenv("NO_STREAM", "1")   # 假 SDK 走非流式路径（确定性）
    fake_sdk = ConditionalFakeSDK()
    adapter = RestrictedAgentAdapter(
        gateway=BudgetGateway(env.application.session_factory),
        runner=ScriptTestRunner(PASS_CHECKER),
        sdk_client=fake_sdk)
    run_id, adapter, worker = _approved_run(env, adapter=adapter)
    result = worker.process_run(run_id)
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result
    workspace = Path(result["workspace"])
    assert (workspace / "decision-on.txt").is_file()
    assert not (workspace / "decision-off.txt").exists()
    # 第二轮模型收到的 messages 含第一轮 read 工具结果（sentinel 值只存在于结果里）
    assert fake_sdk.calls == 3
    assert len(fake_sdk.messages_seen) == 3
    assert not all(m == fake_sdk.messages_seen[0] for m in fake_sdk.messages_seen)
    second_round_tools = [m for m in fake_sdk.messages_seen[1]
                          if isinstance(m, dict) and m.get("role") == "tool"]
    assert any("SENTINEL_VALUE_77=on" in str(m.get("content"))
               for m in second_round_tools)
    assert adapter.native_calls == 3


@native_required
def test_native_delegation_fails_closed_without_model_config(env, monkeypatch):
    """live 装配缺凭据 → model_not_configured（fail-closed，不静默降级）。"""
    for key in ("APIKEY", "EVOHARNESS_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    from evoharness.work.agent_adapter import _native_model_config
    assert _native_model_config() is None
    adapter = RestrictedAgentAdapter(
        gateway=BudgetGateway(env.application.session_factory),
        runner=ScriptTestRunner([sys.executable, "-c", CHECK_SCRIPT]))
    run_id, adapter, worker = _approved_run(env, adapter=adapter)
    result = worker.process_run(run_id)
    assert result.get("ok") is False, result
    with env.application.transaction() as session:
        run_row = session.get(ExecutionRun, run_id)
        assert run_row.state == "failed"
        assert "model_not_configured" in (run_row.checkpoint_json or "")


# ── 网关感知 SDK 端口（生产装配单测）：call_id 意图→调用→结算 ──────────────────


class _FakeCompletions:
    def __init__(self, responses, *, stream_chunks=None):
        self.responses = list(responses)
        self.stream_chunks = stream_chunks
        self.seen_kwargs: list[dict] = []

    async def create(self, **kwargs):
        self.seen_kwargs.append(kwargs)
        if kwargs.get("stream") and self.stream_chunks is not None:
            return _FakeStream(list(self.stream_chunks))
        return self.responses.pop(0)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeRealOpenAI:
    def __init__(self, responses=(), *, stream_chunks=None):
        self.chat = SimpleNamespace(completions=_FakeCompletions(
            responses, stream_chunks=stream_chunks))


def test_gateway_port_reserves_settles_and_records_intent(env):
    """每次模型调用：call_id 意图→executing 留痕→真实 usage 结算（N02 语义）。"""
    settled: list[int] = []
    port = GatewayModelPort(BudgetGateway(env.application.session_factory),
                            budget_kwargs={"org_id": "org-demo",
                                           "space_id": "space-support",
                                           "principal_id": "ai-support",
                                           "category": "model_call"},
                            on_settled=settled.append)
    port.real = _FakeRealOpenAI(responses=[
        _SdkResponse.build("hi", [], (137, 13))])
    port.model = "fake-model"
    client = asyncio.run(_call_once(port))
    assert client is not None
    assert settled == [150]                   # 真实 usage = prompt+completion
    with env.application.transaction() as session:
        rows = list(session.scalars(select(BudgetReservation).where(
            BudgetReservation.category == "model_call")))
        assert len(rows) == 1
        row = rows[0]
        assert row.call_id and row.status == "settled"
        assert row.used_tokens == 150 and row.ok is True
        assert row.intent_status == "recorded" and row.usage_status == "recorded"
    assert port.calls == 1


async def _call_once(port):
    from evoharness.work.agent_adapter import _GatewayOpenAIClient
    client = _GatewayOpenAIClient(port)
    await client.chat.completions.create(model="fake-model",
                                         messages=[{"role": "user", "content": "hi"}])
    return client


def test_gateway_port_stream_usage_captured_then_settled(env):
    """流式调用：usage chunk 捕获后于流结束时结算（不保守全额归集）。"""
    settled: list[int] = []
    port = GatewayModelPort(BudgetGateway(env.application.session_factory),
                            budget_kwargs={"org_id": "org-demo",
                                           "category": "model_call"},
                            on_settled=settled.append)
    port.model = "fake-model"
    chunk_no_usage = SimpleNamespace(usage=None, choices=[])
    chunk_usage = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=200, completion_tokens=33), choices=[])
    port.real = _FakeRealOpenAI(stream_chunks=[chunk_no_usage, chunk_usage])
    client = _gateway_client(port)

    async def _consume():
        stream = await client.chat.completions.create(
            model="fake-model", messages=[{"role": "user", "content": "hi"}],
            stream=True, stream_options={"include_usage": True})
        async for _chunk in stream:
            pass

    asyncio.run(_consume())
    assert settled == [233]
    with env.application.transaction() as session:
        row = session.scalar(select(BudgetReservation).where(
            BudgetReservation.category == "model_call"))
        assert row.status == "settled" and row.used_tokens == 233


def _gateway_client(port):
    from evoharness.work.agent_adapter import _GatewayOpenAIClient
    return _GatewayOpenAIClient(port)


def test_gateway_port_failure_settles_conservatively(env):
    """调用异常：保守全额归集（usage_status=unknown），不留免费副作用。"""
    settled: list[int] = []
    port = GatewayModelPort(BudgetGateway(env.application.session_factory),
                            budget_kwargs={"org_id": "org-demo",
                                           "category": "model_call"},
                            on_settled=settled.append)
    port.model = "fake-model"

    class _Boom:
        async def create(self, **kwargs):
            raise RuntimeError("synthetic sdk failure")

    port.real = SimpleNamespace(chat=SimpleNamespace(completions=_Boom()))
    client = _gateway_client(port)
    with pytest.raises(RuntimeError):
        asyncio.run(client.chat.completions.create(
            model="fake-model", messages=[{"role": "user", "content": "hi"}]))
    assert settled and settled[0] > 0        # 保守全额（预留额）
    with env.application.transaction() as session:
        row = session.scalar(select(BudgetReservation).where(
            BudgetReservation.category == "model_call"))
        assert row.status == "settled" and row.ok is False
        assert row.usage_status == "unknown"
