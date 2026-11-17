"""N03 工具进程隔离回归矩阵：shell/检查器经独立受限容器执行，容器边界是唯一防线。

复审探针 2（shell_boundary）修复后必须失效：
- 目录逃逸负例：经正式 ToolExecutor 的 run_shell 在工作目录之外写文件——
  容器内只有工作区读写挂载，宿主侧 outside 不得被创建；
- 宿主环境哨兵不透传：容器 env 白名单只含指向容器内部的非秘密字段；
- Docker 不可用 → waiting_environment（fail-closed，不算成功）；
- 检查器（ScriptTestRunner 类）经同一隔离通道；
- 纵深拒绝清单（docker socket / /proc/*/environ）；
- BP-01 隔离 manifest 落盘；
- 与 N02 取消重验组合：执行中撤权/取消 → 下一工具调用被拒并留痕。

真实 Docker 集成测试（逃逸/环境/网络/只读挂载）以 docker 可用为门控
（pytest skipif），容器本身 --network none，全部离线确定性。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness.work import executor, tool_isolation
from evoharness.work.agent_adapter import (
    AgentAdapterError,
    AgentSandbox,
    FakeProvider,
    RestrictedAgentAdapter,
    ToolExecutor,
)
from evoharness.work.tool_isolation import (
    WAITING_ENVIRONMENT,
    DockerShellRunner,
    IsolatedChecker,
)
from evoharness.governance.budget_gateway import BudgetGateway


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return DockerShellRunner().available()


DOCKER_AVAILABLE = _docker_ready()
docker_required = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker unavailable")


def _tools(workspace: Path, allowed=frozenset({"run_shell"}), **kwargs) -> ToolExecutor:
    workspace.mkdir(parents=True, exist_ok=True)
    return ToolExecutor(workspace, frozenset(allowed),
                        lambda p: executor.guard_path([str(workspace)], p),
                        AgentSandbox(workspace), **kwargs)


def _manifest_of(workspace: Path) -> dict:
    path = workspace / ".agent-env" / "isolation-manifest.json"
    assert path.is_file(), "BP-01 隔离 manifest 未落盘"
    return json.loads(path.read_text(encoding="utf-8"))


# ── 纵深拒绝清单（离线确定性，不触 docker） ───────────────────────────────────


@pytest.mark.parametrize("argv,rule", [
    (["python3", "-c", "open('/var/run/docker.sock')"], "docker_socket_access"),
    (["python3", "-c", "open('/proc/self/environ').read()"], "proc_environ_access"),
    (["python3", "-c", "open('/proc/1/environ').read()"], "proc_environ_access"),
    (["python3", "-c", "shutil.copy('/home/u/.ssh/id_rsa', 'x')"], "host_ssh_material"),
])
def test_shell_deny_rules_refuse_before_any_execution(tmp_path, argv, rule):
    tools = _tools(tmp_path / "ws")
    outcome = tools.run("run_shell", {"argv": argv})
    assert outcome.ok is False
    assert outcome.detail == f"shell_deny_rule:{rule}"
    assert f"run_shell:shell_deny_rule:{rule}" in tools.refusals


def test_docker_unavailable_returns_waiting_environment_fail_closed(tmp_path):
    """Docker 不可用 → run_shell 返回 waiting_environment（fail-closed，不算成功）。"""

    class UnavailableRunner:
        kind = "docker"

        def available(self) -> bool:
            return False

        def run(self, workdir, argv, **kwargs):
            return tool_isolation.ShellOutcome(ok=False, detail=WAITING_ENVIRONMENT,
                                               waiting_environment=True)

    tools = _tools(tmp_path / "ws", shell_runner=UnavailableRunner())
    outcome = tools.run("run_shell", {"argv": ["python3", "-c", "print('hi')"]})
    assert outcome.ok is False
    assert outcome.detail == WAITING_ENVIRONMENT
    assert "run_shell:waiting_environment" in tools.refusals


def test_run_shell_requires_isolation_runner_no_host_fallback(tmp_path, monkeypatch):
    """缺省装配也无宿主兜底：runner 走 DockerShellRunner；不可达即 waiting_environment。"""

    class NoDockerRunner(DockerShellRunner):
        def available(self) -> bool:
            return False

    tools = _tools(tmp_path / "ws", shell_runner=NoDockerRunner())
    outcome = tools.run("run_shell", {"argv": ["python3", "-c", "print('x')"]})
    assert outcome.ok is False and outcome.detail == WAITING_ENVIRONMENT


def test_isolation_manifest_dumped_with_mounts_and_fields(tmp_path):
    """BP-01 产物：挂载路径与允许字段形成 manifest 落盘。"""
    runner = DockerShellRunner()
    argv, manifest = runner.build_argv(tmp_path / "ws", ["python3", "-c", "print(1)"])
    assert manifest.network == "none"
    assert manifest.readonly_rootfs is True
    assert manifest.host_env_passthrough is False
    assert manifest.docker_socket_mounted is False
    assert manifest.user and manifest.user != "root"
    assert any(m["target"] == "/workspace" and m["mode"] == "rw" for m in manifest.mounts)
    assert manifest.env_allowlist == tool_isolation.ENV_ALLOWLIST
    dumped = manifest.dump(tmp_path / "m.json")
    loaded = json.loads(dumped.read_text(encoding="utf-8"))
    assert loaded["kind"] == "tool_isolation_manifest" and loaded["bp"] == "BP-01"
    assert loaded["network"] == "none"
    # argv 中的镜像位置正确（flags → mounts/env → image → command）
    assert argv.count("python:3.13-slim") == 1
    assert argv[-3:] == ["python3", "-c", "print(1)"]


def test_container_argv_maps_host_interpreter():
    mapped = tool_isolation.container_argv(
        [r"D:\venv\python.exe", "-c", "print(1)"])
    assert mapped[0] == "python3"
    assert tool_isolation.container_argv(["python3", "x"]) == ["python3", "x"]


# ── 真实 Docker 集成（docker 可用时才跑；容器 --network none 全程离线） ────────


@docker_required
def test_directory_escape_rejected_outside_never_created(tmp_path):
    """复审探针 2 场景：经正式 ToolExecutor 的 run_shell 在工作目录之外写文件。"""
    workspace = tmp_path / "shell-work"
    outside = tmp_path / "outside-workspace.txt"
    monkey_sentinel = "synthetic-env-value"
    os.environ["EVO_AUDIT_SENTINEL"] = monkey_sentinel
    try:
        tools = _tools(workspace)
        code = ("from pathlib import Path;import os;Path("
                + repr(str(outside)) + ").write_text(os.environ.get("
                "'EVO_AUDIT_SENTINEL','missing'))")
        result = tools.run("run_shell", {"argv": [sys.executable, "-c", code]})
        # 容器边界：宿主侧 outside 不存在；工具结论不伪装成功为越界写入
        assert not outside.exists()
        assert not (tmp_path / "outside-workspace.txt").exists()
        # 哨兵环境值不进入容器输出（missing = 未透传）
        assert monkey_sentinel not in (result.detail or "")
    finally:
        os.environ.pop("EVO_AUDIT_SENTINEL", None)


@docker_required
def test_workspace_writable_and_host_env_not_passed_through(tmp_path, monkeypatch):
    """容器内可写工作目录（宿主可见）；宿主环境哨兵不透传。"""
    workspace = tmp_path / "ws"
    monkeypatch.setenv("EVO_AUDIT_SENTINEL_ISOLATION", "host-secret-9137")
    tools = _tools(workspace)
    result = tools.run("run_shell", {"argv": ["python3", "-c",
                                              "open('made-in-container.txt','w').write('ok');"
                                              "import os;print(os.environ.get("
                                              "'EVO_AUDIT_SENTINEL_ISOLATION','MISSING'))"]})
    assert result.ok is True, result.detail
    assert (workspace / "made-in-container.txt").read_text(encoding="utf-8") == "ok"
    assert "host-secret-9137" not in result.detail
    assert "MISSING" in result.detail
    manifest = _manifest_of(workspace)
    assert manifest["host_env_passthrough"] is False
    assert manifest["network"] == "none"


@docker_required
def test_readonly_input_mount_rejects_writes(tmp_path):
    """输入源码/原测试只读挂载：容器内写 /inputs 失败、读成功。"""
    workspace = tmp_path / "ws"
    inputs = tmp_path / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "source.txt").write_text("input-content", encoding="utf-8")
    runner = DockerShellRunner()
    outcome = runner.run(workspace, ["python3", "-c",
                                     "print(open('/inputs/0/source.txt').read())\n"
                                     "try:\n"
                                     "    open('/inputs/0/evil.txt','w').write('x')\n"
                                     "    print('WRITE-SUCCEEDED')\n"
                                     "except OSError:\n"
                                     "    print('readonly-blocked')"],
                          readonly_mounts=(inputs,),
                          manifest_path=workspace / ".agent-env" / "isolation-manifest.json")
    assert outcome.ok is True, outcome.detail
    assert "input-content" in outcome.detail
    assert "readonly-blocked" in outcome.detail
    assert "WRITE-SUCCEEDED" not in outcome.detail
    assert not (inputs / "evil.txt").exists()


@docker_required
def test_container_has_no_network(tmp_path):
    """--network none：容器内尝试出网失败。"""
    workspace = tmp_path / "ws"
    runner = DockerShellRunner(timeout_seconds=60.0)
    outcome = runner.run(workspace, ["python3", "-c",
                                     "import socket;\n"
                                     "try:\n"
                                     "    socket.create_connection(('93.184.216.34', 80), timeout=3)\n"
                                     "    print('NETWORK-REACHED')\n"
                                     "except OSError as e:\n"
                                     "    print('network-blocked:', type(e).__name__)"],
                          manifest_path=workspace / ".agent-env" / "isolation-manifest.json")
    assert outcome.ok is True, outcome.detail
    assert "NETWORK-REACHED" not in outcome.detail
    assert "network-blocked" in outcome.detail


@docker_required
def test_checker_runs_through_isolated_channel(tmp_path):
    """检查器（ScriptTestRunner 类）走隔离通道：容器内裁决，宿主解释器映射。"""
    from evoharness.workflows.code_fix import ScriptTestRunner

    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    runner = IsolatedChecker(ScriptTestRunner(
        [sys.executable, "-c",
         "import pathlib, sys; sys.exit(0 if pathlib.Path('src/deliverable.py').is_file() else 1)"]))
    # 交付文件缺失 → 检查失败可见（exit 1）
    report = runner.run(workspace, "gate")
    assert report.ok is False and report.exit_code == 1
    # 交付文件存在 → 检查通过；manifest 落盘（BP-01）
    (workspace / "src" / "deliverable.py").write_text("X=1\n", encoding="utf-8")
    report = runner.run(workspace, "gate")
    assert report.ok is True and report.exit_code == 0
    manifest = _manifest_of(workspace)
    assert manifest["argv"][0] == "python3"


@docker_required
def test_adapter_routes_checker_through_isolation_channel(tmp_path):
    """适配器装配：ScriptTestRunner 缺省经隔离容器通道执行（回执留隔离通道事实）。"""
    import io

    from evoharness.__main__ import main as root_main
    from evoharness.api import WorkApi
    from evoharness.application import Application
    from evoharness.governance import budget
    from evoharness.work.worker import WorkWorker, WorkerConfig
    from evoharness.workflows.code_fix import ScriptTestRunner

    tmp = tmp_path
    app = Application.open("sqlite:///" + (tmp / "iso.db").as_posix())
    api = WorkApi(app, artifacts_dir=tmp / "artifacts")
    out = io.StringIO()
    code = root_main(["--db", "sqlite:///" + (tmp / "iso.db").as_posix(),
                      "--json", "init", "--org-id", "org-demo"], stdout=out)
    assert code == 0, out.getvalue()
    with app.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    created = api.create_work_item(
        tenant="local", owner="founder", title="隔离通道检查", action="fix",
        goal="交付补丁且回归全部通过",
        acceptance=[{"kind": "code", "criteria": "回归全部通过",
                     "required_artifacts": ["diff", "test_report"]}],
        workflow_kind="delegated_delivery", org_id="org-demo",
        space_id="space-support")
    task_id = created["data"]["id"]
    assert api.transition_work_item(task_id, to_state="queued", actor="founder")["ok"]
    payload = api.run_request_payload(task_id)["data"]
    assert api.submit_approval(
        org_id="org-demo", actor_id="founder", target_type="work_item",
        target_id=str(task_id), target_revision=payload["target_revision"],
        payload=payload["payload"], actions=["work.execute"],
        grantee_id="ai-support")["ok"]
    started = api.start_run(task_id, actor_id="ai-support",
                            output_root=str(tmp / "out"))
    run_id = started["data"]["operation_id"]
    provider = FakeProvider([
        {"tool_calls": [{"name": "write_file", "arguments": {
            "path": "src/gateway_timeout.py", "content": "TIMEOUT_SECONDS = 5\n"}}],
         "content": "写入", "usage_tokens": 150},
        {"content": "完成", "final": True, "usage_tokens": 90}])
    adapter = RestrictedAgentAdapter(
        provider=provider, gateway=BudgetGateway(app.session_factory),
        runner=ScriptTestRunner([sys.executable, "-c",
                                 "import pathlib, sys; "
                                 "sys.exit(0 if pathlib.Path('src/gateway_timeout.py')"
                                 ".is_file() else 1)"]))
    worker = WorkWorker(app.session_factory, adapter, api._authorize_adapter(),
                        config=WorkerConfig(worker_id="w-iso"))
    try:
        result = worker.process_run(run_id)
        assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
            result
        receipts = adapter.last_receipts
        assert "docker-isolated" in receipts["test_report"]["checker"]
        workspace = Path(result["workspace"])
        manifest = _manifest_of(workspace)
        assert manifest["network"] == "none"
    finally:
        app.engine.dispose()


# ── 与 N02 取消重验的组合：执行中取消 → 下一工具调用被拒 ─────────────────────


def test_cancel_between_turns_refuses_next_tool_call(tmp_path):
    """N02×N03：执行中取消（并发短事务提交）→ 下一工具调用被拒并留痕。"""
    from evoharness.work.models import ExecutionRun
    from evoharness.work.worker import WorkWorker, WorkerConfig
    from evoharness.workflows.code_fix import ScriptTestRunner

    from evoharness.api import WorkApi
    from evoharness.application import Application
    from evoharness.governance import budget

    tmp = tmp_path
    app = Application.open("sqlite:///" + (tmp / "cancel.db").as_posix())
    api = WorkApi(app, artifacts_dir=tmp / "artifacts")
    out = io.StringIO()
    from evoharness.__main__ import main as root_main
    assert root_main(["--db", "sqlite:///" + (tmp / "cancel.db").as_posix(),
                      "--json", "init", "--org-id", "org-demo"],
                     stdout=out) == 0
    with app.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    created = api.create_work_item(
        tenant="local", owner="founder", title="执行中取消", action="fix", goal="g",
        acceptance=[{"kind": "code", "criteria": "c"}],
        workflow_kind="delegated_delivery", org_id="org-demo",
        space_id="space-support")
    task_id = created["data"]["id"]
    api.transition_work_item(task_id, to_state="queued", actor="founder")
    payload = api.run_request_payload(task_id)["data"]
    api.submit_approval(org_id="org-demo", actor_id="founder",
                        target_type="work_item", target_id=str(task_id),
                        target_revision=payload["target_revision"],
                        payload=payload["payload"], actions=["work.execute"],
                        grantee_id="ai-support")
    started = api.start_run(task_id, actor_id="ai-support",
                            output_root=str(tmp / "out"))
    run_id = started["data"]["operation_id"]

    state = {"calls": 0, "cancelled": False}

    TURN1_WRITE = {"tool_calls": [{"name": "write_file", "arguments": {
        "path": "src/a.py", "content": "A=1\n"}}],
        "content": "第一幕", "usage_tokens": 60}
    TURN2_READ = {"tool_calls": [{"name": "read_file", "arguments": {
        "path": "src/a.py"}}], "content": "第二幕", "usage_tokens": 60}
    TURN3_FINAL = {"content": "done", "final": True, "usage_tokens": 20}

    def script(context):
        # FakeProvider 可调用剧本约定：每次返回完整剧本，按调用序号取幕
        state["calls"] += 1
        if state["calls"] >= 2 and not state["cancelled"]:
            state["cancelled"] = True
            # 第 2 幕模型调用返回后、工具执行前并发提交取消（N02 短事务通道）
            cancelled = api.cancel_work_item(task_id, actor="founder", reason="synthetic")
            assert cancelled.get("ok") is True, cancelled
        return [TURN1_WRITE, TURN2_READ, TURN3_FINAL]

    adapter = RestrictedAgentAdapter(provider=FakeProvider(script),
                                     gateway=BudgetGateway(app.session_factory),
                                     runner=ScriptTestRunner([sys.executable, "-c",
                                                              "print('checker')"]))
    worker = WorkWorker(app.session_factory, adapter, api._authorize_adapter(),
                        config=WorkerConfig(worker_id="w-cancel"))
    try:
        result = worker.process_run(run_id)
        # 第二幕工具调用被拒（取消重验在工具层生效）并留痕
        assert "read_file:run_cancelled" in adapter.last_refusals, adapter.last_refusals
        # 第三幕模型调用同样被拒 → Run 受控终止（任务已取消），失败可见
        assert result.get("ok") is False, result
        assert result.get("run_state") == "cancelled", result
        with app.transaction() as session:
            run_row = session.get(ExecutionRun, run_id)
            assert run_row.state == "cancelled"
            # 原因留痕：Run 行携带真实触发原因（执行中取消）
            assert "run_cancelled" in (run_row.error or "")
        # 账目：第三幕模型调用被拒于预留之前（无第三笔 model_call 预留）
        from sqlalchemy import select as _select
        from evoharness.governance.models import BudgetReservation
        with app.transaction() as session:
            call_rows = list(session.scalars(_select(BudgetReservation).where(
                BudgetReservation.run_id == run_id,
                BudgetReservation.category == "model_call")))
            assert len(call_rows) == 2
            assert all(row.status == "settled" for row in call_rows)
    finally:
        app.engine.dispose()


def test_nonroot_linux_runner_matches_workspace_owner(monkeypatch, tmp_path):
    monkeypatch.delenv("EVOHARNESS_TOOL_USER", raising=False)
    monkeypatch.setattr(tool_isolation.os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(tool_isolation.os, "getgid", lambda: 4321, raising=False)
    runner = DockerShellRunner()
    assert runner.user == "1234:4321"
    argv, manifest = runner.build_argv(tmp_path, ["python3", "-c", "print(1)"])
    assert argv[argv.index("--user") + 1] == "1234:4321"
    assert manifest.user == "1234:4321"


def test_explicit_isolation_user_matches_manifest_and_command(tmp_path):
    runner = DockerShellRunner(user="1234:4321")
    argv, manifest = runner.build_argv(tmp_path, ["python3", "-c", "print(1)"])
    assert argv[argv.index("--user") + 1] == manifest.user == "1234:4321"


def test_linux_isolation_rejects_windows_container_daemon(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="windows\n")
    monkeypatch.setattr(tool_isolation.subprocess, "run", run)
    assert DockerShellRunner().available() is False
    assert len(calls) == 1


def test_linux_isolation_requires_the_configured_linux_image(monkeypatch):
    from types import SimpleNamespace
    replies = iter([SimpleNamespace(returncode=0, stdout="linux\n"),
                    SimpleNamespace(returncode=1, stdout="")])
    monkeypatch.setattr(tool_isolation.subprocess, "run", lambda *a, **kw: next(replies))
    assert DockerShellRunner().available() is False
