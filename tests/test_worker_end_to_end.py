"""R03 离线端到端：根入口 → 排队 → 生产 Worker 认领（预算预留）→ 受限 Agent 执行
→ R07 产物完整性验收 → Task done。

对应独立审核 R03/P1（"排队成功不等于数字员工开始工作"）的核心交付：
- 全链从根入口 ``python -m evoharness init``（合成组织）起步，经 application
  组装层建单/批准/派工，由 ``evoharness.work.worker.WorkWorker`` 真实消费队列；
- ``RestrictedAgentAdapter`` + ``FakeProvider``（脚本化回复，不触网）在隔离
  工作区执行：工具白名单/写根 guard 硬约束、DE-29 每 Run 独立沙箱根、
  真实检查运行器、R07 检查回执；
- 预算网关契约（fail-closed）：无预算记录的付费调用拒绝、有预算正常、
  预留不足失败、结算后账目逐笔可对；
- R05 租约心跳（控制时钟推进跨过固定租约仍可续租回写）、取消传播、
  R06 崩溃恢复接管、单实例锁、未认领释放预留，全部离线确定性覆盖。

真实模型/真实平台仍单列不碰（见 AUDIT-FIXES/R03-20261114.md 剩余接线清单）。
"""
from __future__ import annotations

import io
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness.__main__ import main as root_main
from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.clock import utcnow as real_utcnow
from evoharness.governance import budget, budget_gateway
from evoharness.governance.budget_gateway import BudgetGateway, NoBudgetRecord
from evoharness.governance.models import BudgetReservation
from evoharness.work import execution
from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
from evoharness.work.models import ArtifactReceipt, ExecutionRun
from evoharness.work.worker import WorkWorker, WorkerConfig
from evoharness.workflows.code_fix import ScriptTestRunner
from ops.single_active import SingleActiveRefused

CHECK_SCRIPT = ("import pathlib, sys; "
                "sys.exit(0 if pathlib.Path('src/gateway_timeout.py').is_file() else 1)")

CODE_ACCEPTANCE = [{"kind": "code", "criteria": "回归全部通过",
                    "required_artifacts": ["diff", "test_report"]}]
CODE_SCRIPT = [
    {"tool_calls": [{"name": "write_file", "arguments": {
        "path": "src/gateway_timeout.py",
        "content": "TIMEOUT_SECONDS = 5  # 修复：登录网关超时从 30s 收敛为 5s\n"}}],
     "content": "最小修复已写入隔离工作区", "usage_tokens": 150},
    {"content": "修复完成，等待真实门禁", "final": True, "usage_tokens": 90},
]


def _script_factory(base_script):
    return lambda context: list(base_script)


@pytest.fixture()
def env(tmp_path):
    application = Application.open("sqlite:///" + (tmp_path / "e2e.db").as_posix())
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    out = io.StringIO()
    code = root_main(["--db", "sqlite:///" + (tmp_path / "e2e.db").as_posix(),
                      "--json", "init", "--org-id", "org-demo"], stdout=out)
    assert code == 0, out.getvalue()
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo", limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    gateway = BudgetGateway(application.session_factory)
    provider = FakeProvider(_script_factory(CODE_SCRIPT))
    adapter = RestrictedAgentAdapter(
        provider=provider, gateway=gateway,
        runner=ScriptTestRunner([sys.executable, "-c", CHECK_SCRIPT]))
    worker = WorkWorker(application.session_factory, adapter,
                        api._authorize_adapter(),
                        config=WorkerConfig(worker_id="w-e2e"))
    yield SimpleNamespace(application=application, api=api, worker=worker,
                          gateway=gateway, provider=provider, adapter=adapter,
                          tmp=tmp_path)
    application.engine.dispose()


