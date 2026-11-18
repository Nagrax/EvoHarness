"""根入口 worker 命令的装配层（N05/BP-02）：``python -m evoharness worker``。

BP-02 明确本轮实现根入口：queued Run 的执行侧由此成为真实命令——

- ``--once``   消费一个 queued Run 后退出（单实例锁内认领→执行→释放）；
- ``--daemon`` 常驻轮询（``--poll-interval`` 可配，SIGINT/SIGTERM 优雅停机）；
- ``--health`` 队列年龄/租约/最近错误（分项事实，只读）。

profile 口径（不新建另一套模型设置）：

- ``live``  委派现有 ``agents.Agent`` 主线（``RestrictedAgentAdapter`` 原生通道），
  模型凭据复用 agents.main 的双协议配置解析（APIKEY/API、OPENAI_*、ANTHROPIC_*，
  MODEL 选型）；无可用凭据 → ``model_not_configured``（fail-closed，不静默降级）；
- ``fake``  离线回归：``FakeProvider`` 条件式剧本 + 确定性检查，不触网、不消耗
  费用（根入口启动测试/演示环境用）。

shell 工具与检查器一律走 ``tool_isolation`` 隔离容器通道（N03）。
"""
from __future__ import annotations

import json
import signal
import threading
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from evoharness.clock import utcnow
from evoharness.governance.budget_gateway import BudgetGateway
from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
from evoharness.work.proposals import CoordinationProposalSink
from evoharness.work.tool_isolation import IsolatedChecker
from evoharness.work.worker import WorkWorker, WorkerConfig
from evoharness.workflows.code_fix import ScriptTestRunner

# fake profile 的默认检查：确认工作区交付文件存在（确定性、离线、容器内可执行）
FAKE_CHECK_SCRIPT = ("import pathlib, sys; "
                     "sys.exit(0 if pathlib.Path('src/gateway_timeout.py').is_file() else 1)")

# fake profile 的默认剧本：写文件 → 读回核对 → 按读到的内容收尾（条件执行，
# 与 N05 反馈通道同一语义：第 3 幕依赖第 2 幕工具结果）
FAKE_SCRIPT_TURNS = [
    {"tool_calls": [{"name": "write_file", "arguments": {
        "path": "src/gateway_timeout.py",
        "content": "TIMEOUT_SECONDS = 5  # offline fake delivery\n"}}],
     "content": "写入交付文件", "usage_tokens": 150},
    {"tool_calls": [{"name": "read_file", "arguments": {
        "path": "src/gateway_timeout.py"}}],
     "content": "读回核对", "usage_tokens": 70},
    {"content": "offline fake delivery complete", "final": True, "usage_tokens": 20},
]


def build_adapter(application, *, profile: str, check_argv: list[str] | None = None,
                  max_turns: int = 32) -> RestrictedAgentAdapter:
    """按 profile 装配生产适配器（Worker 执行侧唯一装配点）。"""
    gateway = BudgetGateway(application.session_factory)
    # DE-10：模型侧协调提案写入器（只落事件，不改状态/权限）
    sink = CoordinationProposalSink(application.session_factory)
    if profile == "fake":
        provider = FakeProvider(list(FAKE_SCRIPT_TURNS))
        runner = ScriptTestRunner([_python_executable(), "-c", FAKE_CHECK_SCRIPT])
        return RestrictedAgentAdapter(provider=provider, gateway=gateway,
                                      runner=runner, checker="root_worker.fake",
                                      max_turns=max_turns, proposal_sink=sink)
    # live：委派 agents.Agent 主线（provider 不注入 → 原生通道 + 网关感知端口）；
    # 检查器走隔离容器通道；模型配置缺失在 execute 期 model_not_configured 暴露。
    argv = list(check_argv or [_python_executable(), "-m", "pytest", "-q"])
    if check_argv is None:
        from evoharness.work.checker import TrustedPytestRunner
        runner = TrustedPytestRunner(timeout_seconds=600.0)
    else:
        runner = ScriptTestRunner(argv, timeout_seconds=600.0)
    return RestrictedAgentAdapter(gateway=gateway, runner=runner,
                                  checker="root_worker.live", max_turns=max_turns,
                                  proposal_sink=sink)


def _python_executable() -> str:
    import sys
    return sys.executable


