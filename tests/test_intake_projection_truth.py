"""DE-05 投影真源：产品库 intake_projections 幂等记录，旧 ledger 可按原 L 号重建。"""
from pathlib import Path
import json
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "eval"))
sys.path.insert(0, str(REPO / "integration"))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.models import Base
from evoharness.work import intake_journal

PAYLOAD = {"logical_key": "feishu-human:abc", "title": "飞书任务 x",
           "detail": {"sender": "alice", "message_id": "m-1"}}
EVENT = {"event_key": "evt-1", "scope_key": "scope", "message_id": "m-1",
         "revision": 1, "action": "upsert",
         "payload_json": json.dumps(PAYLOAD, ensure_ascii=False)}


@pytest.fixture()
def factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        engine.dispose()


def test_record_projection_is_idempotent_and_recall_marks_state(factory):
    with factory.begin() as session:
        first = intake_journal.record_projection(session, event=EVENT,
                                                 item_id="L0007")
        assert first.logical_key == "feishu-human:abc"
        assert first.item_id == "L0007" and first.state == "active"
        changed = dict(EVENT, payload_json=json.dumps(
            dict(PAYLOAD, title="飞书任务 y"), ensure_ascii=False))
        intake_journal.record_projection(session, event=changed, item_id="L0007")
        rows = intake_journal.projections(session)
        assert len(rows) == 1 and rows[0].title == "飞书任务 y"
        recalled = dict(EVENT, action="recalled")
        intake_journal.record_projection(session, event=recalled, item_id="L0007")
        assert intake_journal.projections(session)[0].state == "recalled"


def test_record_and_mark_commits_projection_and_receipt(factory):
    journal = intake_journal.ProductIntakeJournal(factory, allow_write=True)
    journal.enqueue("scope", {"message_id": "m-1", "id": 1, "create_time": 1,
                              "revision": 1, "recalled": False},
                    "evt-1", PAYLOAD)
    journal.record_and_mark(dict(EVENT), "L0007")
    assert journal.pending("scope") == []
    with factory() as session:
        event = session.get(intake_journal.IntakeEvent, "evt-1")
        projection = session.get(intake_journal.IntakeProjection,
                                 "feishu-human:abc")
        assert event.state == "projected" and event.projected_ref == "L0007"
        assert projection.item_id == "L0007"


def test_rebuild_ledger_passes_stored_item_ids(factory):
    with factory.begin() as session:
        intake_journal.record_projection(session, event=EVENT, item_id="L0007")
        second = dict(EVENT, event_key="evt-2", payload_json=json.dumps(
            {"logical_key": "feishu-human:def", "title": "t2",
             "detail": {"sender": "bob", "message_id": "m-2"}},
            ensure_ascii=False))
        intake_journal.record_projection(session, event=second, item_id="L0008")
        calls = []

        def fake_project(event, *, forced_item_id=""):
            calls.append((event["event_key"], forced_item_id))
            return forced_item_id, False

        out = intake_journal.rebuild_ledger(session, project=fake_project)
        assert out["rebuilt"] == 2
        assert calls == [("evt-1", "L0007"), ("evt-2", "L0008")]


def test_rebuild_restores_legacy_ledger_with_same_ids(tmp_path, monkeypatch, factory):
    import loop as loop_mod
    import integration.intake_store as store
    monkeypatch.setattr(loop_mod, "BACKLOG", tmp_path / "BACKLOG.json")
    monkeypatch.setattr(loop_mod, "_write_allowed", lambda path: True)

    item_id, created = store.project(dict(EVENT), loop_mod)
    assert item_id == "L0001" and created is True
    with factory.begin() as session:
        intake_journal.record_projection(session, event=EVENT, item_id=item_id)
    (tmp_path / "BACKLOG.json").unlink()          # 旧 ledger 丢失，从产品库重建

    with factory.begin() as session:
        out = intake_journal.rebuild_ledger(
            session,
            project=lambda event, forced_item_id="": store.project(
                event, loop_mod, forced_item_id=forced_item_id))
    assert out["rebuilt"] == 1
    state = loop_mod.load()
    restored = next(row for row in state["items"] if row["id"] == "L0001")
    assert restored["key"] == "feishu-human:abc"
    assert restored["title"] == "飞书任务 x"
    assert restored["detail"]["intake_event_key"] == "evt-1"


def test_forced_item_id_is_used_when_creating(tmp_path, monkeypatch):
    import loop as loop_mod
    import integration.intake_store as store
    monkeypatch.setattr(loop_mod, "BACKLOG", tmp_path / "BACKLOG.json")
    monkeypatch.setattr(loop_mod, "_write_allowed", lambda path: True)
    item_id, created = store.project(dict(EVENT), loop_mod,
                                     forced_item_id="L0420")
    assert item_id == "L0420" and created is True