def _create_approved_run(env, *, title="修复登录超时缺陷", acceptance=None,
                         actor="ai-support"):
    """根入口组装层：create_work_item → queued → 显式批准（grantee 绑定）→ start_run。"""
    created = env.api.create_work_item(
        tenant="local", owner="founder", title=title, action="fix",
        goal="交付补丁且回归全部通过",
        acceptance=acceptance or CODE_ACCEPTANCE,
        workflow_kind="delegated_delivery",
        org_id="org-demo", space_id="space-support")
    assert created.get("ok") is True, created
    task_id = created["data"]["id"]
    assert env.api.transition_work_item(task_id, to_state="queued",
                                        actor="founder")["ok"]
    payload = env.api.run_request_payload(task_id)["data"]
    approved = env.api.submit_approval(
        org_id="org-demo", actor_id="founder", target_type="work_item",
        target_id=str(task_id), target_revision=payload["target_revision"],
        payload=payload["payload"], actions=["work.execute"], grantee_id=actor)
    assert approved.get("ok") is True, approved
    started = env.api.start_run(task_id, actor_id=actor,
                                output_root=str(env.tmp / f"out-{task_id}"))
    assert started.get("ok") is True, started
    assert started["data"]["state"] == "queued"
    return task_id, started["data"]["operation_id"]


# ── 核心端到端：根入口 → Worker → 受限 Agent → R07 验收 → done ────────────────


def test_root_entry_end_to_end_worker_execution_and_done(env):
    task_id, run_id = _create_approved_run(env)

    # 排队成功后数字员工确实开始工作：Worker 认领（租约+generation）+ 预算预留
    result = env.worker.process_run(run_id)
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result
    assert result["heartbeat_ticks"] >= 1          # R05：转换前显式续租生效
    assert set(result["artifacts"]) == {"diff", "test_report"}
    assert result["budget"]["category"] == "work.run"
    assert result["budget"]["reserved_tokens"] > 0
    # N04：envelope 是托管额度不是消费账——真实 usage 在调用行（唯一 call_id）
    assert result["budget"]["used_tokens"] == 0
    assert result["budget"]["call_usage_tokens"] == 240

    # DE-29：每 Run 独立沙箱根；真实工具写出的源文件在工作区内
    with env.application.transaction() as session:
        run_row = session.get(ExecutionRun, run_id)
        assert run_row.state == "waiting_acceptance" and run_row.attempts == 1
        workspace = Path(json.loads(run_row.checkpoint_json)["frozen"]["workspace"])
        assert workspace.is_relative_to(env.tmp / f"out-{task_id}")
        receipts = list(session.scalars(select(ArtifactReceipt).where(
            ArtifactReceipt.run_id == run_id)))
        assert len(receipts) == 1                  # R07：test_report 检查回执已绑定
        assert receipts[0].checker.startswith("restricted_agent")
        assert len(receipts[0].summary_sha256) == 64
    assert (workspace / ".agent-env" / "home").is_dir()
    assert (workspace / ".agent-env" / "sessions").is_dir()
    assert (workspace / "src" / "gateway_timeout.py").is_file()
    assert (workspace / "artifacts" / "diff.patch").is_file()

    # 独立验收（reviewer ≠ executor）→ done 唯一入口
    requested = env.api.request_acceptance(task_id, run_id=run_id, requester="ai-support")
    assert requested.get("ok") is True, requested
    reviewed = env.api.review_acceptance(task_id, run_id=run_id, reviewer="founder",
                                         verdict="pass")
    assert reviewed.get("ok") is True and reviewed["data"]["state"] == "done", reviewed

    # 预算账目逐笔可对：每次模型调用一笔预留+结算；Run 级托管额度只承担
    # escrow 职能，不再聚合第二份实际消费（N04：240 只记一次，不双记 480）
    with env.application.transaction() as session:
        rows = list(session.scalars(select(BudgetReservation).where(
            BudgetReservation.run_id == run_id)))
        calls = [row for row in rows if row.category == "model_call"]
        runs = [row for row in rows if row.category == "work.run"]
        assert len(calls) == env.provider.calls == 2
        assert all(row.status == "settled" and row.used_tokens > 0 and row.ok
                   for row in calls)
        assert sum(row.used_tokens for row in calls) == 240   # 150 + 90 真实 usage
        assert all(row.call_id and row.usage_status == "recorded"
                   and row.intent_status == "recorded" and row.outcome == "ok"
                   for row in calls)
        assert len(runs) == 1 and runs[0].status == "settled"
        # N04 语义修正（原断言 runs[0].used_tokens == 240 是双记账）：envelope
        # 结算残差为 0，真实 usage 唯一落在调用行；usage_status 区分口径
        assert runs[0].used_tokens == 0 and runs[0].usage_status == "not_applicable"
        summary = budget.usage_summary(session, org_id="org-demo")
        assert summary["used_total"] == sum(row.used_tokens for row in rows) == 240
        assert summary["by_category"] == {"model_call": 240, "work.run": 0}
        assert summary["reserved_open"] == 0 and summary["released"] == 0


