"""N02/N04 Worker 崩溃耐久矩阵：真实子进程（os._exit）在 6 个崩溃点验证
持久提交语义与接管对账，另含预算双记负例、托管互斥与取消并发提交可见性。

审核定位：
- N02/P1：Worker 曾把认领、费用和外部动作包在一个未提交事务里——崩溃后
  痕迹全部回滚（Run 仍 queued、generation=0、无预算行），但副作用已发生。
  修复后：认领（generation/lease_owner）与预算预留（envelope）在适配器执行
  前已持久提交；每次调用以稳定 call_id 在独立短事务提交动作意图与回执；
  崩溃后新连接可见已提交部分、意图可对账，接管进程按回执续跑不盲重放。
- N04/P1：同一模型 usage 曾同时记入 Run 账与调用账（240 双记为 480）。
  修复后：envelope 是托管额度不是第二份消费账，真实 usage 以唯一 call_id
  记一次；调用结算互斥释放父预留对应份额，可用额度不被提前拒绝。

崩溃点矩阵（全部离线、确定性）：
1. claim 提交后          → 已提交认领可见，无副作用，接管续跑
2. 预留提交后（未认领）  → envelope 可见且被接管进程幂等复用（不双预留）
3. 外部调用开始后        → 副作用标记已存在 + 意图 executing 无回执 → 对账保守归集
4. 外部调用完成回执未写  → 同上（provider 已返回但结算未提交）
5. 回执后下一检查点前    → 已回执调用保留、在途意图对账，接管上下文带 reconciliation
6. 验收前（全部落盘后）  → waiting_acceptance 完整可见，不重放，验收可继续
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness.__main__ import main as root_main
from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.governance import budget
from evoharness.governance.budget_gateway import BudgetGateway
from evoharness.governance.models import BudgetReservation
from evoharness.work import execution
from evoharness.work.agent_adapter import FakeProvider, ProviderTurn, RestrictedAgentAdapter
from evoharness.work.models import ArtifactReceipt, ExecutionRun
from evoharness.work.worker import WorkWorker, WorkerConfig
from evoharness.workflows.code_fix import ScriptTestRunner

REPO_ROOT = str(Path(__file__).resolve().parents[1])

CHECK_SCRIPT = ("import pathlib, sys; "
                "sys.exit(0 if pathlib.Path('src/gateway_timeout.py').is_file() else 1)")

CODE_ACCEPTANCE = [{"kind": "code", "criteria": "回归全部通过",
                    "required_artifacts": ["diff", "test_report"]}]

# 子进程剧本：两幕真实工具执行（150 + 90 = 240 真实 usage）
TURN1_WRITE = {"tool_calls": [{"name": "write_file", "arguments": {
    "path": "src/gateway_timeout.py",
    "content": "TIMEOUT_SECONDS = 5\n"}}], "content": "最小修复已写入", "usage_tokens": 150}
TURN3_FINAL = {"content": "修复完成，等待真实门禁", "final": True, "usage_tokens": 90}
TURN2_READ = {"tool_calls": [{"name": "read_file", "arguments": {
    "path": "src/gateway_timeout.py"}}], "content": "复查", "usage_tokens": 70}
# 复审探针 2 场景：三次调用 150+70+20=240 真实 token
TURN3_FINAL20 = {"content": "修复完成，等待真实门禁", "final": True, "usage_tokens": 20}


@pytest.fixture()
def env(tmp_path):
    db_url = "sqlite:///" + (tmp_path / "durability.db").as_posix()
    application = Application.open(db_url)
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    out = io.StringIO()
    code = root_main(["--db", db_url, "--json", "init", "--org-id", "org-demo"], stdout=out)
    assert code == 0, out.getvalue()
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo", limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    gateway = BudgetGateway(application.session_factory)
    provider = FakeProvider([TURN1_WRITE, TURN3_FINAL])
    adapter = RestrictedAgentAdapter(
        provider=provider, gateway=gateway,
        runner=ScriptTestRunner([sys.executable, "-c", CHECK_SCRIPT]))
    worker = WorkWorker(application.session_factory, adapter,
                        api._authorize_adapter(),
                        config=WorkerConfig(worker_id="w-durability"))
    yield SimpleNamespace(application=application, api=api, worker=worker,
                          gateway=gateway, provider=provider, adapter=adapter,
                          tmp=tmp_path, db_url=db_url)
    application.engine.dispose()


def _create_approved_run(env, *, title="耐久矩阵任务"):
    created = env.api.create_work_item(
        tenant="local", owner="founder", title=title, action="fix",
        goal="交付补丁且回归全部通过", acceptance=CODE_ACCEPTANCE,
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
        payload=payload["payload"], actions=["work.execute"], grantee_id="ai-support")
    assert approved.get("ok") is True, approved
    started = env.api.start_run(task_id, actor_id="ai-support",
                                output_root=str(env.tmp / f"out-{task_id}"))
    assert started.get("ok") is True, started
    return task_id, started["data"]["operation_id"]


# ── 子进程脚本（崩溃注入 / 接管续跑） ─────────────────────────────────────────

_CHILD_COMMON = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, r"@REPO@")

from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.governance.budget_gateway import BudgetGateway
from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
from evoharness.work.worker import WorkWorker, WorkerConfig
from evoharness.workflows.code_fix import ScriptTestRunner

CHECKER = [sys.executable, "-c",
           "import pathlib,sys;sys.exit(0 if pathlib.Path('src/gateway_timeout.py').is_file() else 1)"]
SCRIPT = [
    {"tool_calls": [{"name": "write_file", "arguments": {
        "path": "src/gateway_timeout.py", "content": "TIMEOUT_SECONDS = 5\n"}}],
     "content": "minimal fix written", "usage_tokens": 150},
    {"content": "fix complete, awaiting gate", "final": True, "usage_tokens": 90},
]


def build(db):
    app = Application.open(db)
    api = WorkApi(app)
    gateway = BudgetGateway(app.session_factory)
    return app, api, gateway


class CrashProvider(FakeProvider):
    """崩溃注入（点 3/4/5）：真实子进程在 provider 调用边界 os._exit(27)。"""

    def __init__(self, *, mode, marker):
        super().__init__(list(SCRIPT))
        self._mode = mode
        self._marker = Path(marker)

    def complete(self, *, context, tools):
        if self._mode == "turn1_start" and self.calls == 0:
            self._marker.write_text("external call started", encoding="utf-8")
            os._exit(27)
        if self._mode == "turn1_end" and self.calls == 0:
            super().complete(context=context, tools=tools)
            self._marker.write_text("external call done, receipt missing", encoding="utf-8")
            os._exit(27)
        if self._mode == "turn2_entry" and self.calls == 1:
            self._marker.write_text("receipt committed, next checkpoint pending", encoding="utf-8")
            os._exit(27)
        return super().complete(context=context, tools=tools)


class CaptureProvider(FakeProvider):
    """接管执行：记录收到的上下文（父进程断言 reconciliation 在场 = 非盲重放）。"""

    def __init__(self):
        super().__init__(list(SCRIPT))
        self.contexts = []

    def complete(self, *, context, tools):
        self.contexts.append(json.loads(json.dumps(context, default=str)))
        return super().complete(context=context, tools=tools)


def make_worker(app, api, gateway, provider, *, crash_phase=None):
    hook = (lambda name: os._exit(27) if name == crash_phase else None) if crash_phase else None
    adapter = RestrictedAgentAdapter(provider=provider, gateway=gateway,
                                     runner=ScriptTestRunner(CHECKER))
    return WorkWorker(app.session_factory, adapter, api._authorize_adapter(),
                      config=WorkerConfig(worker_id="w-child"),
                      phase_hook=hook)
'''

_CRASH_MAIN = r'''
db, run_id, mode, marker = sys.argv[1:5]
app, api, gateway = build(db)
if mode in {"claimed", "reserved", "envelope_settled"}:
    provider = FakeProvider(list(SCRIPT))
    worker = make_worker(app, api, gateway, provider, crash_phase=mode)
else:
    provider = CrashProvider(mode=mode, marker=marker)
    worker = make_worker(app, api, gateway, provider)
worker.process_run(run_id)
print("CRASH-CHILD-SURVIVED")   # 不应到达；到达说明崩溃点未触发
'''

_TAKEOVER_MAIN = r'''
from datetime import timedelta

from evoharness.work import execution

db, run_id, recover_flag = sys.argv[1], sys.argv[2], sys.argv[3]
app, api, gateway = build(db)
if recover_flag == "recover":
    # 崩溃子进程的租约早已过期：用显式推进的时钟触发恢复扫描（确定性）
    with app.transaction() as session:
        recovered = execution.recover_expired(
            session, now=execution.utcnow() + timedelta(seconds=300))
    assert run_id in recovered, recovered
    recovery_worker = make_worker(app, api, gateway, FakeProvider(list(SCRIPT)))
    resumed = recovery_worker.resume_recovered(run_id)
    assert resumed["ok"] is True and resumed["state"] == "queued", resumed
provider = CaptureProvider()
worker = make_worker(app, api, gateway, provider)
result = worker.process_run(run_id)
print(json.dumps({"result": {k: result.get(k) for k in ("ok", "reason", "run_state")},
                  "contexts": provider.contexts}))
'''


def _write_child(tmp_path: Path, name: str, main: str) -> Path:
    script = tmp_path / name
    script.write_text(_CHILD_COMMON.replace("@REPO@", REPO_ROOT) + main,
                      encoding="utf-8")
    return script


def _run_child(script: Path, *args):
    return subprocess.run(
        [sys.executable, "-I", str(script)] + [str(a) for a in args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def _snapshot(env, run_id: str) -> dict:
    """新连接读取持久状态（崩溃后另一连接所见 = 已提交事实）。"""
    with env.application.transaction() as session:
        run = session.get(ExecutionRun, run_id)
        rows = list(session.scalars(select(BudgetReservation).where(
            BudgetReservation.run_id == run_id)))
        return {"state": run.state, "generation": run.generation,
                "lease_owner": run.lease_owner, "attempts": run.attempts,
                "checkpoint": json.loads(run.checkpoint_json or "{}"),
                "rows": [{"kind": r.kind, "category": r.category, "status": r.status,
                          "intent": r.intent_status, "usage_status": r.usage_status,
                          "used": r.used_tokens, "reserved": r.reserved_tokens,
                          "call_id": r.call_id} for r in rows]}


def _takeover(env, tmp_path, run_id, *, recover: bool) -> dict:
    script = _write_child(tmp_path, "takeover_child.py", _TAKEOVER_MAIN)
    proc = _run_child(script, env.db_url, run_id, "recover" if recover else "norecover")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── 崩溃点 1：claim 提交后 ────────────────────────────────────────────────────

def test_crash_after_claim_commit_durable_then_takeover(env, tmp_path):
    task_id, run_id = _create_approved_run(env)
    marker = env.tmp / "marker-1.txt"
    crash = _write_child(tmp_path, "crash_child.py", _CRASH_MAIN)
    proc = _run_child(crash, env.db_url, run_id, "claimed", marker)
    assert proc.returncode == 27, proc.stderr[-2000:]
    snap = _snapshot(env, run_id)
    # 认领已持久提交：新连接可见 claimed/running + generation + lease_owner
    assert snap["state"] in {"claimed", "running"}
    assert snap["generation"] >= 1 and snap["lease_owner"]
    assert [r for r in snap["rows"] if r["kind"] == "envelope"
            and r["status"] == "reserved"]
    assert not any(r["kind"] == "call" for r in snap["rows"])   # 尚无调用
    assert not marker.exists()                                  # 无副作用痕迹
    # 接管：恢复 → 回执对账 → 重排 → 续跑完成（envelope 复用，不双预留）
    payload = _takeover(env, tmp_path, run_id, recover=True)
    assert payload["result"]["ok"] is True, payload
    assert payload["result"]["run_state"] == "waiting_acceptance"
    assert payload["contexts"][0].get("reconciliation") is not None
    final = _snapshot(env, run_id)
    envelopes = [r for r in final["rows"] if r["kind"] == "envelope"]
    assert len(envelopes) == 1 and envelopes[0]["status"] == "settled"
    calls = [r for r in final["rows"] if r["kind"] == "call"]
    assert sum(r["used"] for r in calls) == 240
    with env.application.transaction() as session:
        assert budget.usage_summary(session, org_id="org-demo")["used_total"] == 240


# ── 崩溃点 2：预留提交后（未认领） ────────────────────────────────────────────

def test_crash_after_reservation_commit_reused_not_duplicated(env, tmp_path):
    task_id, run_id = _create_approved_run(env)
    marker = env.tmp / "marker-2.txt"
    crash = _write_child(tmp_path, "crash_child.py", _CRASH_MAIN)
    proc = _run_child(crash, env.db_url, run_id, "reserved", marker)
    assert proc.returncode == 27, proc.stderr[-2000:]
    snap = _snapshot(env, run_id)
    # 已提交部分可见：envelope 预留落盘；未认领（generation=0、无租约）
    assert snap["state"] == "queued" and snap["generation"] == 0
    assert snap["lease_owner"] == ""
    envelopes = [r for r in snap["rows"] if r["kind"] == "envelope"
                 and r["status"] == "reserved"]
    assert len(envelopes) == 1
    assert not marker.exists()
    # 接管进程幂等复用同一托管额度（不双预留），直接认领执行
    payload = _takeover(env, tmp_path, run_id, recover=False)
    assert payload["result"]["ok"] is True, payload
    final = _snapshot(env, run_id)
    envelopes = [r for r in final["rows"] if r["kind"] == "envelope"]
    assert len(envelopes) == 1 and envelopes[0]["status"] == "settled"
    assert sum(r["used"] for r in final["rows"] if r["kind"] == "call") == 240
    with env.application.transaction() as session:
        assert budget.usage_summary(session, org_id="org-demo")["used_total"] == 240


# ── 崩溃点 3：外部调用开始后（副作用已发生、意图已提交、回执未写） ─────────────

def test_crash_after_external_call_started_reconciles_unknown(env, tmp_path):
    task_id, run_id = _create_approved_run(env)
    marker = env.tmp / "marker-3.txt"
    crash = _write_child(tmp_path, "crash_child.py", _CRASH_MAIN)
    proc = _run_child(crash, env.db_url, run_id, "turn1_start", marker)
    assert proc.returncode == 27, proc.stderr[-2000:]
    snap = _snapshot(env, run_id)
    # 探针 1 语义反转：副作用痕迹存在时，认领与预算预留必须已持久可见
    assert marker.exists()                       # 副作用已发生
    assert snap["state"] in {"claimed", "running"}
    assert snap["generation"] >= 1 and snap["lease_owner"]
    assert any(r["kind"] == "envelope" and r["status"] == "reserved"
               for r in snap["rows"])
    intents = [r for r in snap["rows"] if r["kind"] == "call"]
    # 意图已持久提交可对账；回执未写（未结算）
    assert len(intents) == 1 and intents[0]["status"] == "reserved"
    assert intents[0]["intent"] == "executing" and intents[0]["call_id"]
    # 接管：结果未知按保守归集（不回滚成未发生、不盲重试），续跑完成
    payload = _takeover(env, tmp_path, run_id, recover=True)
    assert payload["result"]["ok"] is True, payload
    reconciliation = payload["contexts"][0]["reconciliation"]
    assert reconciliation["unknown_intents"] == [intents[0]["call_id"]]
    assert reconciliation["completed_calls"] == []
    final = _snapshot(env, run_id)
    calls = [r for r in final["rows"] if r["kind"] == "call"]
    orphan = [r for r in calls if r["call_id"] == intents[0]["call_id"]]
    assert orphan[0]["status"] == "settled" and orphan[0]["usage_status"] == "unknown"
    assert orphan[0]["used"] == orphan[0]["reserved"]   # 未知 ≠ 零：按预留保守归集
    fresh = [r for r in calls if r["call_id"] != intents[0]["call_id"]]
    assert sum(r["used"] for r in fresh) == 240          # 接管执行的正常记账
    with env.application.transaction() as session:
        summary = budget.usage_summary(session, org_id="org-demo")
        assert summary["used_total"] == sum(r["used"] for r in calls)


# ── 崩溃点 4：外部调用完成而回执未写 ─────────────────────────────────────────

def test_crash_after_external_call_done_before_receipt(env, tmp_path):
    task_id, run_id = _create_approved_run(env)
    marker = env.tmp / "marker-4.txt"
    crash = _write_child(tmp_path, "crash_child.py", _CRASH_MAIN)
    proc = _run_child(crash, env.db_url, run_id, "turn1_end", marker)
    assert proc.returncode == 27, proc.stderr[-2000:]
    snap = _snapshot(env, run_id)
    assert marker.exists()
    assert snap["state"] in {"claimed", "running"}
    intents = [r for r in snap["rows"] if r["kind"] == "call"]
    assert len(intents) == 1 and intents[0]["status"] == "reserved"
    assert intents[0]["intent"] == "executing"
    payload = _takeover(env, tmp_path, run_id, recover=True)
    assert payload["result"]["ok"] is True, payload
    reconciliation = payload["contexts"][0]["reconciliation"]
    assert reconciliation["unknown_intents"] == [intents[0]["call_id"]]
    final = _snapshot(env, run_id)
    calls = [r for r in final["rows"] if r["kind"] == "call"]
    orphan = [r for r in calls if r["call_id"] == intents[0]["call_id"]][0]
    assert orphan["status"] == "settled" and orphan["usage_status"] == "unknown"
    with env.application.transaction() as session:
        summary = budget.usage_summary(session, org_id="org-demo")
        assert summary["used_total"] == sum(r["used"] for r in calls)


# ── 崩溃点 5：回执已提交、下一检查点之前 ─────────────────────────────────────

def test_crash_after_receipt_keeps_completed_and_reconciles_pending(env, tmp_path):
    task_id, run_id = _create_approved_run(env)
    marker = env.tmp / "marker-5.txt"
    crash = _write_child(tmp_path, "crash_child.py", _CRASH_MAIN)
    proc = _run_child(crash, env.db_url, run_id, "turn2_entry", marker)
    assert proc.returncode == 27, proc.stderr[-2000:]
    snap = _snapshot(env, run_id)
    assert marker.exists()
    calls = [r for r in snap["rows"] if r["kind"] == "call"]
    # 第一幕已回执（真实 usage 150 已持久记账）；第二幕意图在途无回执
    recorded = [r for r in calls if r["status"] == "settled"]
    pending = [r for r in calls if r["status"] == "reserved"]
    assert len(recorded) == 1 and recorded[0]["used"] == 150
    assert recorded[0]["usage_status"] == "recorded"
    assert len(pending) == 1 and pending[0]["intent"] == "executing"
    payload = _takeover(env, tmp_path, run_id, recover=True)
    assert payload["result"]["ok"] is True, payload
    reconciliation = payload["contexts"][0]["reconciliation"]
    # 接管上下文携带持久对账结论：已完成步骤与未知意图分列（非盲重放）
    assert [c["call_id"] for c in reconciliation["completed_calls"]] == \
        [recorded[0]["call_id"]]
    assert reconciliation["unknown_intents"] == [pending[0]["call_id"]]
    final = _snapshot(env, run_id)
    all_calls = [r for r in final["rows"] if r["kind"] == "call"]
    kept = [r for r in all_calls if r["call_id"] == recorded[0]["call_id"]][0]
    assert kept["used"] == 150 and kept["usage_status"] == "recorded"
    with env.application.transaction() as session:
        summary = budget.usage_summary(session, org_id="org-demo")
        assert summary["used_total"] == sum(r["used"] for r in all_calls)


# ── 崩溃点 6：验收前（worker 全部落盘后） ────────────────────────────────────

def test_crash_before_acceptance_keeps_waiting_acceptance_without_replay(env, tmp_path):
    task_id, run_id = _create_approved_run(env)
    marker = env.tmp / "marker-6.txt"
    crash = _write_child(tmp_path, "crash_child.py", _CRASH_MAIN)
    proc = _run_child(crash, env.db_url, run_id, "envelope_settled", marker)
    assert proc.returncode == 27, proc.stderr[-2000:]
    assert not marker.exists()
    snap = _snapshot(env, run_id)
    # 已提交部分全部可见：waiting_acceptance + 产物回执 + usage 240 记一次
    assert snap["state"] == "waiting_acceptance"
    calls = [r for r in snap["rows"] if r["kind"] == "call"]
    assert sum(r["used"] for r in calls) == 240
    assert all(r["usage_status"] == "recorded" for r in calls)
    envelope = [r for r in snap["rows"] if r["kind"] == "envelope"][0]
    assert envelope["status"] == "settled" and envelope["used"] == 0
    with env.application.transaction() as session:
        assert len(list(session.scalars(select(ArtifactReceipt).where(
            ArtifactReceipt.run_id == run_id)))) == 1
    # 接管 Worker 不盲重放：Run 已在等待验收，重新认领被状态机拒绝
    replay = env.worker.process_run(run_id)
    assert replay.get("reason") == "run_state:waiting_acceptance", replay
    # 验收流程照常继续：waiting_acceptance → done
    requested = env.api.request_acceptance(task_id, run_id=run_id, requester="ai-support")
    assert requested.get("ok") is True, requested
    reviewed = env.api.review_acceptance(task_id, run_id=run_id, reviewer="founder",
                                         verdict="pass")
    assert reviewed.get("ok") is True and reviewed["data"]["state"] == "done", reviewed


# ── N04 负例：240 只记一次 / 托管互斥不提前拒绝 / 零与未知分状态 ───────────────

def test_budget_total_240_recorded_once_not_twice(env):
    env.provider._script = lambda context: [TURN1_WRITE, TURN2_READ, TURN3_FINAL20]
    task_id, run_id = _create_approved_run(env)
    result = env.worker.process_run(run_id)
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result
    # envelope 不聚合第二份实际消费；真实 usage 以唯一 call_id 记在调用行
    assert result["budget"]["used_tokens"] == 0
    assert result["budget"]["call_usage_tokens"] == 240
    with env.application.transaction() as session:
        summary = budget.usage_summary(session, org_id="org-demo")
        assert summary["used_total"] == 240          # 150 + 70 + 20，不是 480
        assert summary["by_category"] == {"model_call": 240, "work.run": 0}
        rows = list(session.scalars(select(BudgetReservation).where(
            BudgetReservation.run_id == run_id)))
        calls = [r for r in rows if r.kind == "call"]
        assert len(calls) == 3
        assert len({r.call_id for r in calls}) == 3   # 每次调用唯一 call_id
        assert all(r.status == "settled" and r.usage_status == "recorded"
                   and r.intent_status == "recorded" and r.outcome == "ok"
                   for r in calls)


def test_escrow_mutex_no_premature_rejection(env):
    # 极紧限额：org 限额 == envelope 预估。修复前 Run 额度与每次调用同时占额，
    # 第一次调用即被 budget_exhausted 拒绝；修复后调用从托管额度互斥划拨。
    with env.application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=1000)
    env.provider._script = lambda context: [TURN1_WRITE, TURN2_READ, TURN3_FINAL20]
    task_id, run_id = _create_approved_run(env)
    result = env.worker.process_run(run_id)
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result
    with env.application.transaction() as session:
        summary = budget.usage_summary(session, org_id="org-demo")
        assert summary["used_total"] == 240          # 真实用量，全程未超限额
        assert summary["reserved_open"] == 0         # escrow 全部释放
        rows = list(session.scalars(select(BudgetReservation).where(
            BudgetReservation.run_id == run_id)))
        envelope = [r for r in rows if r.kind == "envelope"][0]
        assert envelope.status == "settled" and envelope.used_tokens == 0
        calls = [r for r in rows if r.kind == "call"]
        assert sum(r.escrow_tokens for r in calls) > 0   # 确有托管划拨发生


def test_real_zero_usage_distinct_from_unknown(env):
    # 真实零 usage（provider 如实报 0）与未知 usage（调用异常）不用同一个值
    class ZeroUsageProvider:
        kind = "zero-usage"

        def reset(self):
            pass

        def complete(self, *, context, tools):
            return ProviderTurn(tool_calls=[], content="zero", final=True,
                                usage_tokens=0)

    outcome = env.gateway.call(lambda: ZeroUsageProvider().complete(context={}, tools=[]),
                               org_id="org-demo", space_id="space-support",
                               principal_id="ai-support", tokens=500,
                               category="model_call")
    assert outcome.ok is True and outcome.used_tokens == 0
    with env.application.transaction() as session:
        row = session.get(BudgetReservation, outcome.reservation_id)
        assert row.status == "settled" and row.used_tokens == 0
        assert row.usage_status == "zero" and row.outcome == "ok"

    def boom():
        raise RuntimeError("provider exploded mid-call")

    failed = env.gateway.call(boom, org_id="org-demo", space_id="space-support",
                              principal_id="ai-support", tokens=300,
                              category="model_call")
    assert failed.ok is False
    with env.application.transaction() as session:
        row = session.get(BudgetReservation, failed.reservation_id)
        assert row.status == "settled" and row.used_tokens == 300   # 保守归集
        assert row.usage_status == "unknown" and row.outcome == "failed"
        assert row.intent_status == "unknown"


# ── 取消并发提交可见性：worker 在途时管理员取消可提交，下一调用前生效 ──────────

def test_admin_cancel_commits_during_execution_and_next_call_refuses(env):
    started = threading.Event()
    proceed = threading.Event()

    def script(context):
        started.set()
        assert proceed.wait(timeout=60)
        return [dict(TURN1_WRITE), dict(TURN3_FINAL)]

    env.provider._script = script
    task_id, run_id = _create_approved_run(env)
    results: dict = {}

    def drive():
        results["worker"] = env.worker.process_run(run_id)

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    assert started.wait(timeout=60)
    # N02 核心：worker 执行在途（数据库无长写事务）→ 管理员取消可并发提交
    cancelled = env.api.cancel_work_item(task_id, actor="founder", reason="紧急停止")
    assert cancelled.get("ok") is True, cancelled
    proceed.set()
    thread.join(timeout=120)
    result = results["worker"]
    # 已发生的第一幕 usage 照实记账；第二幕付费动作前重验取消即拒绝
    assert result.get("ok") is False and result.get("run_state") == "cancelled", result
    assert env.provider.calls == 1
    with env.application.transaction() as session:
        run = session.get(ExecutionRun, run_id)
        assert run.state == "cancelled"
        rows = list(session.scalars(select(BudgetReservation).where(
            BudgetReservation.run_id == run_id)))
        calls = [r for r in rows if r.kind == "call"]
        assert len(calls) == 1 and calls[0].used_tokens == 150
        assert calls[0].usage_status == "recorded" and calls[0].outcome == "ok"
        envelope = [r for r in rows if r.kind == "envelope"][0]
        assert envelope.status == "settled" and envelope.used_tokens == 0
