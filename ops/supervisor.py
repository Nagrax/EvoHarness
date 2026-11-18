"""单活服务主管：启动前检查、中断恢复、优雅停机（DE-23）。

职责边界
--------
- 主管只做"看护 + 恢复 + 收尾"，不实现业务；业务语义全部复用
  `evoharness/work/execution.py`（Run 租约/恢复）与 `evoharness/outbox.py`
  （回执未知/租约恢复）。
- 单活约束见 `ops/single_active.py`：第二写实例启动被明确拒绝并给原因。
- 停机口径（与指南 §10.1 一致）：子进程终止后统一对账——
  在途发送停在 `delivery_unknown` 等对账，不盲重试；
  被中断的 Run 停在 `waiting_recovery`，重启/人工 reconcile 后才可续跑。

停机信号（跨平台口径，诚实声明）
--------------------------------
- POSIX：主管进程安装 SIGTERM/SIGINT 处理，走同一优雅停机路径。
- Windows：进程没有可投递的协作式 SIGTERM，主管额外支持**停机请求文件**
  （`ops/supervisor.py stop` 生成，主管轮询到即优雅停机）；
  两者最终都执行同一个 `stop()`：终止子进程 → 等待退出 → 在途副作用对账收尾。
- 子进程内的"发一半"网络调用无法由主管撤回：一律按回执未知对账，不伪装成功。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from evoharness.clock import utcnow
from evoharness.migrations import MigrationContext, migrate
from evoharness.models import Base

from ops.single_active import SingleActiveGuard, SingleActiveRefused

REPO = Path(__file__).resolve().parents[1]
STOP_REQUEST_FILE = "supervisor-stop.request"
DEFAULT_LEASE_KEY = "single_active:writer"


# ---------------------------------------------------------------------------
# 启动前检查
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _feishu_settings():
    """复用飞书应用的 Settings 校验（非法模式/时间窗在这里暴露）。

    app 包导入时会自动把产品根插进 sys.path，失败如实返回原因。
    """
    feishu_root = REPO / "feishu-daily-agent"
    if str(feishu_root) not in sys.path:
        sys.path.insert(0, str(feishu_root))
    try:
        from app.core.config import get_settings
    except Exception as exc:  # noqa: BLE001 — 依赖缺失要可见
        return None, f"{type(exc).__name__}: {exc}"
    try:
        return get_settings(), ""
    except Exception as exc:  # noqa: BLE001 — 配置非法要可见
        return None, f"{type(exc).__name__}: {exc}"


def preflight(
    database_url: str,
    *,
    token_key_path: Path | None = None,
    check_settings: bool = True,
    migration_backup_dir: str | Path | None = None,
) -> dict:
    """启动前检查：数据库可达 + 迁移可重入 + token 密钥来源 + 配置合法。

    返回 `{ok, checks:[{name, ok, detail}]}`；任何一项失败 ok=False。
    """
    checks: list[CheckResult] = []

    # 1) 数据库可达 + 迁移（幂等、先备份）
    try:
        from sqlalchemy import create_engine

        engine = create_engine(database_url, future=True)
        try:
            Base.metadata.create_all(bind=engine)
            report = migrate(engine, backup_dir=migration_backup_dir)
            checks.append(CheckResult(
                "database_migrate", True,
                f"schema {report.get('fromVersion')}->{report.get('toVersion')}"
                f" changes={report.get('changes')} backup={bool(report.get('backup'))}",
            ))
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001
        checks.append(CheckResult("database_migrate", False, f"{type(exc).__name__}: {exc}"))

    # 2) token 密钥来源：环境变量或已存在的本地密钥文件
    secret = (os.environ.get("FEISHU_TOKEN_SECRET") or "").strip()
    key_path = Path(token_key_path) if token_key_path else (
        REPO / "feishu-daily-agent" / "data" / ".token_key"
    )
    if secret:
        checks.append(CheckResult("token_secret", True, "FEISHU_TOKEN_SECRET 已配置（部署提供）"))
    elif key_path.is_file():
        checks.append(CheckResult("token_secret", True, f"本地密钥文件存在：{key_path}"))
    else:
        checks.append(CheckResult(
            "token_secret", False,
            "缺少 FEISHU_TOKEN_SECRET 且无 data/.token_key；生产部署必须显式配置密钥",
        ))

    # 3) 配置校验（能导入 app 时才检查；导入失败本身也是可见故障）
    if check_settings:
        settings, error = _feishu_settings()
        if settings is None:
            checks.append(CheckResult("settings_valid", False, error or "settings_unavailable"))
        else:
            try:
                checks.append(CheckResult(
                    "settings_valid", True,
                    f"mode={settings.deployment_mode} tenant={settings.tenant_id}"
                    f" daily={settings.daily_report_time}({settings.daily_report_timezone})",
                ))
            except Exception as exc:  # noqa: BLE001
                checks.append(CheckResult("settings_valid", False, f"{type(exc).__name__}: {exc}"))

    return {"ok": all(c.ok for c in checks),
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks]}


# ---------------------------------------------------------------------------
# 重启恢复（接管被中断的 Run 与 outbox 在途租约）
# ---------------------------------------------------------------------------

def recover_interrupted(session_factory, *, policy=None, now: datetime | None = None) -> dict:
    """启动/接管时的统一对账：outbox 过期租约 + Run 过期租约。

    - outbox `sending` 且租约过期 → offline 模式标 `delivery_unknown`（回执未知，
      等人工/网关对账），非 offline 且在幂等窗内回队列重试（DE-14 语义）。
    - Run `claimed/running` 且租约过期 → `waiting_recovery`，generation+1，
      必须先 reconcile 副作用再 resume（DE-11 语义，不盲重放）。
    """
    from evoharness import outbox as outbox_module
    from evoharness.work import execution

    if policy is None:
        settings, error = _feishu_settings()
        if settings is None:
            raise ValueError(f"policy_required: {error or 'settings_unavailable'}")
        policy = settings

    session = session_factory()
    try:
        recovered_outbox = outbox_module.recover_expired_leases(session, policy=policy, now=now)
        recovered_runs = execution.recover_expired(session, now=now)
        session.commit()
        return {
            "outboxLeasesRecovered": recovered_outbox,
            "runsMovedToRecovery": recovered_runs,
        }
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 主管
# ---------------------------------------------------------------------------

def _default_spawner(command: list[str], cwd: Path, env: dict | None):
    return subprocess.Popen(
        command, cwd=str(cwd), env=env,
        # POSIX 上 CTRL_BREAK 语义不可用；统一由主管做停机收尾。
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )


def default_drain(session_factory, *, policy, now: datetime | None = None) -> dict:
    """停机收尾：对账在途副作用（离线模拟语义，绝不盲重试）。"""
    return recover_interrupted(session_factory, policy=policy, now=now)


class Supervisor:
    """单活主管：acquire 单活锁 → 启动子服务 → 停机时终止并对账收尾。

    `spawner`/`drain`/`clock` 可注入：离线演练不真正拉起网络服务，
    也不断言真实信号投递（Windows 无协作式 SIGTERM，见模块 docstring）。
    """

    def __init__(
        self,
        *,
        command: list[str],
        cwd: Path,
        database_url: str,
        lock_path: Path,
        session_factory,
        policy=None,
        lease_key: str = DEFAULT_LEASE_KEY,
        lease_seconds: int = 90,
        grace_seconds: float = 30.0,
        env: dict | None = None,
        clock: Callable[[], datetime] = utcnow,
        spawner: Callable[[list[str], Path, dict | None], object] | None = None,
        drain: Callable[..., dict] | None = None,
        stop_dir: Path | None = None,
    ):
        self.command = command
        self.cwd = Path(cwd)
        self.database_url = database_url
        self.session_factory = session_factory
        self.policy = policy
        self.lease_key = lease_key
        self.grace_seconds = grace_seconds
        self.env = env
        self.clock = clock
        self._spawner = spawner or _default_spawner
        self._drain = drain or default_drain
        self.stop_dir = Path(stop_dir) if stop_dir else self.cwd
        self.guard = SingleActiveGuard(
            lock_path=lock_path,
            lease_key=lease_key,
            session_factory=session_factory,
            lease_seconds=lease_seconds,
            clock=clock,
            holder={"role": "supervisor"},
        )
        self.child = None
        self.started_at: datetime | None = None
        self.stopped_at: datetime | None = None

    # -- 生命周期 -----------------------------------------------------------
    def start(self, *, preflight_report: dict | None = None) -> dict:
        report = preflight_report
        if report is None:
            report = preflight(self.database_url)
        if not report["ok"]:
            failed = [c for c in report["checks"] if not c["ok"]]
            raise RuntimeError(
                "preflight_failed: " + "; ".join(f"{c['name']}={c['detail']}" for c in failed)
            )
        self.guard.acquire()  # 第二实例在这里被拒，SingleActiveRefused 带原因上抛
        self.child = self._spawner(self.command, self.cwd, self.env)
        self.started_at = self.clock()
        return {"ok": bool(self.child), "pid": getattr(self.child, "pid", None),
                "holder": self.guard.holder, "preflight": report}

    def request_stop(self, *, reason: str = "operator") -> dict:
        """跨平台停机请求：写停机请求文件（主管轮询到即优雅停机）。"""
        self.stop_dir.mkdir(parents=True, exist_ok=True)
        path = self.stop_dir / STOP_REQUEST_FILE
        path.write_text(json.dumps({"reason": reason, "at": self.clock().isoformat()},
                                   ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "request": str(path)}

    def stop_requested(self) -> bool:
        return (self.stop_dir / STOP_REQUEST_FILE).is_file()

    def stop(self, *, reason: str = "operator", wait_seconds: float | None = None) -> dict:
        """优雅停机：终止子进程 → 等待退出 → 在途副作用对账 → 释放单活锁。"""
        detail: dict = {"reason": reason, "childTerminated": False, "drain": None}
        if self.child is not None and getattr(self.child, "poll", lambda: None)() is None:
            self._terminate_child()
            detail["childTerminated"] = True
            try:
                self.child.wait(timeout=wait_seconds if wait_seconds is not None
                                else self.grace_seconds)
            except Exception:  # noqa: BLE001 — 超时也继续收尾（子进程被强杀）
                pass
        self._clear_stop_request()
        policy = self.policy
        if policy is None:
            settings, error = _feishu_settings()
            if settings is None:
                raise ValueError(f"policy_required: {error or 'settings_unavailable'}")
            policy = settings
        detail["drain"] = self._drain(self.session_factory, policy=policy)
        self.guard.release()
        self.stopped_at = self.clock()
        detail["ok"] = True
        return detail

    def _terminate_child(self) -> None:
        child = self.child
        try:
            if os.name == "nt" and hasattr(child, "send_signal"):
                # CREATE_NEW_PROCESS_GROUP 下 CTRL_BREAK 可投递给子进程组；
                # 子进程未处理时仍由主管 drain 兜底对账。
                try:
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                    child.wait(timeout=2)
                    return
                except Exception:  # noqa: BLE001
                    pass
            if os.name != "nt" and hasattr(child, "send_signal"):
                child.send_signal(signal.SIGTERM)
            else:
                child.terminate()
        except Exception:  # noqa: BLE001 — 已退出的子进程
            pass

    def _clear_stop_request(self) -> None:
        try:
            (self.stop_dir / STOP_REQUEST_FILE).unlink(missing_ok=True)
        except OSError:
            pass

    def status(self) -> dict:
        child_state = None
        if self.child is not None:
            child_state = "running" if getattr(self.child, "poll", lambda: 0)() is None else "exited"
        return {
            "child": child_state,
            "holder": self.guard.holder or None,
            "dbLease": self.guard.db_lease_status(),
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "stopRequested": self.stop_requested(),
        }

    # -- 常驻循环 -----------------------------------------------------------
    def supervise(self, *, poll_interval: float = 1.0, max_restarts: int = 5,
                  restart_backoff_seconds: float = 5.0,
                  should_continue: Callable[[], bool] | None = None) -> dict:
        """常在线主循环：停机请求/子进程退出 → 恢复 → 重启（有界）。"""
        done, lost = threading.Event(), threading.Event()
        interval = max(0.05, float(self.guard.lease_seconds) / 3.0)

        def renew():
            while not done.wait(interval):
                try:
                    valid = self.guard.renew()
                except Exception:
                    valid = False
                if not valid:
                    lost.set()
                    return

        renewer = threading.Thread(target=renew, name="supervisor-lease", daemon=True)
        renewer.start()
        try:
            return self._supervise_loop(poll_interval=min(poll_interval, interval),
                max_restarts=max_restarts, restart_backoff_seconds=restart_backoff_seconds,
                should_continue=should_continue, lost=lost)
        finally:
            done.set()
            renewer.join(timeout=5)

    def _supervise_loop(self, *, poll_interval, max_restarts,
                        restart_backoff_seconds, should_continue, lost):
        restarts = 0
        while should_continue is None or should_continue():
            if lost.is_set():
                self.stop(reason="single_active_lost")
                return {"ok": False, "reason_code": "single_active_lost", "restarts": restarts}
            if self.stop_requested():
                return self.stop(reason="stop_request")
            if self.child is not None and getattr(self.child, "poll", lambda: None)() is not None:
                # 子进程意外退出：先对账接管（可注入），再有界重启
                self._drain(self.session_factory, policy=self.policy_or_raise())
                restarts += 1
                if restarts > max_restarts:
                    self.stop(reason=f"max_restarts:{max_restarts}")
                    return {"ok": False, "reason_code": "max_restarts", "restarts": restarts}
                time.sleep(restart_backoff_seconds)
                if lost.is_set():
                    continue
                self.child = self._spawner(self.command, self.cwd, self.env)
            time.sleep(poll_interval)
        return self.stop(reason="loop_exit")

    def policy_or_raise(self):
        if self.policy is None:
            settings, error = _feishu_settings()
            if settings is None:
                raise ValueError(f"policy_required: {error or 'settings_unavailable'}")
            self.policy = settings
        return self.policy


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _session_factory_for(database_url: str):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EvoHarness 数字员工单活服务主管")
    parser.add_argument("action", choices=["check", "recover", "start", "stop"])
    parser.add_argument("--db", default=os.environ.get("DATABASE_URL", "sqlite:///data/agent.db"))
    parser.add_argument("--cwd", default=str(REPO / "feishu-daily-agent"))
    parser.add_argument("--command", default="python main.py run")
    parser.add_argument("--lock-dir", default=None, help="单活锁目录（默认 <cwd>/run）")
    parser.add_argument("--grace-seconds", type=float, default=30.0)
    parser.add_argument("--lease-seconds", type=float, default=90.0)
    parser.add_argument("--once", action="store_true",
                        help="前台运行一次：子进程退出后停机收尾，不自动重启（调试用）")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    lock_dir = Path(args.lock_dir).resolve() if args.lock_dir else cwd / "run"
    if args.action == "check":
        report = preflight(args.db)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 2
    if args.action == "recover":
        factory = _session_factory_for(args.db)
        print(json.dumps(recover_interrupted(factory), ensure_ascii=False, indent=2))
        return 0
    if args.action == "stop":
        path = cwd / STOP_REQUEST_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"reason": "cli", "at": utcnow().isoformat()},
                                   ensure_ascii=False), encoding="utf-8")
        print(f"停机请求已写入：{path}")
        return 0

    # start：先检查，再单活，再常驻
    report = preflight(args.db)
    if not report["ok"]:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    factory = _session_factory_for(args.db)
    supervisor = Supervisor(
        command=args.command.split(),
        cwd=cwd,
        database_url=args.db,
        lock_path=lock_dir / "single-active.lock",
        session_factory=factory,
        grace_seconds=args.grace_seconds,
        lease_seconds=int(args.lease_seconds),
    )
    try:
        supervisor.start(preflight_report=report)
    except SingleActiveRefused as exc:
        print(json.dumps({"ok": False, "reason_code": exc.reason_code,
                          "holder": exc.holder, "next_action": exc.next_action},
                         ensure_ascii=False, indent=2))
        return 3
    # POSIX：SIGTERM/SIGINT 走同一优雅停机路径
    if os.name != "nt":
        def _handler(signum, frame):  # noqa: ANN001
            supervisor.stop(reason=f"signal:{signum}")
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    if args.once:
        # 调试形态：子进程退出后即停机收尾，不自动重启
        result = supervisor.supervise(
            should_continue=lambda: supervisor.child is None
            or supervisor.child.poll() is None,
        )
    else:
        result = supervisor.supervise()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
