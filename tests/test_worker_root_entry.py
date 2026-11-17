"""根入口 worker 命令回归（BP-02）：``python -m evoharness worker`` 成为真实命令。

- ``--once``   真实子进程消费一个 queued Run 后退出：Run 到 waiting_acceptance、
   产物落盘、预算逐笔结算（fake profile：离线确定性，不触网）；
- ``--health`` 队列年龄/租约/最近错误（只读分项事实）；
- ``--once`` 空队列 → queue_empty；
- ``--check-argv`` 非法 JSON → 明确拒绝。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness.__main__ import main as root_main
from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.governance import budget
from evoharness.governance.models import BudgetReservation
from evoharness.work.models import ExecutionRun

REPO_ROOT = str(Path(__file__).resolve().parents[1])
PYTHON = sys.executable


@pytest.fixture()
def env(tmp_path):
    application = Application.open("sqlite:///" + (tmp_path / "root.db").as_posix())
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    out = io.StringIO()
    code = root_main(["--db", "sqlite:///" + (tmp_path / "root.db").as_posix(),
                      "--json", "init", "--org-id", "org-demo"], stdout=out)
    assert code == 0, out.getvalue()
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    yield _Env(application, api, tmp_path)
    application.engine.dispose()


class _Env:
    def __init__(self, application, api, tmp):
        self.application = application
        self.api = api
        self.tmp = tmp
        self.db_url = application._db_url if hasattr(application, "_db_url") else \
            "sqlite:///" + (tmp / "root.db").as_posix()

    def queue_run(self, *, title="根入口 worker 任务") -> str:
        created = self.api.create_work_item(
            tenant="local", owner="founder", title=title, action="fix",
            goal="交付补丁且回归全部通过",
            acceptance=[{"kind": "code", "criteria": "回归全部通过",
                         "required_artifacts": ["diff", "test_report"]}],
            workflow_kind="delegated_delivery", org_id="org-demo",
            space_id="space-support")
        task_id = created["data"]["id"]
        assert self.api.transition_work_item(task_id, to_state="queued",
                                             actor="founder")["ok"]
        payload = self.api.run_request_payload(task_id)["data"]
        assert self.api.submit_approval(
            org_id="org-demo", actor_id="founder", target_type="work_item",
            target_id=str(task_id), target_revision=payload["target_revision"],
            payload=payload["payload"], actions=["work.execute"],
            grantee_id="ai-support")["ok"]
        started = self.api.start_run(task_id, actor_id="ai-support",
                                     output_root=str(self.tmp / f"out-{task_id}"))
        assert started.get("ok") is True, started
        return started["data"]["operation_id"]


def _run_worker_cli(db_url: str, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """真实子进程跑根入口 worker 命令（不是进程内函数调用）。"""
    env = {**os.environ,
           "PYTHONPATH": REPO_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
           "PYTHONIOENCODING": "utf-8",
           "FEISHU_TOKEN_SECRET": os.environ.get("FEISHU_TOKEN_SECRET",
                                                 "offline-test-only-token-secret")}
    return subprocess.run(
        [PYTHON, "-m", "evoharness", "--db", db_url, "--json", "worker", *args],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        cwd=REPO_ROOT, env=env, shell=False)


def test_worker_once_subprocess_completes_queued_run(tmp_path):
    """真实子进程 ``worker --once --profile fake``：消费 queued Run 后退出。"""
    application = Application.open("sqlite:///" + (tmp_path / "root.db").as_posix())
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    out = io.StringIO()
    db_url = "sqlite:///" + (tmp_path / "root.db").as_posix()
    assert root_main(["--db", db_url, "--json", "init", "--org-id", "org-demo"],
                     stdout=out) == 0
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    env = _Env(application, api, tmp_path)
    run_id = env.queue_run()
    application.engine.dispose()

    proc = _run_worker_cli(db_url, "--once", "--profile", "fake")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    envelope = json.loads(proc.stdout)
    assert envelope.get("ok") is True, envelope
    assert envelope.get("mode") == "once"
    assert envelope.get("run_state") == "waiting_acceptance", envelope
    assert envelope.get("run_id") == run_id
    assert sorted(envelope.get("artifacts") or []) == ["diff", "test_report"]

    # 独立连接核验持久状态：Run 终态、产物文件、预算逐笔结算
    verify = Application.open(db_url)
    try:
        with verify.transaction() as session:
            run_row = session.get(ExecutionRun, run_id)
            assert run_row.state == "waiting_acceptance"
            import json as _json
            workspace = Path(_json.loads(run_row.checkpoint_json)["frozen"]["workspace"])
            assert (workspace / "src" / "gateway_timeout.py").is_file()
            assert (workspace / "artifacts" / "diff.patch").is_file()
            assert (workspace / "artifacts" / "test_report.txt").is_file()
            # BP-01：隔离 manifest 落盘（检查器走隔离容器通道）
            manifest = workspace / ".agent-env" / "isolation-manifest.json"
            assert manifest.is_file()
            loaded = _json.loads(manifest.read_text(encoding="utf-8"))
            assert loaded["network"] == "none" and loaded["host_env_passthrough"] is False
            calls = [row for row in session.scalars(select(BudgetReservation).where(
                BudgetReservation.run_id == run_id,
                BudgetReservation.category == "model_call"))]
            assert len(calls) == 3
            assert sum(row.used_tokens for row in calls) == 240   # 150 + 70 + 20
            assert all(row.call_id and row.status == "settled" for row in calls)
    finally:
        verify.engine.dispose()


def test_worker_once_empty_queue_is_ok(tmp_path):
    db_url = "sqlite:///" + (tmp_path / "root.db").as_posix()
    application = Application.open(db_url)
    out = io.StringIO()
    assert root_main(["--db", db_url, "--json", "init", "--org-id", "org-demo"],
                     stdout=out) == 0
    application.engine.dispose()
    proc = _run_worker_cli(db_url, "--once", "--profile", "fake")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    envelope = json.loads(proc.stdout)
    assert envelope.get("ok") is True and envelope.get("reason") == "queue_empty"


def test_worker_health_reports_queue_and_leases(tmp_path):
    db_url = "sqlite:///" + (tmp_path / "root.db").as_posix()
    application = Application.open(db_url)
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    out = io.StringIO()
    assert root_main(["--db", db_url, "--json", "init", "--org-id", "org-demo"],
                     stdout=out) == 0
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    env = _Env(application, api, tmp_path)
    run_id = env.queue_run()
    # 只读观测：队列深度/最老排队年龄/租约/最近错误
    proc = _run_worker_cli(db_url, "--health")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    health = json.loads(proc.stdout)
    assert health.get("ok") is True
    assert health["queue"]["depth"] == 1
    assert health["queue"]["oldest_queued_run_id"] == run_id
    assert health["queue"]["oldest_queued_age_seconds"] >= 0
    assert health["leases"] == [] and health["recent_errors"] == []
    application.engine.dispose()


def test_worker_rejects_invalid_check_argv(tmp_path):
    db_url = "sqlite:///" + (tmp_path / "root.db").as_posix()
    application = Application.open(db_url)
    out = io.StringIO()
    assert root_main(["--db", db_url, "--json", "init", "--org-id", "org-demo"],
                     stdout=out) == 0
    application.engine.dispose()
    proc = _run_worker_cli(db_url, "--once", "--profile", "live",
                           "--check-argv", "not-json")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    envelope = json.loads(proc.stdout)
    assert envelope.get("ok") is False
    assert envelope.get("reason_code") == "check_argv_invalid_json"


def test_worker_daemon_graceful_stop_processes_queue(tmp_path):
    """daemon 常驻：轮询消费 queued Run；stop_event 置位优雅停机。"""
    import threading

    from evoharness.governance.budget_gateway import BudgetGateway
    from evoharness.work import worker_cli

    application = Application.open("sqlite:///" + (tmp_path / "root.db").as_posix())
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    out = io.StringIO()
    assert root_main(["--db", "sqlite:///" + (tmp_path / "root.db").as_posix(),
                      "--json", "init", "--org-id", "org-demo"], stdout=out) == 0
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    env = _Env(application, api, tmp_path)
    run_id = env.queue_run()

    stop = threading.Event()
    outcome: dict = {}

    def _daemon():
        # 非 主线程：signal.signal 抛 ValueError → worker_cli 捕获，不装信号处理器
        outcome["result"] = worker_cli.worker_daemon(
            application, api, profile="fake", poll_interval_seconds=0.2,
            stop_event=stop, install_signal_handlers=False)

    thread = threading.Thread(target=_daemon, daemon=True)
    thread.start()
    deadline = 30.0
    import time
    waited = 0.0
    while waited < deadline:
        with application.transaction() as session:
            row = session.get(ExecutionRun, run_id)
            if row.state == "waiting_acceptance":
                break
        time.sleep(0.2)
        waited += 0.2
    stop.set()
    thread.join(timeout=15)
    assert not thread.is_alive(), "daemon 未在 stop_event 后优雅退出"
    assert outcome["result"]["reason"] == "stopped_gracefully"
    with application.transaction() as session:
        assert session.get(ExecutionRun, run_id).state == "waiting_acceptance"
    application.engine.dispose()


def test_worker_daemon_processes_two_rounds_then_stops(tmp_path):
    """DE-08：daemon 至少两轮实际消费（不是只输出 started），随后优雅停机。"""
    import threading
    import time

    from evoharness.work import worker_cli

    application = Application.open("sqlite:///" + (tmp_path / "root.db").as_posix())
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    out = io.StringIO()
    assert root_main(["--db", "sqlite:///" + (tmp_path / "root.db").as_posix(),
                      "--json", "init", "--org-id", "org-demo"], stdout=out) == 0
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    env = _Env(application, api, tmp_path)
    run_ids = [env.queue_run(title=f"两轮任务 {index}") for index in (1, 2)]

    stop = threading.Event()
    outcome: dict = {}

    def _daemon():
        outcome["result"] = worker_cli.worker_daemon(
            application, api, profile="fake", poll_interval_seconds=0.2,
            stop_event=stop, install_signal_handlers=False)

    thread = threading.Thread(target=_daemon, daemon=True)
    thread.start()
    deadline = 60.0
    waited = 0.0
    while waited < deadline:
        with application.transaction() as session:
            states = [session.get(ExecutionRun, run_id).state for run_id in run_ids]
        if all(state == "waiting_acceptance" for state in states):
            break
        time.sleep(0.2)
        waited += 0.2
    stop.set()
    thread.join(timeout=15)
    assert not thread.is_alive(), "daemon 未在 stop_event 后优雅退出"
    assert outcome["result"]["reason"] == "stopped_gracefully"
    with application.transaction() as session:
        states = [session.get(ExecutionRun, run_id).state for run_id in run_ids]
    assert states == ["waiting_acceptance", "waiting_acceptance"]
    application.engine.dispose()
