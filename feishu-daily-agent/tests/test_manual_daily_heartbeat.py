"""F08 回归：手动日报（main.py daily / run_manual_daily）必须写 daily 心跳。"""
import pytest

from app.domain import health as health_module


def test_manual_daily_ok_writes_heartbeat(monkeypatch):
    import main as main_module

    written = []
    monkeypatch.setattr(main_module, "run_daily_pipeline", lambda: {"synced": 2, "processed": 1})
    monkeypatch.setattr(main_module.health, "heartbeat", lambda name, payload: written.append((name, payload)))

    summary = main_module.run_manual_daily()

    assert summary == {"synced": 2, "processed": 1}
    assert written == [("daily", {"status": "ok", "summary": {"synced": 2, "processed": 1}, "trigger": "manual"})]


def test_manual_daily_failure_writes_error_heartbeat(monkeypatch):
    import main as main_module

    written = []

    def boom():
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(main_module, "run_daily_pipeline", boom)
    monkeypatch.setattr(main_module.health, "heartbeat", lambda name, payload: written.append((name, payload)))

    with pytest.raises(RuntimeError):
        main_module.run_manual_daily()

    assert len(written) == 1
    assert written[0][0] == "daily"
    assert written[0][1]["status"] == "error"
    assert "pipeline exploded" in written[0][1]["error"]
    assert written[0][1]["trigger"] == "manual"
