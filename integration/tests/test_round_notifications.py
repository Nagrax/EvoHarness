"""Offline notification gates and real scheduler/lease regression tests."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
for folder in (ROOT, ROOT / "integration", ROOT / "eval"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
from integration import iterate, feishu_notify
from integration.round_lease import RoundLease, process_marker


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr(iterate, "LOOP_LOCK", tmp_path / "iterate.lock")
    monkeypatch.setattr(iterate, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(iterate, "audit_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(iterate.task_intake, "run", lambda: {"added": 0, "items": []})
    monkeypatch.setattr(iterate, "_run", lambda *args, **kwargs: {"exitCode": 0, "seconds": 0})
    monkeypatch.setattr(iterate, "compose", lambda: "round report")
    monkeypatch.setattr(iterate.nudge, "collect", lambda: {})
    monkeypatch.setattr(iterate.nudge, "compose", lambda payload: "stuck task")
    return tmp_path


def test_skip_notify_suppresses_report_and_pm_notifier_calls(controller, monkeypatch):
    calls = []
    monkeypatch.setattr(iterate, "send_text", lambda *args, **kwargs: calls.append(kwargs))
    result = iterate.run_round(skip_notify=True)
    assert calls == []
    assert result["notify"]["mode"] == "disabled"
    assert result["notify"]["delivered"] is False
    assert Path(result["report"]).is_file()
    assert [step for step in result["steps"] if step["name"] == "pmNudge"][0]["pushed"] is False


def test_skip_notify_also_suppresses_scheduled_nudge(controller, monkeypatch):
    monkeypatch.setattr(iterate, "send_text", lambda *args, **kwargs: pytest.fail("notifier must not be called"))
    iterate._run_exclusive(lambda: iterate._push_nudge(skip_notify=True))


def test_dryrun_does_not_count_as_delivered_or_pushed(controller, monkeypatch):
    modes = []
    def send(*args, **kwargs):
        modes.append(kwargs["mode"])
        return {"ok": True, "mode": "dryrun", "status": "dryrun", "delivered": False}
    monkeypatch.setattr(iterate, "send_text", send)
    result = iterate.run_round(notify_mode="dryrun")
    assert modes == ["dryrun", "dryrun"]
    assert [step for step in result["steps"] if step["name"] == "pmNudge"][0]["pushed"] is False


def test_disabled_notifier_does_not_read_credentials_or_construct_transport(monkeypatch):
    monkeypatch.setattr(feishu_notify, "load_feishu_env", lambda: pytest.fail("disabled mode reads no credentials"))
    result = feishu_notify.send_text("x", mode="disabled")
    assert result == {"ok": True, "mode": "disabled", "status": "disabled", "delivered": False}


def test_dryrun_file_has_mode_and_safe_name(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_notify, "OUTBOX", tmp_path)
    monkeypatch.setattr(feishu_notify, "load_feishu_env", lambda: {})
    result = feishu_notify.send_text("hello", mode="dryrun", title="../../outside")
    path = Path(result["file"])
    assert path.parent == tmp_path
    assert "mode: dryrun" in path.read_text(encoding="utf-8")
    assert result["delivered"] is False


def test_live_failure_does_not_retry_through_direct_transport(monkeypatch):
    monkeypatch.setattr(feishu_notify, "load_feishu_env", lambda: {"FEISHU_OWNER_CHAT_ID": "oc_target"})
    monkeypatch.setattr(feishu_notify, "feishu_live", lambda env: True)
    calls = []
    def ambiguous(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("unknown receipt")
    monkeypatch.setattr(feishu_notify, "_send_via_outbox", ambiguous)
    result = feishu_notify.send_text("hello", mode="live")
    assert calls == [True]
    assert result["status"] == "delivery_unknown"
    assert result["ok"] is False and result["delivered"] is False


def test_process_identity_is_available_without_tasklist():
    assert process_marker(os.getpid())


def test_two_threads_share_single_round_capacity(tmp_path):
    path = tmp_path / "round.sqlite3"
    barrier = threading.Barrier(8)
    def acquire(_):
        barrier.wait()
        return RoundLease.acquire(path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(acquire, range(8)))
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    assert winners[0].release()


def test_two_processes_cannot_claim_the_same_capacity(tmp_path):
    path = tmp_path / "round.sqlite3"
    # 持有者等显式释放信号，而不是固定 sleep：慢机器上解释器冷启动可能超过
    # sleep 窗口，持有者先退出、竞争者合法接管（死进程接管是产品语义），
    # 回归就退化成时序抽奖（2026-11-19 门禁两轮都踩中）。信号文件让
    # "持有期间竞争者必须 locked" 变成确定性断言，断言本身不变。
    holder_code = (
        "from pathlib import Path; from integration.round_lease import RoundLease; import sys,time\n"
        "lease=RoundLease.acquire(Path(sys.argv[1])); print('claimed' if lease else 'locked',flush=True)\n"
        "release=Path(sys.argv[2]); deadline=time.monotonic()+10\n"
        "while lease and not release.is_file() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
    )
    contender_code = (
        "from pathlib import Path; from integration.round_lease import RoundLease; import sys; "
        "lease=RoundLease.acquire(Path(sys.argv[1])); print('claimed' if lease else 'locked',flush=True)"
    )
    release = tmp_path / "release.holder"
    first = subprocess.Popen([sys.executable, "-c", holder_code, str(path), str(release)], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == "claimed"
        second = subprocess.run([sys.executable, "-c", contender_code, str(path)], cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert second.stdout.strip() == "locked"
    finally:
        release.write_text("release", encoding="ascii")
        first.wait(timeout=10)
    # Dead-worker takeover ignores the unexpired monotonic deadline.
    lease = RoundLease.acquire(path)
    assert lease is not None
    lease.release()


def test_expired_worker_cannot_release_new_generation(tmp_path):
    path = tmp_path / "round.sqlite3"
    old = RoundLease.acquire(path)
    assert old
    with sqlite3.connect(path) as db:
        db.execute("UPDATE lease SET deadline=0")
    new = RoundLease.acquire(path)
    assert new and new.generation > old.generation
    assert old.release() is False
    assert new.current()
    assert old.renew() is False
    new.release()


def test_wall_clock_change_does_not_expire_active_lease(tmp_path, monkeypatch):
    lease = RoundLease.acquire(tmp_path / "round.sqlite3")
    monkeypatch.setattr(time, "time", lambda: -10**12)
    assert lease.current()
    assert lease.renew()
    lease.release()


def test_exception_releases_and_records_health(controller):
    def crash():
        raise RuntimeError("intentional offline probe")
    with pytest.raises(RuntimeError):
        iterate._run_exclusive(crash)
    assert iterate._acquire_lock()
    iterate._release_lock()
    health = [json.loads(path.read_text(encoding="utf-8")) for path in (controller / "reports").glob("health-*.json")]
    assert any(row["state"] == "failed" and row["reason"] == "RuntimeError" for row in health)


def test_real_scheduler_executes_two_offline_rounds_without_outer_lock(controller, monkeypatch):
    calls = []
    completed = threading.Event()
    scheduler = iterate.build_scheduler(0.05 / 3600, 8, 35, skip_notify=True)
    def round_body(**kwargs):
        calls.append(kwargs)
        if len(calls) >= 2:
            completed.set()
        return {"ok": True}
    monkeypatch.setattr(iterate, "run_round", round_body)
    def stop_scheduler():
        completed.wait(3)
        if scheduler.running:
            scheduler.shutdown(wait=True)
    timer = threading.Thread(target=stop_scheduler, daemon=True)
    timer.start()
    try:
        scheduler.start()
    finally:
        timer.join(timeout=5)
    assert len(calls) >= 2
    assert all(row["skip_notify"] for row in calls)
    assert iterate._acquire_lock()
    iterate._release_lock()
