"""DE-23 单活服务主管：启动前检查、中断恢复接管、优雅停机、有界重启。"""
from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness import outbox as outbox_module
from evoharness.models import Base, Outbox, OutboxStatus, Task, TaskStatus
from evoharness.work import execution
from ops.single_active import SingleActiveRefused
from ops.supervisor import (
    STOP_REQUEST_FILE,
    Supervisor,
    preflight,
    recover_interrupted,
)

TOKEN_SECRET = "offline-test-only-token-secret"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_TOKEN_SECRET", TOKEN_SECRET)
    db = tmp_path / "agent.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return tmp_path, factory, engine


def _policy():
    return types.SimpleNamespace(deployment_mode="offline")


def _supervisor(env, *, spawner=None, drain=None, command=None):
    tmp_path, factory, _engine = env
    return Supervisor(
        command=command or ["python", "main.py", "run"],
        cwd=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'agent.db').as_posix()}",
        lock_path=tmp_path / "run" / "single-active.lock",
        session_factory=factory,
        policy=_policy(),
        lease_seconds=90,
        grace_seconds=5,
        spawner=spawner,
        drain=drain,
        stop_dir=tmp_path,
        clock=lambda: datetime(2026, 11, 14, 12, 0, 0),
    )


def test_preflight_runs_migrations_and_reports_checks(env):
    tmp_path, _factory, _engine = env
    report = preflight(f"sqlite:///{(tmp_path / 'agent.db').as_posix()}")
    assert report["ok"], report
    names = {c["name"] for c in report["checks"]}
    assert {"database_migrate", "token_secret", "settings_valid"} <= names
    assert all(c["ok"] for c in report["checks"])


def test_preflight_fails_without_token_secret(env, tmp_path, monkeypatch):
    _tmp, _factory, _engine = env
    monkeypatch.delenv("FEISHU_TOKEN_SECRET", raising=False)
    report = preflight(
        f"sqlite:///{(tmp_path / 'agent.db').as_posix()}",
        token_key_path=tmp_path / "absent" / ".token_key",
    )
    assert not report["ok"]
    failed = {c["name"] for c in report["checks"] if not c["ok"]}
    assert "token_secret" in failed


def _seed_interrupted_work(factory, *, now):
    """模拟崩溃现场：Run 租约过期 + outbox 停在 sending 且租约过期。"""
    from evoharness.work import service

    with factory.begin() as session:
        task = Task(task_key="de23", tenant="t", owner="alice", chat_id="chat", title="修复",
                    action="fix", status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                    goal="修复缺陷", acceptance_json='["回归通过"]', revision=1)
        session.add(task)
        session.flush()
        service.transition_to(session, task.id, to_state="queued", actor="alice")
        run = execution.request_run(session, task_id=task.id, context={"g": 1},
                                    actor_id="runner", output_root="out",
                                    idempotency_key="i1", validate_context=lambda s, c, a: True)
        claimed = execution.claim(session, run.id, worker="dead-worker",
                                  validate_context=lambda s, c, a: True, now=now,
                                  lease_seconds=60)
        row = outbox_module.enqueue(session, intent="de23|notice", tenant="t", kind="notice",
                                    target_type="chat", target_id="oc_1", payload={"text": "hi"},
                                    next_at=now)  # 注入固定时钟：真实 utcnow 晚于固定 now 会让 claim_next(now=now) 看不到行（时间脆弱）
        token = outbox_module.claim_next(session, now=now, outbox_id=row.id)
    return task, claimed, row, token


def test_recover_interrupted_takes_over_run_and_outbox(env):
    tmp_path, factory, _engine = env
    now = datetime(2026, 11, 14, 12, 0, 0)
    after_crash = datetime(2026, 11, 14, 13, 0, 0)  # 租约（60s/120s）已全部过期
    task, run, row, _token = _seed_interrupted_work(factory, now=now)

    # 崩溃后重启：统一对账接管
    report = recover_interrupted(factory, policy=_policy(), now=after_crash)
    assert run.id in report["runsMovedToRecovery"]
    assert row.id in report["outboxLeasesRecovered"]

    with factory() as session:
        recovered_run = session.get(execution.ExecutionRun, run.id)
        assert recovered_run.state == "waiting_recovery"
        assert recovered_run.generation == run.generation + 1
        assert "reconcile" in (recovered_run.error or "")
        outbox = session.get(Outbox, row.id)
        # offline：回执未知，等对账，不盲重试
        assert outbox.status == OutboxStatus.DELIVERY_UNKNOWN

    # 对账通过后显式恢复：Run 可重新认领，outbox 可重新排队
    with factory() as session:
        resumed = execution.resume(session, run.id, reconcile=lambda s, r: True,
                                   disposition="queued", note="drill reconcile ok")
        assert resumed.state == "queued"
        outbox = session.get(Outbox, row.id)
        outbox.status = OutboxStatus.QUEUED
        outbox.next_at = after_crash
        session.commit()
        assert outbox_module.claim_next(session, now=after_crash, outbox_id=row.id) is not None


