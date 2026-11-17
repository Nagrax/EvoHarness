"""DE-05 产品库 journal 管线适配器：事件+游标同事务、pending 回执、源变更。"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.models import Base
from evoharness.work import intake_journal


@pytest.fixture()
def factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        engine.dispose()


def _msg(mid, *, create_time=1, row_id=1, revision=1, recalled=False):
    return {"message_id": mid, "id": row_id, "create_time": create_time,
            "revision": revision, "recalled": recalled}


def test_requires_explicit_journal_write_permission(factory):
    with pytest.raises(PermissionError):
        intake_journal.ProductIntakeJournal(factory)


def test_enqueue_commits_events_and_cursor_together(factory):
    journal = intake_journal.ProductIntakeJournal(factory, allow_write=True)
    journal.enqueue("scope", _msg("m-1", create_time=10, row_id=1),
                    "evt-1", {"logical_key": "l-1"})
    journal.enqueue("scope", _msg("m-2", create_time=11, row_id=2),
                    "evt-2", {"logical_key": "l-2"})
    pending = journal.pending("scope")
    assert [row["event_key"] for row in pending] == ["evt-1", "evt-2"]
    assert json.loads(pending[0]["payload_json"]) == {"logical_key": "l-1"}
    assert journal.cursor("scope") == (11, 2)


def test_mark_projected_and_source_changed_clear_pending(factory):
    journal = intake_journal.ProductIntakeJournal(factory, allow_write=True)
    journal.enqueue("scope", _msg("m-1"), "evt-1", {"logical_key": "l-1"})
    journal.enqueue("scope", _msg("m-2", create_time=2, row_id=2), "evt-2",
                    {"logical_key": "l-2"})
    assert journal.mark_projected("evt-1", "item-1") is True
    assert journal.mark_source_changed("evt-2") is True
    assert journal.pending("scope") == []
    with factory() as session:
        first = session.get(intake_journal.IntakeEvent, "evt-1")
        second = session.get(intake_journal.IntakeEvent, "evt-2")
        assert first.state == "projected" and first.projected_ref == "item-1"
        assert second.state == "source_changed"


def test_replay_after_crash_does_not_duplicate_events(factory):
    journal = intake_journal.ProductIntakeJournal(factory, allow_write=True)
    journal.enqueue("scope", _msg("m-1"), "evt-1", {"logical_key": "l-1"})
    journal.enqueue("scope", _msg("m-1"), "evt-1", {"logical_key": "l-1"})
    with factory() as session:
        rows = list(session.scalars(select(intake_journal.IntakeEvent)))
    assert len(rows) == 1 and rows[0].state == "pending"


def test_reset_cursor(factory):
    journal = intake_journal.ProductIntakeJournal(factory, allow_write=True)
    journal.enqueue("scope", _msg("m-1", create_time=5, row_id=5), "evt-1", {})
    assert journal.cursor("scope") == (5, 5)
    assert journal.reset_cursor("scope") is True
    assert journal.cursor("scope") is None


def test_task_intake_exposes_product_journal_hook():
    source = (Path(__file__).resolve().parents[1] / "integration" /
              "task_intake.py").read_text(encoding="utf-8")
    assert "journal_factory=None" in source
    assert "journal_factory(scope.key)" in source
    assert "project_pending_product" in source


def test_iterate_product_journal_factory_off_by_default(monkeypatch):
    monkeypatch.delenv("EVOHARNESS_INTAKE_PRODUCT_JOURNAL", raising=False)
    from integration import iterate
    assert iterate._intake_journal_factory() is None


def test_iterate_product_journal_factory_opt_in(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    db = tmp_path / "iterate-intake.db"
    engine = create_engine("sqlite:///" + db.as_posix(), future=True)
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("EVOHARNESS_INTAKE_PRODUCT_JOURNAL", "1")
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL", "sqlite:///" + db.as_posix())
    from integration import iterate
    factory = iterate._intake_journal_factory()
    assert factory is not None
    try:
        journal = factory("scope")
        journal.enqueue("scope", _msg("m-1", create_time=9, row_id=9),
                        "evt-1", {"logical_key": "l-1"})
        assert journal.cursor("scope") == (9, 9)
        assert journal.pending("scope")[0]["event_key"] == "evt-1"
    finally:
        factory.app.engine.dispose()
