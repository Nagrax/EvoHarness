"""EVO-04（DE-14）跨进程崩溃生产演练：真实守护进程被外力终止后的接管续跑。

与 tests/test_worker_durability.py 的口径差别（DE-14 剩余项
"跨进程崩溃生产演练仍外部"）：

- 那是"测试内子进程"：崩溃由子进程自己的 os._exit(27) 注入，接管进程用
  显式推进的时钟（now+300s）触发恢复扫描——都不是生产形态；
- 本演练是"生产形态"：两个真实独立进程 + 真实时钟，一键可重复：

  1. 受害者是以生产装配（``worker_cli.build_adapter(profile="fake")`` +
     ``WorkWorker.run_forever``）运行的常驻守护进程：单实例 DB 锁 + 独立
     续租线程 + 轮询消费，与 ``python -m evoharness worker --daemon`` 同一
     装配路径；
  2. 认领（generation/lease_owner）与预算 envelope 预留已持久提交后（锚点
     是轮询 DB 的已提交状态，不是固定 sleep），编排者用 ``Popen.kill()``
     外力终止——Windows 即 TerminateProcess、POSIX 即 SIGKILL，进程没有任何
     优雅收尾机会（phase_hook("claimed") 是 worker/executor 既定的
     崩溃注入/观测点，演练用它把守护进程停在"认领已提交、调用未开始"的
     持久化锚点上，保证 kill 时点确定性）；
  3. 卡住段证明（不接管会卡住）：受害者死后即使执行租约真实过期
     （lease_until <= now），没有任何进程推进 Run——无自愈；
  4. 接管者同样是真实 worker 进程：等受害者单活 DB 租约过期（单活锁认领
     不再被拒）→ 每轮 ``run_once`` 的 ``recover_expired`` → waiting_recovery
     时按生产接管 API ``resume_recovered`` 依持久回执对账 → 放回队列 →
     重新认领续跑到 waiting_acceptance。

演练不修改任何产品代码：phase_hook 是 worker/executor 既定注入点；租约/
轮询参数全部是 WorkerConfig 既有配置项（演练用短租约，避免按生产默认
120s/90s 等待真实时钟）。验收断言：run 续跑到 waiting_acceptance、
envelope 复用不双预留、usage 恰好一次、generation/lease_owner 更新。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from evoharness.__main__ import main as root_main          # noqa: E402
from evoharness.api import WorkApi                          # noqa: E402
from evoharness.application import Application              # noqa: E402
from evoharness.clock import utcnow                         # noqa: E402
from evoharness.governance import budget                    # noqa: E402
from evoharness.governance.models import BudgetReservation  # noqa: E402
from evoharness.work.models import ExecutionRun             # noqa: E402

# 演练用短租约（生产默认 run lease 120s / 单活锁 90s）：卡住段按真实时钟
# 等过期，短租约让一键演练在秒级完成，语义与生产完全同构（等过期才可接管）。
LEASE_SECONDS = 6
VICTIM_ID = "w-drill-victim"
TAKEOVER_ID = "w-drill-takeover"
SINGLE_ACTIVE_KEY = "work.worker.single_active"

CODE_ACCEPTANCE = [{"kind": "code", "criteria": "回归全部通过",
                    "required_artifacts": ["diff", "test_report"]}]


@pytest.fixture()
def env(tmp_path):
    db_url = "sqlite:///" + (tmp_path / "crash_drill.db").as_posix()
    application = Application.open(db_url)
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    out = io.StringIO()
    code = root_main(["--db", db_url, "--json", "init", "--org-id", "org-demo"],
                     stdout=out)
    assert code == 0, out.getvalue()
    with application.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    yield SimpleNamespace(application=application, api=api, tmp=tmp_path,
                          db_url=db_url)
    application.engine.dispose()


def _create_approved_run(env) -> str:
    """装配一条已批准的 delegated_delivery Run（与耐久矩阵同一装配思路）。"""
    created = env.api.create_work_item(
        tenant="local", owner="founder", title="跨进程崩溃演练任务", action="fix",
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
    return started["data"]["operation_id"]


# ── 演练子进程脚本（生产装配 + 真实守护循环） ─────────────────────────────────

_CHILD_COMMON = r'''
import sys, threading
from pathlib import Path
sys.path.insert(0, r"@REPO@")

from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.work import worker_cli
from evoharness.work.worker import WorkWorker, WorkerConfig


def build(db_url, artifacts_dir):
    # 与根入口 worker 命令同一生产装配（fake profile：离线确定性剧本）。
    # N03：生产 fake profile 的检查器默认走隔离容器通道（Docker 不可用即
    # waiting_environment，fail-closed）。本演练的目标是崩溃/租约/接管语义，
    # 容器隔离由 N03 专项与 Linux CI 把关；离线一键演练按仓库既有约定
    # （tests/test_dryrun_boundary.py 等）走宿主 ScriptTestRunner。
    application = Application.open(db_url)
    api = WorkApi(application, artifacts_dir=artifacts_dir)
    adapter = worker_cli.build_adapter(application, profile="fake")
    adapter.isolate_checks = False
    return application, api, adapter
'''


_VICTIM_MAIN = r'''
db_url, worker_id, lease_seconds, artifacts_dir = sys.argv[1:5]
application, api, adapter = build(db_url, artifacts_dir)
held = threading.Event()


def phase_gate(name):
    # 生产既定的崩溃注入/观测点：认领相（generation/lease_owner）已独立
    # 短事务 COMMIT。守护进程在此停住、不再推进任何持久相——编排者锚定
    # DB 已提交状态后以外力终止（非优雅停机，收尾逻辑不会执行）。
    if name == "claimed":
        held.wait()


worker = WorkWorker(application.session_factory, adapter, api._authorize_adapter(),
                    config=WorkerConfig(worker_id=worker_id,
                                        poll_interval_seconds=0.05,
                                        lease_seconds=int(lease_seconds),
                                        heartbeat_interval_seconds=2.0,
                                        single_active_lease_seconds=int(lease_seconds)),
                    phase_hook=phase_gate)
worker.run_forever()   # 常驻守护循环；本进程中由外力终止
'''


_TAKEOVER_MAIN = r'''
import json, time
from pathlib import Path

from evoharness.work.models import ExecutionRun
from ops.single_active import SingleActiveRefused

db_url, run_id, worker_id, lease_seconds, out_path, artifacts_dir = sys.argv[1:7]
application, api, adapter = build(db_url, artifacts_dir)
observed = {}


def observe(name):
    # 生产观测点：接管认领提交后的租约事实（父进程断言 generation/owner 更新）
    if name == "claimed":
        with application.transaction() as session:
            run = session.get(ExecutionRun, run_id)
            observed["claim"] = {"state": run.state, "generation": int(run.generation),
                                 "lease_owner": run.lease_owner}


worker = WorkWorker(application.session_factory, adapter, api._authorize_adapter(),
                    config=WorkerConfig(worker_id=worker_id,
                                        poll_interval_seconds=0.05,
                                        lease_seconds=int(lease_seconds),
                                        heartbeat_interval_seconds=2.0,
                                        single_active_lease_seconds=int(lease_seconds)),
                    phase_hook=observe)

# 生产接管序列（R06）：单活锁（受害者租约未过期即被拒，等真实时钟过期）
# → 每轮 recover_expired → waiting_recovery 时显式 resume_recovered 依持久
# 回执对账 → 放回队列 → 重新认领续跑到 waiting_acceptance。
deadline = time.monotonic() + 120
final_state = ""
while time.monotonic() < deadline:
    try:
        worker.run_once(single_active=True)
    except SingleActiveRefused:
        pass   # 被杀守护进程的单活租约未过期：接管必须等它真实过期
    with application.transaction() as session:
        final_state = session.get(ExecutionRun, run_id).state
    if final_state == "waiting_recovery":
        worker.resume_recovered(run_id)
    elif final_state == "waiting_acceptance":
        break
    time.sleep(0.05)
worker.release_single_active()
Path(out_path).write_text(json.dumps(
    {"final_state": final_state, "claim": observed.get("claim")},
    ensure_ascii=False), encoding="utf-8")
sys.exit(0 if final_state == "waiting_acceptance" else 3)
'''


def _write_child(tmp_path: Path, name: str, main: str) -> Path:
    script = tmp_path / name
    script.write_text(_CHILD_COMMON.replace("@REPO@", str(REPO_ROOT)) + main,
                      encoding="utf-8")
    return script


# ── 父进程观测（新连接读取 = 已提交事实） ─────────────────────────────────────

def _snapshot(env, run_id: str) -> dict:
    with env.application.transaction() as session:
        run = session.get(ExecutionRun, run_id)
        rows = list(session.scalars(select(BudgetReservation).where(
            BudgetReservation.run_id == run_id)))
        return {"state": run.state,
                "generation": int(run.generation or 0),
                "lease_owner": run.lease_owner,
                "attempts": int(run.attempts or 0),
                "lease_until": run.lease_until,
                "lease_expired": (run.lease_until is not None
                                  and run.lease_until <= utcnow()),
                "envelopes": [{"status": r.status, "used": int(r.used_tokens or 0),
                               "reserved": int(r.reserved_tokens or 0)}
                              for r in rows if r.kind == "envelope"],
                "calls": [{"call_id": r.call_id, "status": r.status,
                           "intent": r.intent_status,
                           "usage_status": r.usage_status,
                           "used": int(r.used_tokens or 0)}
                          for r in rows if r.kind == "call"]}


def _poll_db(env, run_id: str, predicate, *, timeout_seconds: float,
             what: str, interval: float = 0.1) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        last = _snapshot(env, run_id)
        if predicate(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"演练超时（{timeout_seconds}s）等待：{what}；最后快照={last}")


def _single_active_holder(env) -> dict | None:
    from ops.single_active import read_db_lease

    with env.application.transaction() as session:
        return read_db_lease(session, SINGLE_ACTIVE_KEY)


def test_daemon_force_killed_then_real_worker_takeover_recovers(env):
    run_id = _create_approved_run(env)

    # ── 第一幕：真实 worker 守护进程认领，停在外力终止锚点上 ──────────────────
    victim_script = _write_child(env.tmp, "drill_victim.py", _VICTIM_MAIN)
    victim_out = (env.tmp / "victim.stdout.log").open("w", encoding="utf-8")
    victim_err = (env.tmp / "victim.stderr.log").open("w", encoding="utf-8")
    victim = subprocess.Popen(
        [sys.executable, "-I", str(victim_script), env.db_url, VICTIM_ID,
         str(LEASE_SECONDS), str(env.tmp / "artifacts")],
        stdout=victim_out, stderr=victim_err,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    killed = False
    try:
        # 锚点（DB 已提交状态，非固定秒数）：认领 + envelope 预留均已持久化，
        # 且守护进程确实持有单实例 DB 锁（daemon 形态事实）。
        claim = _poll_db(
            env, run_id,
            lambda s: (s["state"] in {"claimed", "running"}
                       and s["generation"] >= 1 and s["lease_owner"] == VICTIM_ID
                       and len(s["envelopes"]) == 1
                       and s["envelopes"][0]["status"] == "reserved"),
            timeout_seconds=90, what="受害者认领+envelope 持久提交")
        assert claim["generation"] == 1
        assert claim["calls"] == []                       # 尚无任何模型调用
        holder = _single_active_holder(env)
        assert holder and holder.get("holder_id") == VICTIM_ID, holder

        # ── 外力终止：TerminateProcess（Windows）/ SIGKILL（POSIX），非优雅停机 ──
        victim.kill()
        victim.wait(timeout=30)
        killed = True
        assert victim.returncode != 0, "外力终止不可能优雅退出（returncode 0）"

        # ── 卡住段（不接管会卡住）：执行租约真实过期后仍无人推进 ────────────────
        stalled = _poll_db(env, run_id, lambda s: s["lease_expired"],
                           timeout_seconds=90, what="受害者执行租约真实过期")
        # 无自愈：没有接管进程时，过期租约不会自己变成恢复/重跑——
        # 状态、generation、租约归属、预算账目全部停在被害时刻。
        assert stalled["state"] in {"claimed", "running"}
        assert stalled["generation"] == 1 and stalled["lease_owner"] == VICTIM_ID
        assert len(stalled["envelopes"]) == 1
        assert stalled["envelopes"][0]["status"] == "reserved"
        assert stalled["calls"] == []

        # ── 接管段：另一真实 worker 进程按生产接管序列恢复续跑 ──────────────────
        takeover_script = _write_child(env.tmp, "drill_takeover.py", _TAKEOVER_MAIN)
        report = env.tmp / "takeover.report.json"
        takeover = subprocess.run(
            [sys.executable, "-I", str(takeover_script), env.db_url, run_id,
             TAKEOVER_ID, str(LEASE_SECONDS), str(report),
             str(env.tmp / "artifacts")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        assert takeover.returncode == 0, (
            f"接管进程失败 rc={takeover.returncode}\n"
            f"stdout={takeover.stdout[-2000:]}\nstderr={takeover.stderr[-2000:]}")
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["final_state"] == "waiting_acceptance", payload
        # 接管认领事实（生产观测点回传）：generation 递增、lease_owner 换人
        assert payload["claim"] == {"state": "running", "generation": 3,
                                    "lease_owner": TAKEOVER_ID}, payload

        # ── 终态持久事实（新连接读取 = 已提交事实） ──────────────────────────────
        final = _snapshot(env, run_id)
        assert final["state"] == "waiting_acceptance"
        # generation：受害认领 +1 → 过期回收 +1 → 接管认领 +1 = 3；
        # attempts：取决于外力终止落在 victim 的哪个执行相位——
        #   claimed（执行相位未启动）→ 接管续跑即第 1 次执行尝试；
        #   running（执行相位已启动）→ 接管续跑是第 2 次。
        # 两种顺序都是产品正确行为（外力 kill 与相位推进天然竞争），
        # 期望值由观测到的卡住相位确定性推导；核心不变量是账目恰好一次（下方断言）。
        assert final["generation"] == 3
        assert final["attempts"] == (2 if stalled["state"] == "running" else 1)
        # 等待类状态释放执行租约（LeaseRelease），接管期租约事实由观测点留证
        assert final["lease_owner"] == "" and final["lease_until"] is None
        # envelope 复用不双预留：被害前预留的那一份被接管相 A 幂等复用并结算
        assert len(final["envelopes"]) == 1
        assert final["envelopes"][0]["status"] == "settled"
        assert final["envelopes"][0]["used"] == 0            # N04：不聚合第二份账
        assert final["envelopes"][0]["reserved"] > 0
        # usage 恰好一次：3 次调用唯一 call_id，150+70+20=240，全部真实回执
        calls = final["calls"]
        assert len(calls) == 3
        assert len({c["call_id"] for c in calls}) == 3
        assert sum(c["used"] for c in calls) == 240
        assert all(c["status"] == "settled" and c["usage_status"] == "recorded"
                   and c["intent"] == "recorded" for c in calls)
        with env.application.transaction() as session:
            summary = budget.usage_summary(session, org_id="org-demo")
            assert summary["used_total"] == 240              # 不是双记的 480
            assert summary["reserved_open"] == 0             # 托管全部释放
            assert summary["by_category"] == {"model_call": 240, "work.run": 0}
            run = session.get(ExecutionRun, run_id)
            checkpoint = json.loads(run.checkpoint_json or "{}")
            reconciliation = (checkpoint.get("frozen") or {}).get("reconciliation")
            # 接管对账结论随 checkpoint 持久化（按回执对账，非盲重放）
            assert reconciliation is not None, checkpoint
            assert reconciliation["generation"] == 2
            assert reconciliation["completed_calls"] == []
            assert reconciliation["unknown_intents"] == []
    finally:
        if not killed and victim.poll() is None:
            victim.kill()
            try:
                victim.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        victim_out.close()
        victim_err.close()
