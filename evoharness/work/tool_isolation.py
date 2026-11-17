"""工具进程隔离执行器（N03/BP-01）：shell 与检查器经独立受限容器执行。

审核定位（N03/P1）：``agent_adapter.ToolExecutor._tool_run_shell`` 直接以模型给的
argv 启动宿主程序——cwd 只设初始目录不限制访问（可越目录读写宿主文件系统），
child_env 复制全部宿主环境再覆盖 HOME（宿主凭据随子进程外泄）。命令字符串解析
提供不了通用隔离——容器边界是唯一防线：

- **受限容器**：``docker run`` 一次性容器执行工具 argv——``--network none``（无网）、
  rootfs 只读（``--read-only``，容器内临时写走 tmpfs /tmp）、工作目录（Run 输出根）
  以显式读写卷挂载到 /workspace、额外输入源码/原测试只读挂载、非特权用户
  （``--user``）+ ``--cap-drop ALL`` + ``no-new-privileges``、**不透传宿主环境**
  （env 白名单只含指向容器内部的非秘密字段）、不挂载 docker socket、不挂载
  宿主密钥/SSH 目录。挂载路径与允许字段形成 manifest 落盘（BP-01 产物）。
- **fail-closed**：Docker 不可用/守护进程未运行 → ``waiting_environment``（不算
  成功、不留宿主副作用），保留不需要 shell 的工作流（qa 等）。
- **纵深拒绝清单**（明确规则，非通用隔离）：argv 命中 docker socket 路径、
  ``/proc/*/environ``（环境注入面）等一律拒绝并留痕。
- **解释器映射**：宿主解释器绝对路径（如 Windows venv python）在容器内不存在；
  argv[0] 为 python 类解释器时映射为容器内 ``python3``（检查脚本本身保持
  解释器无关的纯 Python）。
- **检查器同通道**：``IsolatedChecker`` 实现与 ``ScriptTestRunner`` 相同的
  TestRunner 协议（run(workdir, phase) -> TestReport），检查 argv 经同一容器
  通道执行——检查器同样越不过工作区边界、拿不到宿主环境。

离线确定性：容器本身 ``--network none``；docker 不可用时全部退化到
waiting_environment，不静默放行到宿主。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from evoharness.clock import utcnow

# fail-closed 语义值：Docker 不可用/守护进程未运行（不算成功，不落宿主副作用）
WAITING_ENVIRONMENT = "waiting_environment"

# 容器内固定布局
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_HOME = "/tmp/isolated-home"
# env 白名单：只允许指向容器内部/非秘密的字段（宿主环境一律不透传）
ENV_ALLOWLIST = {
    "HOME": CONTAINER_HOME,
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONIOENCODING": "utf-8",
}

# 默认镜像与运行身份（可用 EVOHARNESS_TOOL_IMAGE / EVOHARNESS_TOOL_USER 覆盖）
DEFAULT_IMAGE = "python:3.13-slim"
DEFAULT_USER = "10001:10001"

# 纵深拒绝清单（明确规则；通用隔离由容器边界承担，不靠解析命令字符串）
_DENY_PATTERNS = (
    (re.compile(r"docker(?:\.sock|\s*socket)", re.IGNORECASE), "docker_socket_access"),
    (re.compile(r"/proc/(?:\d+|self)/environ"), "proc_environ_access"),
    (re.compile(r"/proc/(?:\d+|self)/mem\b"), "proc_mem_access"),
    (re.compile(r"\.ssh/[A-Za-z_]*id_|authorized_keys"), "host_ssh_material"),
)


def deny_reason(argv: Iterable[object]) -> str:
    """argv 纵深拒绝清单：命中返回规则名，否则空串（容器边界仍是唯一防线）。"""
    for item in argv:
        text = str(item or "")
        for pattern, rule in _DENY_PATTERNS:
            if pattern.search(text):
                return rule
    return ""


def container_argv(argv: list[str]) -> list[str]:
    """宿主 argv → 容器 argv：python 类解释器绝对路径映射为容器内 python3。

    检查/工具脚本本身是解释器无关的纯 Python；宿主 venv 解释器路径在容器内
    不存在，不映射则所有合法调用都变成 runner_error。
    """
    mapped = list(argv)
    if mapped:
        name = Path(str(mapped[0]).replace("\\", "/")).name.lower()
        if name.startswith("python"):
            mapped[0] = "python3"
    return mapped


@dataclass
class ShellOutcome:
    """一次隔离容器执行的结论：exit code / 输出 / waiting_environment 语义。"""

    ok: bool
    detail: str
    waiting_environment: bool = False
    exit_code: int | None = None


@dataclass
class IsolationManifest:
    """BP-01 产物：一次隔离执行的挂载路径与允许字段快照（落盘可审计）。"""

    runner: str
    image: str
    network: str
    readonly_rootfs: bool
    user: str
    cap_drop: list[str]
    security_opts: list[str]
    env_allowlist: dict
    host_env_passthrough: bool
    docker_socket_mounted: bool
    mounts: list[dict]
    tmpfs: list[str]
    deny_rules: list[str]
    argv: list[str]
    invoked_at: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": "tool_isolation_manifest",
            "bp": "BP-01",
            "runner": self.runner,
            "image": self.image,
            "network": self.network,
            "readonly_rootfs": self.readonly_rootfs,
            "user": self.user,
            "cap_drop": list(self.cap_drop),
            "security_opts": list(self.security_opts),
            "env_allowlist": dict(self.env_allowlist),
            "host_env_passthrough": self.host_env_passthrough,
            "docker_socket_mounted": self.docker_socket_mounted,
            "mounts": [dict(m) for m in self.mounts],
            "tmpfs": list(self.tmpfs),
            "deny_rules": list(self.deny_rules),
            "argv": list(self.argv),
            "invoked_at": self.invoked_at,
        }

    def dump(self, path: Path) -> Path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError:
            pass  # manifest 落盘失败不改变执行结论（账实仍以容器结果为准）
        return path


def _default_user() -> str:
    configured = str(os.environ.get("EVOHARNESS_TOOL_USER") or "").strip()
    if configured:
        return configured
    if hasattr(os, "getuid") and os.getuid() != 0:
        return f"{os.getuid()}:{os.getgid()}"
    return DEFAULT_USER


def _docker_base_argv(user: str = "") -> list[str]:
    user = user or _default_user()
    return ["docker", "run", "--rm", "--name", f"evo-tool-{uuid.uuid4().hex[:12]}",
            "--network", "none", "--read-only", "--user", user,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--pull", "never"]


class DockerShellRunner:
    """受限容器 shell 通道：唯一防线是容器边界（不做命令字符串通用隔离）。

    - ``available()``：docker CLI 可用且守护进程可达；不可达时调用方按
      waiting_environment fail-closed；
    - ``run()``：工作目录读写挂载 /workspace，额外输入只读挂载 /inputs/<n>；
      宿主环境零透传；超时由宿主侧 docker kill 兜底（容器 --rm 自清理）。
    """

    kind = "docker"

    def __init__(self, *, image: str = "", user: str = "",
                 timeout_seconds: float = 30.0):
        self.image = image or DEFAULT_IMAGE
        self.user = user or _default_user()
        self.timeout_seconds = float(timeout_seconds)

    # ── 可用性 ──────────────────────────────────────────────────────────────

    def available(self) -> bool:
        try:
            proc = subprocess.run(
                ["docker", "info", "--format", "{{.OSType}}"],
                capture_output=True, text=True, timeout=15,
                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode != 0 or proc.stdout.strip() != "linux":
                return False
            image = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Os}}", self.image],
                capture_output=True, text=True, timeout=15,
                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return image.returncode == 0 and image.stdout.strip() == "linux"
        except (OSError, subprocess.SubprocessError):
            return False

    # ── 执行 ────────────────────────────────────────────────────────────────

    def build_argv(self, workdir: Path, argv: list[str], *,
                   readonly_mounts: tuple[Path, ...] = ()) -> tuple[list[str], IsolationManifest]:
        base = _docker_base_argv(self.user)
        mounts = [{"source": Path(workdir).resolve().as_posix(),
                   "target": CONTAINER_WORKSPACE, "mode": "rw"}]
        base += ["--mount",
                 f"type=bind,src={Path(workdir).resolve()},dst={CONTAINER_WORKSPACE}"]
        for index, source in enumerate(readonly_mounts):
            base += ["--mount",
                     f"type=bind,src={Path(source).resolve()},dst=/inputs/{index},readonly"]
            mounts.append({"source": Path(source).resolve().as_posix(),
                           "target": f"/inputs/{index}", "mode": "ro"})
        for key, value in ENV_ALLOWLIST.items():
            base += ["-e", f"{key}={value}"]
        base += ["-w", CONTAINER_WORKSPACE]
        manifest = IsolationManifest(
            runner=self.kind, image=self.image, network="none",
            readonly_rootfs=True, user=self.user,
            cap_drop=["ALL"], security_opts=["no-new-privileges"],
            env_allowlist=dict(ENV_ALLOWLIST), host_env_passthrough=False,
            docker_socket_mounted=False, mounts=mounts,
            tmpfs=["/tmp:rw,size=64m"], deny_rules=[rule for _p, rule in _DENY_PATTERNS],
            argv=container_argv(argv),
            invoked_at=utcnow().isoformat())
        # docker run [flags] image [command]：镜像之后才是容器命令
        base += [self.image]
        base += container_argv(argv)
        return base, manifest

    def run(self, workdir: Path, argv: list[str], *,
            readonly_mounts: tuple[Path, ...] = (),
            manifest_path: Path | None = None) -> ShellOutcome:
        if not self.available():
            return ShellOutcome(ok=False, detail=WAITING_ENVIRONMENT,
                                waiting_environment=True)
        base, manifest = self.build_argv(workdir, argv, readonly_mounts=readonly_mounts)
        if manifest_path is not None:
            manifest.dump(manifest_path)
        try:
            proc = subprocess.run(
                base, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=self.timeout_seconds, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            self._kill_container(base)
            return ShellOutcome(ok=False, detail="timeout", exit_code=None)
        except OSError as exc:
            return ShellOutcome(ok=False, detail=f"runner_error: {exc}")
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        detail = f"exit={proc.returncode}\n{output}".strip()
        if proc.returncode != 0 and _is_daemon_error(proc.stderr or ""):
            return ShellOutcome(ok=False, detail=WAITING_ENVIRONMENT,
                                waiting_environment=True)
        return ShellOutcome(ok=proc.returncode == 0, detail=detail,
                            exit_code=proc.returncode)

    @staticmethod
    def _kill_container(base_argv: list[str]) -> None:
        try:
            name = base_argv[base_argv.index("--name") + 1]
            subprocess.run(["docker", "kill", name], capture_output=True,
                           timeout=15, shell=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (ValueError, OSError, subprocess.SubprocessError):
            pass  # 兜底清理失败不影响 waiting/timeout 结论


def _is_daemon_error(stderr: str) -> bool:
    lowered = (stderr or "").lower()
    return ("cannot connect to the docker daemon" in lowered
            or "error response from daemon" in lowered
            or "is the docker daemon running" in lowered)


class IsolatedChecker:
    """检查器的隔离通道：TestRunner 协议同构（ScriptTestRunner 语义），容器内执行。

    - 包装 ``ScriptTestRunner``（读取其 argv/timeout/max_chars）或直接给 argv；
    - docker 不可用 → TestReport(ok=False, output=waiting_environment...)：
      检查不算通过、交付失败可见（fail-closed），不静默放行为宿主进程。
    """

    kind = "docker-isolated"

    def __init__(self, runner, *, shell_runner: DockerShellRunner | None = None):
        self._argv = list(getattr(runner, "argv") or [])
        self._timeout_seconds = float(getattr(runner, "timeout_seconds", 120.0) or 120.0)
        self._max_chars = int(getattr(runner, "max_chars", 4000) or 4000)
        self.shell_runner = shell_runner or DockerShellRunner(
            timeout_seconds=self._timeout_seconds)

    def run(self, workdir: Path, phase: str) -> "TestReport":  # noqa: F821 (协议同构)
        from evoharness.workflows.code_fix import TestReport

        workdir = Path(workdir)
        manifest_path = workdir / ".agent-env" / "isolation-manifest.json"
        outcome = self.shell_runner.run(workdir, self._argv,
                                        manifest_path=manifest_path)
        if outcome.waiting_environment:
            return TestReport(ok=False, exit_code=None,
                              output=f"{WAITING_ENVIRONMENT}: docker unavailable "
                                     f"(checker not executed on host)")
        output = (outcome.detail or "(no output)")[: self._max_chars]
        if outcome.exit_code is None:
            return TestReport(ok=False, timed_out=True, exit_code=None, output=output)
        return TestReport(ok=bool(outcome.ok), exit_code=outcome.exit_code,
                          output=output)


__all__ = [
    "WAITING_ENVIRONMENT", "ENV_ALLOWLIST", "DEFAULT_IMAGE", "DEFAULT_USER",
    "ShellOutcome", "IsolationManifest", "DockerShellRunner", "IsolatedChecker",
    "deny_reason", "container_argv",
]
