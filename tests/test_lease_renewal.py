"""R05/R06：Run 租约续期（心跳）与崩溃恢复接管语义。

覆盖（对应独立审核探针 executor_long_run / executor_recovery 的负例）：
- 121 秒模拟执行（控制时钟跨过 120 秒固定租约）后回写成功、Run 正常完成、
  产物被验收链登记并可通过独立验收到 done；
- renew_lease API：stale worker/generation 拒绝、已被收走的过期租约不续、
  续租后新过期时间正确；
- 心跳线程在适配器执行期间按控制时钟续租（主线程阻塞期间只有心跳能续）；
- 心跳禁用时复现修复前旧行为（stale_run_lease），证明续租是修复来源；
- 等待类状态释放执行租约（LeaseRelease）→ reacquire 重新授权认领 → 恢复续跑；
- recover_expired 覆盖仍持过期租约的等待类 Run；
- 恢复统一 Task/Run 语义（resume 同步 Task 转换并留 TaskEvent）；
- prepare_workspace 复用既有工作区：读取原 checkpoint 校验、按新 generation
  续跑、未完成项传给适配器、不匹配才报 workspace_exists；
- 真实子进程级接管：worker A 崩溃后进程 B 经恢复语义接管续跑成功。

全部离线、控制时钟、确定性：内存库经 StaticPool 供心跳线程跨线程访问，
子进程用独立文件库，不触网、不调用真实模型。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness.clock import utcnow
from evoharness.models import Base, Task, TaskEvent, TaskStatus
from evoharness.work import acceptance, execution, executor, service
from evoharness.work.models import Artifact, ExecutionRun

REPO_ROOT = Path(__file__).resolve().parents[1]


def _allow(*_args, **_kwargs):
    return True


@pytest.fixture()
def env():
    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
    engine.dispose()


def _task_run(s, tmp_path: Path, key: str = "k1", actor_id: str = "executor"):
    task = Task(task_key=key, tenant="t", owner="alice", chat_id="chat", title="修复",
                action="fix", status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                goal="修复缺陷", acceptance_json='[{"kind": "code"}]',
                revision=1, assignee_id="bob")
    s.add(task)
    s.flush()
    run = execution.request_run(s, task_id=task.id, context={"g": 1}, actor_id=actor_id,
                                output_root=str(tmp_path / "out"), idempotency_key=key,
                                validate_context=_allow)
    s.flush()
    service.transition_to(s, task.id, to_state="queued", actor="bob")
    return task, run


# ── R05 探针 1 负例：121 秒模拟执行仍能回写并完成 ────────────────────────────


def test_long_execution_121s_renews_lease_and_delivers(monkeypatch, env, tmp_path):
    s = env
    task, run = _task_run(s, tmp_path)
    t0 = utcnow()
    clock = {"now": t0}
    monkeypatch.setattr(execution, "utcnow", lambda: clock["now"])

    class LongAdapter:
        kind = "synthetic"

        def execute(self, workdir, *args):
            (workdir / "answer.txt").write_text("synthetic artifact", encoding="utf-8")
            (workdir / "report.txt").write_text("checks passed", encoding="utf-8")
            clock["now"] = t0 + timedelta(seconds=121)   # 控制时钟跨过 120s 固定租约
            return executor.AdapterResult(ok=True, claimed_done=True,
                                          artifacts=[("diff", "answer.txt"),
                                                     ("test_report", "report.txt")])

    result = executor.execute_run(s, run.id, adapter=LongAdapter(), worker="w",
                                  validate_context=_allow)
    assert result["claimed"] and result["ok"]
    assert run.state == "waiting_acceptance"
    assert task.status == "waiting_acceptance"
    # 产物被验收链登记
    artifacts = list(s.scalars(select(Artifact).where(Artifact.run_id == run.id)))
    assert {a.kind for a in artifacts} == {"diff", "test_report"}
    assert all(a.sha256 for a in artifacts)
    # 验收链可用登记产物走完：独立验收通过才 done
    acceptance.request_acceptance(s, task.id, run_id=run.id, requester="bob")
    acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert task.status == TaskStatus.DONE


# ── R05 续租 API 单元 ────────────────────────────────────────────────────────


def test_renew_lease_semantics(env, tmp_path):
    s = env
    task, run = _task_run(s, tmp_path)
    t0 = utcnow()
    claimed = execution.claim(s, run.id, worker="w1", validate_context=_allow,
                              now=t0, lease_seconds=120)
    generation = claimed.generation
    # 续租后新过期时间正确：now + lease_seconds
    renewed = execution.renew_lease(s, run.id, worker="w1", generation=generation,
                                    now=t0 + timedelta(seconds=30), lease_seconds=120)
    assert renewed.lease_until == t0 + timedelta(seconds=150)
    # 非 worker 持有者拒绝
    with pytest.raises(ValueError, match="stale_run_lease"):
        execution.renew_lease(s, run.id, worker="w2", generation=generation,
                              now=t0 + timedelta(seconds=31))
    # 旧代际拒绝
    with pytest.raises(ValueError, match="stale_run_lease"):
        execution.renew_lease(s, run.id, worker="w1", generation=generation + 5,
                              now=t0 + timedelta(seconds=32))
    # queued 状态（未持有执行租约）拒绝
    run.state = "queued"
    s.flush()
    with pytest.raises(ValueError, match="stale_run_lease"):
        execution.renew_lease(s, run.id, worker="w1", generation=generation,
                              now=t0 + timedelta(seconds=33))
    run.state = "claimed"
    s.flush()
    # 已过期且被恢复收走的租约不续：generation 已递增、owner 已清空
    run.lease_until = t0 - timedelta(seconds=60)
    s.flush()
    recovered = execution.recover_expired(s, now=t0)
    assert run.id in recovered and run.state == "waiting_recovery"
    with pytest.raises(ValueError, match="stale_run_lease"):
        execution.renew_lease(s, run.id, worker="w1", generation=generation,
                              now=t0 + timedelta(seconds=34))
    # 非法时长拒绝
    with pytest.raises(ValueError, match="invalid_lease_seconds"):
        execution.renew_lease(s, run.id, worker="w1", generation=generation + 2,
                              now=t0, lease_seconds=0)


def test_heartbeat_renews_with_control_clock(monkeypatch, env, tmp_path):
    """控制时钟在适配器执行中跨过原租约：主线程阻塞期间只有心跳线程能续租。"""
    s = env
    task, run = _task_run(s, tmp_path)
    t0 = utcnow()
    clock = {"now": t0}
    monkeypatch.setattr(execution, "utcnow", lambda: clock["now"])
    main_thread = threading.current_thread()
    heartbeat_renewed = threading.Event()
    real_renew = execution.renew_lease
    heartbeat_calls = []

    def spy(session, run_id, *, worker, generation, now=None, lease_seconds=120):
        if threading.current_thread() is not main_thread and now is not None \
                and now > t0 + timedelta(seconds=120):
            heartbeat_calls.append(now)
            heartbeat_renewed.set()
        return real_renew(session, run_id, worker=worker, generation=generation,
                          now=now, lease_seconds=lease_seconds)

    monkeypatch.setattr(execution, "renew_lease", spy)

    class BlockedAdapter:
        kind = "synthetic"

        def execute(self, workdir, *args):
            (workdir / "answer.txt").write_text("artifact", encoding="utf-8")
            clock["now"] = t0 + timedelta(seconds=130)   # 跨过原 120s 租约
            # 主线程阻塞在适配器里：此后发生的续租只能来自心跳线程
            assert heartbeat_renewed.wait(timeout=10), "heartbeat did not renew"
            return executor.AdapterResult(ok=True, claimed_done=True,
                                          artifacts=[("diff", "answer.txt")])

    result = executor.execute_run(s, run.id, adapter=BlockedAdapter(), worker="w1",
                                  validate_context=_allow, heartbeat_interval=0.05)
    assert result["ok"] is True and result["heartbeat_ticks"] >= 1
    assert heartbeat_renewed.is_set() and heartbeat_calls
    assert run.state == "waiting_acceptance"
    assert task.status == "waiting_acceptance"


def test_heartbeat_disabled_reproduces_stale_run_lease(monkeypatch, env, tmp_path):
    """心跳（续租能力）禁用时旧行为可复现：跨租约回写抛 stale_run_lease、Run 卡 running。"""
    s = env
    task, run = _task_run(s, tmp_path)
    t0 = utcnow()
    clock = {"now": t0}
    monkeypatch.setattr(execution, "utcnow", lambda: clock["now"])

    class LongAdapter:
        kind = "synthetic"

        def execute(self, workdir, *args):
            (workdir / "answer.txt").write_text("artifact", encoding="utf-8")
            clock["now"] = t0 + timedelta(seconds=130)
            return executor.AdapterResult(ok=True, claimed_done=True,
                                          artifacts=[("diff", "answer.txt")])

    with pytest.raises(ValueError, match="stale_run_lease"):
        executor.execute_run(s, run.id, adapter=LongAdapter(), worker="w1",
                             validate_context=_allow, heartbeat=False)
    assert run.state == "running"          # 修复前缺陷形态：租约过期后无法收尾
    assert task.status == "running"


# ── R05 等待类：释放租约 → 重新授权认领 → 恢复续跑 ──────────────────────────


def test_waiting_release_then_reacquire_restores_execution(env, tmp_path):
    s = env
    task, run = _task_run(s, tmp_path)
    claimed = execution.claim(s, run.id, worker="w1", validate_context=_allow)
    first_generation = claimed.generation   # 认领与恢复共用同一 ORM 对象，先固化代数
    service.transition_to(s, task.id, to_state="running", actor="w1")
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_allow)
    # 进入等待即释放执行租约（LeaseRelease）：不占住单活执行位
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="waiting_budget", validate_context=_allow)
    assert run.lease_owner == "" and run.lease_until is None
    # 租约已释放：不经重新认领不能回 running（旧 worker 直写过不了 fencing）
    with pytest.raises(ValueError, match="stale_run_lease"):
        execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                             state="running", validate_context=_allow)
    # 预算恢复后重新授权认领成功：generation 递增、租约重取、状态回 claimed
    reacquired = execution.reacquire(s, run.id, worker="w2", validate_context=_allow)
    assert reacquired.state == "claimed"
    assert reacquired.generation == first_generation + 1
    assert reacquired.lease_owner == "w2" and reacquired.lease_until is not None
    execution.transition(s, run.id, worker="w2", generation=reacquired.generation,
                         state="running", validate_context=_allow)
    assert task.status == "running"
    # 非等待类状态不可重取认领
    with pytest.raises(ValueError, match="run_not_reclaimable"):
        execution.reacquire(s, run.id, worker="w3", validate_context=_allow)
    # 授权失效时重取认领被拒（等待不是绕过授权的通道）
    execution.transition(s, run.id, worker="w2", generation=reacquired.generation,
                         state="waiting_external", validate_context=_allow)
    assert run.lease_owner == "" and run.lease_until is None
    denied = lambda *a, **k: False
    with pytest.raises(ValueError, match="authorization_invalid"):
        execution.reacquire(s, run.id, worker="w3", validate_context=denied)


def test_recover_expired_covers_waiting_runs_with_stale_lease(env, tmp_path):
    s = env
    task, run = _task_run(s, tmp_path)
    claimed = execution.claim(s, run.id, worker="w1", validate_context=_allow)
    service.transition_to(s, task.id, to_state="running", actor="w1")
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_allow)
    # 崩溃于"进入等待"与"释放租约"之间的历史形态：等待态仍持过期租约 → 可恢复
    run.state = "waiting_external"
    run.lease_until = utcnow() - timedelta(seconds=60)
    s.flush()
    recovered = execution.recover_expired(s, now=utcnow())
    assert run.id in recovered and run.state == "waiting_recovery"

    # 已按 LeaseRelease 释放租约的等待 Run 安全停放，不被误回收
    task2, run2 = _task_run(s, tmp_path, key="k2")
    claimed2 = execution.claim(s, run2.id, worker="w1", validate_context=_allow)
    service.transition_to(s, task2.id, to_state="running", actor="w1")
    execution.transition(s, run2.id, worker="w1", generation=claimed2.generation,
                         state="running", validate_context=_allow)
    execution.transition(s, run2.id, worker="w1", generation=claimed2.generation,
                         state="waiting_budget", validate_context=_allow)
    assert run2.lease_until is None
    assert run2.id not in execution.recover_expired(s, now=utcnow())
    assert run2.state == "waiting_budget"


# ── R06 恢复统一 Task/Run 语义 ──────────────────────────────────────────────


def test_resume_syncs_task_state_and_records_event(env, tmp_path):
    s = env
    task, run = _task_run(s, tmp_path)
    claimed = execution.claim(s, run.id, worker="w1", validate_context=_allow)
    service.transition_to(s, task.id, to_state="running", actor="w1")
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_allow)
    run.lease_until = utcnow() - timedelta(seconds=60)
    s.flush()
    execution.recover_expired(s, now=utcnow())
    assert run.state == "waiting_recovery" and task.status == "running"
    # 对账通过 → queued：Task 走恢复转换 running→queued 并留 TaskEvent
    execution.resume(s, run.id, reconcile=lambda sess, r: True, disposition="queued")
    assert run.state == "queued" and task.status == "queued"
    events = list(s.scalars(select(TaskEvent).where(
        TaskEvent.task_id == task.id, TaskEvent.event_type == "queued",
        TaskEvent.actor == "recovery")))
    assert events and "恢复" in events[-1].detail
    # 恢复后新 Worker 可正常认领并推进
    re_claimed = execution.claim(s, run.id, worker="w2", validate_context=_allow)
    assert re_claimed is not None
    execution.transition(s, run.id, worker="w2", generation=re_claimed.generation,
                         state="running", validate_context=_allow)
    # 对账未通过 → failed：Task 同步走到 failed
    run.lease_until = utcnow() - timedelta(seconds=60)
    s.flush()
    execution.recover_expired(s, now=utcnow())
    execution.resume(s, run.id, reconcile=lambda sess, r: False, disposition="queued",
                     note="side effect unmatched")
    assert run.state == "failed" and task.status == "failed"


# ── R06 探针 2 负例：恢复后新 Worker 全程成功 ────────────────────────────────


def test_recovery_flow_new_worker_succeeds_with_reused_workspace(env, tmp_path):
    s = env
    task, run = _task_run(s, tmp_path)
    claimed = execution.claim(s, run.id, worker="old", validate_context=_allow)
    service.transition_to(s, task.id, to_state="running", actor="old")
    execution.transition(s, run.id, worker="old", generation=claimed.generation,
                         state="running", validate_context=_allow,
                         checkpoint={"done": ["reproduce"], "todo": ["fix", "gate"],
                                     "next": "fix", "side_effects": []})
    workdir, _ = executor.prepare_workspace(run, task)
    (workdir / "from_first_attempt.txt").write_text("first attempt", encoding="utf-8")
    # 租约过期 → 恢复扫描 → 对账 → 重新排队
    recover_at = run.lease_until + timedelta(seconds=1)
    execution.recover_expired(s, now=recover_at)
    execution.resume(s, run.id, reconcile=lambda *a: True, disposition="queued")
    assert task.status == "queued"

    seen = {}

    class ResumeAdapter:
        kind = "synthetic"

        def execute(self, workdir_arg, context, allowed_tools, guard):
            seen["workdir"] = Path(workdir_arg)
            seen["resume_todo"] = list(context.get("resume_todo") or [])
            seen["resumed"] = bool((context.get("frozen") or {}).get("resumed"))
            seen["resumed_generation"] = (context.get("frozen") or {}).get("resumed_generation")
            assert (Path(workdir_arg) / "from_first_attempt.txt").is_file()
            (Path(workdir_arg) / "answer.txt").write_text("recovered", encoding="utf-8")
            return executor.AdapterResult(ok=True, claimed_done=True,
                                          artifacts=[("diff", "answer.txt")])

    result = executor.execute_run(s, run.id, adapter=ResumeAdapter(), worker="new",
                                  validate_context=_allow)
    assert result["ok"] is True and result["resumed"] is True
    assert seen["resumed"] is True
    assert seen["resume_todo"] == ["fix", "gate"]           # 原 checkpoint 未完成项传给适配器
    assert seen["resumed_generation"] == run.generation      # 按新 generation 续跑
    assert seen["workdir"] == workdir                        # 工作区复用非重建
    assert (workdir / "from_first_attempt.txt").read_text(encoding="utf-8") == "first attempt"
    # Task/Run 状态一致收尾
    assert run.state == "waiting_acceptance" and task.status == "waiting_acceptance"
    assert list(s.scalars(select(Artifact).where(Artifact.run_id == run.id)))[0].kind == "diff"


def test_prepare_workspace_reuse_validates_and_rejects_mismatch(env, tmp_path):
    s = env
    task, run = _task_run(s, tmp_path)
    root = Path(run.output_root).resolve() / f"run-{run.id[:12]}"
    root.mkdir(parents=True)
    (root / "keep.txt").write_text("kept", encoding="utf-8")
    # 无固化记录（同一路径即本 Run 专属目录）：复用并按新 generation 续跑
    run.generation = 3
    s.flush()
    workdir, frozen = executor.prepare_workspace(run, task)
    assert workdir == root and frozen["resumed"] is True
    assert frozen["resumed_generation"] == 3
    assert frozen["resumed_todo"] == []
    assert (root / "keep.txt").is_file()
    # 固化记录指向别处：不匹配才报错
    other = tmp_path / "somewhere-else"
    other.mkdir()
    run.checkpoint_json = json.dumps({"frozen": {"workspace": other.as_posix()},
                                      "todo": ["x"]})
    s.flush()
    with pytest.raises(ValueError, match="workspace_exists"):
        executor.prepare_workspace(run, task)


def test_prepare_workspace_failure_lands_run_failed_not_raised(env, tmp_path):
    """R06：准备工作区异常在适配器异常处理范围内——Run 记 failed，不裸抛。"""
    s = env
    task, run = _task_run(s, tmp_path)
    other = tmp_path / "occupied"
    other.mkdir()
    root = Path(run.output_root).resolve() / f"run-{run.id[:12]}"
    root.mkdir(parents=True)   # 工作区已存在才进入校验分支
    # 固化记录指向别处 → prepare_workspace 抛 workspace_exists → 转 Run 失败
    run.checkpoint_json = json.dumps({"frozen": {"workspace": other.as_posix()}})
    s.flush()
    result = executor.execute_run(s, run.id, adapter=_noop_adapter(), worker="w1",
                                  validate_context=_allow)
    assert result["ok"] is False and "workspace_exists" in result["error"]
    assert run.state == "failed"
    checkpoint = json.loads(run.checkpoint_json)
    assert "workspace_exists" in checkpoint["error"]
    assert task.status == "failed"


class _noop_adapter:
    kind = "noop"

    def execute(self, workdir, *args):  # pragma: no cover - 不应被调用
        raise AssertionError("adapter must not run when workspace preparation fails")


# ── 真实子进程级接管：A 崩溃后进程 B 接管续跑 ────────────────────────────────


_WORKER_A = '''
import json, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from evoharness.models import Task
from evoharness.work import execution, executor, service

def allow(*a, **k):
    return True

db, run_id = sys.argv[1], sys.argv[2]
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
engine = create_engine("sqlite:///" + db, future=True)
s = sessionmaker(bind=engine, expire_on_commit=False)()
claimed = execution.claim(s, run_id, worker="worker-A", validate_context=allow, lease_seconds=1)
task = s.get(Task, claimed.task_id)
service.transition_to(s, task.id, to_state="running", actor="worker-A")
execution.transition(s, run_id, worker="worker-A", generation=claimed.generation,
                     state="running", validate_context=allow)
workdir, frozen = executor.prepare_workspace(claimed, task)
(workdir / "from_a.txt").write_text("crashed mid-flight", encoding="utf-8")
s.commit()   # 认领/转换/工作区事实已提交；崩溃点 = 之后不再续租、不再写回
print(json.dumps({{"workspace": workdir.as_posix(), "generation": claimed.generation}}))
# 进程正常退出 = 崩溃模拟：不释放租约、不写回
'''

_WORKER_B = '''
import json, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from evoharness.work import execution, executor
from evoharness.work.executor import AdapterResult

def allow(*a, **k):
    return True

db, run_id = sys.argv[1], sys.argv[2]
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
engine = create_engine("sqlite:///" + db, future=True)
s = sessionmaker(bind=engine, expire_on_commit=False)()
recover_at = execution.utcnow() + __import__("datetime").timedelta(seconds=30)
recovered = execution.recover_expired(s, now=recover_at)
assert run_id in recovered, recovered
execution.resume(s, run_id, reconcile=lambda sess, r: True, disposition="queued")

class TakeoverAdapter:
    kind = "synthetic"
    def execute(self, workdir, context, allowed_tools, guard):
        frozen = context.get("frozen") or {{}}
        assert frozen.get("resumed") is True, frozen
        assert (Path(workdir) / "from_a.txt").is_file(), "workspace must be reused"
        (Path(workdir) / "answer.txt").write_text("recovered artifact", encoding="utf-8")
        return AdapterResult(ok=True, claimed_done=True,
                             artifacts=[("diff", "answer.txt")],
                             checkpoint={{"done": ["B 接管续跑"], "todo": [], "next": "acceptance"}})

result = executor.execute_run(s, run_id, adapter=TakeoverAdapter(), worker="worker-B",
                              validate_context=allow)
s.commit()   # B 的恢复与续跑事实提交，父进程独立连接核验终态
print(json.dumps({{"ok": result.get("ok"), "resumed": result.get("resumed"),
                   "workspace": result.get("workspace"), "error": result.get("error", "")}}))
'''


def test_subprocess_takeover_after_worker_crash(tmp_path):
    db = tmp_path / "takeover.db"
    engine = create_engine("sqlite:///" + db.as_posix(), future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    task = Task(task_key="k-sub", tenant="t", owner="alice", chat_id="chat", title="接管",
                action="fix", status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                goal="崩溃后由新进程接管", acceptance_json='[{"kind": "code"}]',
                revision=1, assignee_id="bob")
    s.add(task)
    s.flush()
    run = execution.request_run(s, task_id=task.id, context={"takeover": True},
                                actor_id="executor", output_root=str(tmp_path / "out"),
                                idempotency_key="sub", validate_context=_allow)
    s.flush()
    service.transition_to(s, task.id, to_state="queued", actor="bob")
    run_id = run.id
    s.commit()   # 子进程用独立连接：父进程必须先提交
    s.close()
    engine.dispose()

    script_a = tmp_path / "worker_a.py"
    script_a.write_text(_WORKER_A.format(repo=str(REPO_ROOT)), encoding="utf-8")
    script_b = tmp_path / "worker_b.py"
    script_b.write_text(_WORKER_B.format(repo=str(REPO_ROOT)), encoding="utf-8")
    env_vars = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    # worker A：认领 → running → 建工作区 → 崩溃退出（租约 1 秒即失效）
    proc_a = subprocess.run([sys.executable, "-I", str(script_a), str(db), run_id],
                            capture_output=True, text=True, timeout=120, env=env_vars)
    assert proc_a.returncode == 0, proc_a.stderr
    payload_a = json.loads(proc_a.stdout.strip().splitlines()[-1])

    # worker B：独立进程接管——恢复扫描 → 对账 → 重新排队 → 续跑完成
    proc_b = subprocess.run([sys.executable, "-I", str(script_b), str(db), run_id],
                            capture_output=True, text=True, timeout=120, env=env_vars)
    assert proc_b.returncode == 0, proc_b.stderr
    payload_b = json.loads(proc_b.stdout.strip().splitlines()[-1])
    assert payload_b["ok"] is True, payload_b
    assert payload_b["resumed"] is True
    # 工作区复用非重建（A 与 B 落在同一工作区路径）
    assert Path(payload_b["workspace"]).resolve() == Path(payload_a["workspace"]).resolve()

    # 父进程核验终态：Run/Task 状态一致、产物登记、A 的现场仍在
    engine2 = create_engine("sqlite:///" + db.as_posix(), future=True)
    s2 = sessionmaker(bind=engine2, expire_on_commit=False)()
    run_row = s2.get(ExecutionRun, run_id)
    task_row = s2.get(Task, task.id)
    assert run_row.state == "waiting_acceptance"
    assert task_row.status == "waiting_acceptance"
    assert run_row.attempts == 2            # A、B 各一次真实执行
    artifacts = list(s2.scalars(select(Artifact).where(Artifact.run_id == run_id)))
    assert [a.kind for a in artifacts] == ["diff"]
    workdir = Path(payload_a["workspace"])
    assert (workdir / "from_a.txt").read_text(encoding="utf-8") == "crashed mid-flight"
    assert (workdir / "answer.txt").read_text(encoding="utf-8") == "recovered artifact"
    s2.close()
    engine2.dispose()