# ── 预算网关契约（fail-closed） ───────────────────────────────────────────────


def test_unbudgeted_model_call_refused_fail_closed(env):
    provider = FakeProvider([{"content": "hi", "final": True}])
    # 1) 完全没有预算记录的付费调用：NoBudgetRecord 拒绝执行
    with pytest.raises(NoBudgetRecord):
        provider.complete(context={"brief": {}}, tools=["read_file"])
    assert provider.calls == 0
    # 2) 类别错配同样拒绝：模型调用的记录不能挪给 embedding 用
    record = env.gateway.reserve(org_id="org-demo", space_id="space-support",
                                 principal_id="ai-support", category="embedding",
                                 tokens=50)
    with budget_gateway.record_context(record):
        with pytest.raises(NoBudgetRecord):
            provider.complete(context={}, tools=[])
    env.gateway.release(record)


def test_gateway_settles_with_actual_usage_and_ledger_consistent(env):
    provider = FakeProvider([{"content": "answer", "final": True,
                              "usage_tokens": 137}])
    outcome = env.gateway.call(
        lambda: provider.complete(context={}, tools=[]),
        org_id="org-demo", space_id="space-support", principal_id="ai-support",
        tokens=200, category="model_call")
    assert outcome.ok is True and outcome.settled is True
    assert outcome.used_tokens == 137              # 按真实 usage 结算，非预留额
    with env.application.transaction() as session:
        row = session.get(BudgetReservation, outcome.reservation_id)
        assert row.status == "settled" and row.used_tokens == 137 and row.ok is True
        summary = budget.usage_summary(session, org_id="org-demo")
        assert summary["used_total"] == 137


def test_reservation_insufficient_fails_and_ledger_stays_consistent(env):
    provider = FakeProvider([{"content": "x", "final": True, "usage_tokens": 30}])
    with env.application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo", limit_tokens=40)
    with pytest.raises(budget.BudgetError) as excinfo:
        env.gateway.call(lambda: provider.complete(context={}, tools=[]),
                         org_id="org-demo", space_id="space-support",
                         principal_id="ai-support", tokens=100, category="model_call")
    assert excinfo.value.code == "budget_exhausted"
    assert provider.calls == 0                     # 预留失败：调用不执行
    with env.application.transaction() as session:
        summary = budget.usage_summary(session, org_id="org-demo")
        assert summary["used_total"] == 0          # 被拒预留不落账，账目一致


def test_worker_budget_exhausted_waits_then_executes_after_topup(env):
    with env.application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo", limit_tokens=1)
    task_id, run_id = _create_approved_run(env)
    result = env.worker.process_run(run_id)
    # 预算耗尽是等待不是失败：Run 保持 queued、attempts 不增、无执行副作用
    assert result.get("reason") == "budget_waiting" and result.get("ok") is False, result
    with env.application.transaction() as session:
        run_row = session.get(ExecutionRun, run_id)
        assert run_row.state == "queued" and run_row.attempts == 0
        assert session.query(BudgetReservation).filter_by(run_id=run_id).count() == 0
    assert env.provider.calls == 0
    # 提额后同一 Run 由下一轮 Worker 认领执行成功
    with env.application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo", limit_tokens=100000)
    result = env.worker.process_run(run_id)
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result


# ── R05 心跳续租 / 取消传播 / R06 恢复接管 / 单实例锁 ─────────────────────────


def test_lease_heartbeat_renews_across_controlled_clock(env, monkeypatch):
    class Clock:
        value = real_utcnow()

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value = self.value + timedelta(seconds=seconds)

    clock = Clock()
    monkeypatch.setattr(execution, "utcnow", clock)
    state = {"calls": 0}

    def script(context):
        state["calls"] += 1
        if state["calls"] >= 2:
            clock.advance(300)                 # 执行中途跨过 120s 固定租约
        return list(CODE_SCRIPT)

    env.provider._script = script
    task_id, run_id = _create_approved_run(env)
    result = env.worker.process_run(run_id)
    # 无续租则 waiting_acceptance 转换必然 stale_run_lease；续租生效即成功
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result
    assert result["heartbeat_ticks"] >= 1
    assert state["calls"] == 2