class FakeChild:
    """离线子进程替身：记录信号，不真正拉起网络服务。"""

    def __init__(self, *, exit_after_signal: bool = True, exit_code: int = 0):
        self.pid = 424242
        self.signals: list[int] = []
        self.terminated = False
        self.returncode = None
        self._exit_after_signal = exit_after_signal
        self._exit_code = exit_code

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if self._exit_after_signal:
            self.returncode = self._exit_code

    def terminate(self):
        self.terminated = True
        self.returncode = self._exit_code

    def wait(self, timeout=None):
        return self.returncode


def test_supervisor_start_graceful_stop_and_single_active(env):
    tmp_path, factory, _engine = env
    drained: list[dict] = []
    drain_calls: list[object] = []

    def drain(session_factory, *, policy, now=None):
        drain_calls.append(session_factory)
        result = recover_interrupted(session_factory, policy=policy)
        drained.append(result)
        return result

    child = FakeChild()
    supervisor = _supervisor(env, spawner=lambda cmd, cwd, env_: child, drain=drain)
    started = supervisor.start()
    assert started["ok"] and started["pid"] == 424242

    # 第二写实例必须被拒并给出原因（端到端走 supervisor 的单活守卫）
    second = _supervisor(env, spawner=lambda cmd, cwd, env_: FakeChild(), drain=drain)
    with pytest.raises(SingleActiveRefused) as excinfo:
        second.start()
    assert excinfo.value.reason_code == "single_active_already_running"

    # 优雅停机：子进程收到终止信号 → 在途副作用对账 → 单活锁释放
    stopped = supervisor.stop(reason="drill")
    assert stopped["ok"] and stopped["childTerminated"] is True
    assert child.signals, "停机必须向子进程投递终止信号"
    assert len(drain_calls) == 1, "停机时执行一次统一对账"
    assert supervisor.guard.holder in (None, {}), "停机后不再持有单活身份"

    # 锁已释放：第二个实例现在可以启动
    third = _supervisor(env, spawner=lambda cmd, cwd, env_: FakeChild(), drain=drain)
    assert third.start()["ok"]
    third.stop(reason="cleanup")


def test_supervisor_stop_request_file_roundtrip(env):
    tmp_path, factory, _engine = env
    supervisor = _supervisor(env, spawner=lambda cmd, cwd, env_: FakeChild())
    assert not supervisor.stop_requested()
    supervisor.request_stop(reason="ops")
    assert (tmp_path / STOP_REQUEST_FILE).is_file()
    assert supervisor.stop_requested()
    supervisor.start()
    result = supervisor.stop(reason="stop_request")
    assert result["ok"]
    assert not (tmp_path / STOP_REQUEST_FILE).exists(), "停机后清掉请求文件"


def test_supervise_loop_recovers_and_bounds_restarts(env):
    tmp_path, factory, _engine = env
    recover_calls: list[dict] = []

    def spy_recover(session_factory, *, policy, now=None):
        result = recover_interrupted(session_factory, policy=policy)
        recover_calls.append(result)
        return result

    # 每次拉起的子进程立即退出：主管应先对账再有界重启，最终放弃
    spawned: list[FakeChild] = []

    def spawner(cmd, cwd, env_):
        child = FakeChild(exit_after_signal=False, exit_code=1)
        child.returncode = 1  # 启动即崩
        spawned.append(child)
        return child

    supervisor = _supervisor(env, spawner=spawner,
                             drain=lambda sf, *, policy, now=None: spy_recover(sf, policy=policy))
    supervisor.start()
    ticks = {"n": 0}

    def should_continue():
        ticks["n"] += 1
        return ticks["n"] <= 10

    result = supervisor.supervise(poll_interval=0.001, max_restarts=2,
                                  restart_backoff_seconds=0,
                                  should_continue=should_continue)
    assert result["ok"] is False and result["reason_code"] == "max_restarts"
    assert len(spawned) == 3, "初始 1 次 + 有界重启 2 次"
    assert len(recover_calls) >= 2, "每次子进程退出都先对账再重启"
    assert supervisor.guard.holder in (None, {}), "放弃重启时必须释放单活锁"
