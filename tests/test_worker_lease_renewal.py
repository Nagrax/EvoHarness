"""DE-08：单实例租约独立续期，续期失败停止派工（离线、确定性）。

长 Run 执行期间由心跳线程续租；续租失败（另一实例可能已接管）立即停止
派工并返回 single_active_lost，不允许两个写者并行。
"""
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.work.worker import WorkWorker, WorkerConfig


class _UnusedAdapter:
    pass


def _worker(tmp_path, **config):
    app = Application.open("sqlite:///" + (tmp_path / "lease.db").as_posix())
    api = WorkApi(app, artifacts_dir=tmp_path / "artifacts")
    base = {"single_active_lease_seconds": 0.3, "poll_interval_seconds": 0.05}
    base.update(config)
    return WorkWorker(app.session_factory, _UnusedAdapter(),
                      api._authorize_adapter(), config=WorkerConfig(**base))


def test_daemon_stops_dispatch_when_renewal_fails(tmp_path):
    worker = _worker(tmp_path)
    calls = {"run_once": 0}

    def _run_once(*, single_active=True):
        calls["run_once"] += 1
        time.sleep(0.2)   # 超过一个续租周期：本轮结束后必须观察到失锁
        return {}

    worker.run_once = _run_once
    worker.renew_single_active = lambda: False
    reason = worker.run_forever()
    assert reason == "single_active_lost"
    assert calls["run_once"] == 1, "续租失败后不得继续派工"
    assert worker._single_holder is None, "退出时释放单实例锁"


def test_daemon_renews_during_long_round_then_stops(tmp_path):
    worker = _worker(tmp_path)
    renewals = {"count": 0}

    def _run_once(*, single_active=True):
        time.sleep(0.4)   # 超过一个租约周期，模拟长 Run
        return {}

    def _renew():
        renewals["count"] += 1
        return True

    worker.run_once = _run_once
    worker.renew_single_active = _renew
    stop = threading.Event()
    timer = threading.Timer(0.95, stop.set)
    timer.start()
    reason = worker.run_forever(stop_event=stop)
    timer.cancel()
    assert reason == "stopped_gracefully"
    assert renewals["count"] >= 2, "长 Run 期间必须持续续租单实例锁"
    assert worker._single_holder is None