def test_cancelled_task_propagates_to_queued_run(env):
    task_id, run_id = _create_approved_run(env)
    cancelled = env.api.cancel_work_item(task_id, actor="founder", reason="重复请求合并处理")
    assert cancelled.get("ok") is True, cancelled
    result = env.worker.process_run(run_id)
    assert result.get("reason") == "task_cancelled", result
    assert result.get("run_state") == "cancelled"
    assert env.provider.calls == 0              # 取消后不再执行任何付费动作


def test_worker_crash_recovery_takeover(env, monkeypatch):
    class Clock:
        value = real_utcnow()

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value = self.value + timedelta(seconds=seconds)

    clock = Clock()
    monkeypatch.setattr(execution, "utcnow", clock)
    task_id, run_id = _create_approved_run(env)
    # 模拟 Worker 崩溃：认领后不再回来，时钟推进越过固定租约
    with env.application.transaction() as session:
        claimed = execution.claim(session, run_id, worker="w-dead",
                                  validate_context=env.api._authorize_adapter())
        assert claimed is not None
        clock.advance(300)
    assert env.worker.recover_expired() == [run_id]   # R06：过期租约 → 对账恢复
    with env.application.transaction() as session:
        run_row = session.get(ExecutionRun, run_id)
        assert run_row.state == "waiting_recovery" and run_row.generation >= 1
    resumed = env.worker.resume_recovered(run_id)
    assert resumed["ok"] is True and resumed["state"] == "queued"
    result = env.worker.process_run(run_id)
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result


def test_single_active_lock_refuses_second_worker(env):
    holder = env.worker.acquire_single_active()
    assert holder["holder_id"] == "w-e2e"
    other = WorkWorker(env.application.session_factory, env.adapter,
                       env.api._authorize_adapter(),
                       config=WorkerConfig(worker_id="w-other"))
    with pytest.raises(SingleActiveRefused):
        other.acquire_single_active()
    assert env.worker.release_single_active() is True
    other.acquire_single_active()               # 释放后可接管
    other.release_single_active()


def test_not_claimed_releases_budget_reservation(env):
    _task_a, run_a = _create_approved_run(env, title="修复登录超时缺陷A")
    _task_b, run_b = _create_approved_run(env, title="修复登录超时缺陷B")
    with env.application.transaction() as session:
        claimed = execution.claim(session, run_a, worker="w-other",
                                  validate_context=env.api._authorize_adapter())
        assert claimed is not None              # 单活执行位被 A 占住
    result = env.worker.process_run(run_b)
    assert result.get("reason") == "not_claimed" and result.get("ok") is False, result
    assert result["reservation"] == "released"  # 未执行的预留不留免费副作用
    with env.application.transaction() as session:
        row = session.query(BudgetReservation).filter_by(
            run_id=run_b, category="work.run").one()
        assert row.status == "released"


# ── 受限执行：工具白名单与写根 guard 硬约束 ───────────────────────────────────


def test_tool_whitelist_and_write_guard_enforced(env):
    def script(context):
        return [
            {"tool_calls": [
                {"name": "agent", "arguments": {"prompt": "递归创建子代理"}},
                {"name": "write_file", "arguments": {"path": "../escape.txt",
                                                     "content": "越界写入"}},
                {"name": "write_file", "arguments": {"path": "src/gateway_timeout.py",
                                                     "content": "TIMEOUT_SECONDS = 5\n"}},
            ],
             "content": "越权尝试后完成修复", "usage_tokens": 100},
            {"content": "done", "final": True, "usage_tokens": 50},
        ]

    env.provider._script = script
    task_id, run_id = _create_approved_run(env)
    result = env.worker.process_run(run_id)
    # 白名单外工具/越界写被拒但真实修复仍完成（拒绝留痕，不静默吞）
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result
    assert "tool_not_allowed:agent" in env.adapter.last_refusals
    assert "write_file:write_outside_roots" in env.adapter.last_refusals
    assert not (env.tmp / "escape.txt").exists()
    with env.application.transaction() as session:
        run_row = session.get(ExecutionRun, run_id)
        workspace = Path(json.loads(run_row.checkpoint_json)["frozen"]["workspace"])
    assert (workspace / "src" / "gateway_timeout.py").is_file()
