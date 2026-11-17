"""DE-15 iterate 子进程真实用量上报与整轮结算回归（离线）。

覆盖：sidecar 累加/无环境变量 no-op、_run 捕获子进程 usage、子进程未上报
返回 None、整轮按上报 tokens 结算（recorded）与无上报时保守归集（unknown）。
"""
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "integration"))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.governance import budget
from evoharness.governance.models import BudgetReservation
from evoharness.governance.usage_sidecar import (USAGE_SIDECAR_ENV, read_usage,
                                                 write_usage)
from evoharness.models import Base


def test_sidecar_accumulates(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    monkeypatch.setenv(USAGE_SIDECAR_ENV, str(path))
    write_usage(10)
    write_usage(5, calls=2)
    assert read_usage(path) == {"tokens": 15, "calls": 3}
    assert read_usage(tmp_path / "missing.json") is None


def test_write_without_env_is_noop(monkeypatch):
    monkeypatch.delenv(USAGE_SIDECAR_ENV, raising=False)
    write_usage(10)


def test_iterate_run_captures_child_usage():
    from integration import iterate
    code = ("from evoharness.governance.usage_sidecar import write_usage; "
            "write_usage(42, 2)")
    result = iterate._run(["-c", code], timeout=60)
    assert result["exitCode"] == 0
    assert result["usage"] == {"tokens": 42, "calls": 2}


def test_iterate_run_without_report_has_none_usage():
    from integration import iterate
    result = iterate._run(["-c", "print('no model call')"], timeout=60)
    assert result["exitCode"] == 0
    assert result["usage"] is None


@pytest.fixture()
def round_db(tmp_path, monkeypatch):
    db = tmp_path / "round.db"
    engine = create_engine("sqlite:///" + db.as_posix(), future=True)
    import evoharness.governance.models  # noqa: F401
    import evoharness.work.models  # noqa: F401
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org",
                         limit_tokens=10000)
    monkeypatch.setenv("EVOHARNESS_LOOP_BUDGET", "1")
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL", "sqlite:///" + db.as_posix())
    monkeypatch.setenv("EVOHARNESS_LOOP_BUDGET_ORG", "org")
    try:
        yield factory
    finally:
        engine.dispose()


def _row(factory):
    with factory() as session:
        return session.scalar(select(BudgetReservation))


def test_round_budget_settles_reported_usage(round_db):
    from integration import iterate
    handle = iterate._round_budget_reserve()
    assert handle and not handle.get("blocked") and not handle.get("disabled")
    iterate._round_budget_settle(handle, used_tokens=123)
    row = _row(round_db)
    assert row.status == "settled" and row.used_tokens == 123
    assert row.usage_status == "recorded"


def test_round_budget_conservative_when_no_report(round_db):
    from integration import iterate
    handle = iterate._round_budget_reserve()
    reserved = handle["record"].reserved_tokens
    iterate._round_budget_settle(handle, used_tokens=None)
    row = _row(round_db)
    assert row.status == "settled"
    assert row.used_tokens == reserved and row.usage_status == "unknown"