def build_worker(application, api, *, profile: str, worker_id: str = "",
                 poll_interval_seconds: float = 2.0,
                 check_argv: list[str] | None = None) -> WorkWorker:
    config = WorkerConfig(
        worker_id=worker_id or f"root-worker-{utcnow().strftime('%H%M%S')}",
        poll_interval_seconds=max(0.05, float(poll_interval_seconds)))
    adapter = build_adapter(application, profile=profile, check_argv=check_argv)
    return WorkWorker(application.session_factory, adapter,
                      api._authorize_adapter(), config=config)


# ── --once ───────────────────────────────────────────────────────────────────

def worker_once(application, api, *, profile: str, worker_id: str = "",
                check_argv: list[str] | None = None) -> dict:
    """消费一个 queued Run 后退出（认领→执行→结算→释放单实例锁）。"""
    worker = build_worker(application, api, profile=profile, worker_id=worker_id,
                          check_argv=check_argv)
    worker.acquire_single_active()
    try:
        queued = worker.claimable_run_ids(limit=1)
        if not queued:
            return {"ok": True, "mode": "once", "reason": "queue_empty",
                    "processed": []}
        result = worker.process_run(queued[0])
        result["mode"] = "once"
        result["processed"] = [queued[0]]
        return result
    finally:
        worker.release_single_active()


# ── --daemon ─────────────────────────────────────────────────────────────────

def worker_daemon(application, api, *, profile: str, worker_id: str = "",
                  poll_interval_seconds: float = 2.0,
                  check_argv: list[str] | None = None,
                  stop_event: threading.Event | None = None,
                  install_signal_handlers: bool = True) -> dict:
    """常驻轮询；SIGINT/SIGTERM 置位 stop_event 优雅停机（当前轮完成后退出）。"""
    worker = build_worker(application, api, profile=profile, worker_id=worker_id,
                          poll_interval_seconds=poll_interval_seconds,
                          check_argv=check_argv)
    stop = stop_event or threading.Event()

    def _handle(signum, frame):  # noqa: ARG001 — 信号签名固定
        stop.set()

    if install_signal_handlers:
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _handle)
            except (ValueError, OSError):
                pass  # 非主线程/无信号权限：仍可用注入 stop_event 停机
    reason = worker.run_forever(stop_event=stop)
    return {"ok": True, "mode": "daemon", "reason": reason,
            "worker_id": worker.worker_id}


# ── --health ─────────────────────────────────────────────────────────────────

def worker_health(application) -> dict:
    """队列年龄/租约/最近错误（只读分项事实）。"""
    from evoharness.work.models import ExecutionRun

    now = utcnow()
    with application.transaction() as session:
        queued = list(session.scalars(select(ExecutionRun).where(
            ExecutionRun.state == "queued").order_by(ExecutionRun.created_at)))
        leased = list(session.scalars(select(ExecutionRun).where(
            ExecutionRun.state.in_(["claimed", "running"]))))
        failed = list(session.scalars(select(ExecutionRun).where(
            ExecutionRun.state == "failed").order_by(
            ExecutionRun.updated_at.desc()).limit(5)))
        waiting_recovery = session.scalar(
            select(ExecutionRun.id).where(ExecutionRun.state == "waiting_recovery").limit(1))
        oldest_age = None
        if queued:
            oldest_age = max(0.0, (now - queued[0].created_at).total_seconds())
        return {"ok": True, "mode": "health",
                "queue": {"depth": len(queued),
                          "oldest_queued_age_seconds": oldest_age,
                          "oldest_queued_run_id": queued[0].id if queued else ""},
                "leases": [{"run_id": row.id, "state": row.state,
                            "lease_owner": row.lease_owner,
                            "generation": int(row.generation or 0),
                            "lease_until": row.lease_until.isoformat()
                            if row.lease_until else ""}
                           for row in leased],
                "waiting_recovery": bool(waiting_recovery),
                "recent_errors": [{"run_id": row.id, "at": row.updated_at.isoformat(),
                                   "error": (row.error or "")[:300]}
                                  for row in failed]}


__all__ = ["build_adapter", "build_worker", "worker_once", "worker_daemon",
           "worker_health", "FAKE_SCRIPT_TURNS", "FAKE_CHECK_SCRIPT"]
