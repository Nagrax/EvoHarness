"""DE-05 产品库事务内 intake：事件/游标/投影意图/业务事实同事务。

覆盖：页级原子入账、重放幂等、编辑/撤回新事件、投影异常整体回滚、
401 条跨三页恰好一次、游标只前进。全部离线。
"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from evoharness.models import Base, Task
from evoharness.work import intake_journal


def _message(mid: str, *, create_time: int = 1, row_id: int = 1,
             revision: int = 1, recalled: bool = False) -> dict:
    return {"message_id": mid, "id": row_id, "create_time": create_time,
            "revision": revision, "recalled": recalled}


def _projector(session, message, event):
    task = Task(task_key=f"intake:{message['message_id']}:{event.revision}:{int(event.recalled)}",
                tenant="t", owner="alice", chat_id="chat", title="intake",
                action="", status="open", workflow_kind="delegated_delivery",
                goal="g", acceptance_json='[{"kind":"code"}]', revision=1,
                org_id="org", space_id="space",
                source_message_ids=json.dumps([message["message_id"]]))
    session.add(task)
    session.flush()
    return f"task:{task.id}"


@pytest.fixture()
def factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        engine.dispose()


def _counts(factory) -> tuple[int, int]:
    with factory() as session:
        events = session.scalar(select(func.count(intake_journal.IntakeEvent.event_key)))
        tasks = session.scalar(select(func.count(Task.id)))
        return int(events or 0), int(tasks or 0)


def test_page_ingest_projects_tasks_and_advances_cursor(factory):
    with factory.begin() as session:
        result = intake_journal.ingest_page(
            session, scope_key="scope", messages=[
                _message("m-1", create_time=10, row_id=1),
                _message("m-2", create_time=11, row_id=2)],
            projector=_projector)
        assert len(result["recorded"]) == 2 and len(result["projected"]) == 2
        assert result["cursor"] == (11, 2)
    assert _counts(factory) == (2, 2)


def test_replay_is_idempotent(factory):
    messages = [_message("m-1", create_time=10, row_id=1)]
    with factory.begin() as session:
        intake_journal.ingest_page(session, scope_key="scope",
                                   messages=messages, projector=_projector)
    with factory.begin() as session:
        result = intake_journal.ingest_page(session, scope_key="scope",
                                            messages=messages, projector=_projector)
        assert result["recorded"] == [] and len(result["skipped"]) == 1
        assert result["cursor"] == (10, 1)
    assert _counts(factory) == (1, 1)


def test_edit_and_recall_create_new_events(factory):
    with factory.begin() as session:
        intake_journal.ingest_page(session, scope_key="scope",
                                   messages=[_message("m-1")], projector=_projector)
    with factory.begin() as session:
        intake_journal.ingest_page(
            session, scope_key="scope",
            messages=[_message("m-1", create_time=2, row_id=2, revision=2)],
            projector=_projector)
    with factory.begin() as session:
        seen = []
        intake_journal.ingest_page(
            session, scope_key="scope",
            messages=[_message("m-1", create_time=3, row_id=3, revision=2,
                               recalled=True)],
            projector=lambda s, m, e: (seen.append(e.recalled), _projector(s, m, e))[1])
        assert seen == [True]
    assert _counts(factory) == (3, 3)


def test_projection_failure_rolls_back_events_and_cursor(factory):
    class Boom(RuntimeError):
        pass

    def risky(session, message, event):
        if message["message_id"] == "m-2":
            raise Boom("projector failed")
        return _projector(session, message, event)

    with pytest.raises(Boom):
        with factory.begin() as session:
            intake_journal.ingest_page(
                session, scope_key="scope",
                messages=[_message("m-1", create_time=1, row_id=1),
                          _message("m-2", create_time=2, row_id=2)],
                projector=risky)
    assert _counts(factory) == (0, 0)
    with factory() as session:
        assert intake_journal.cursor_of(session, "scope") is None
    # 重试成功后恰好一次
    with factory.begin() as session:
        intake_journal.ingest_page(
            session, scope_key="scope",
            messages=[_message("m-1", create_time=1, row_id=1),
                      _message("m-2", create_time=2, row_id=2)],
            projector=_projector)
    assert _counts(factory) == (2, 2)


def test_401_messages_across_three_pages_exactly_once(factory):
    rows = [_message(f"m-{i}", create_time=1000 + i, row_id=i) for i in range(1, 402)]
    pages = [rows[:200], rows[200:400], rows[400:]]
    for page in pages:
        with factory.begin() as session:
            result = intake_journal.ingest_page(session, scope_key="scope",
                                                messages=page, projector=_projector)
            assert len(result["recorded"]) == len(page)
    assert _counts(factory) == (401, 401)
    with factory() as session:
        assert intake_journal.cursor_of(session, "scope") == (1401, 401)
    for page in pages:
        with factory.begin() as session:
            result = intake_journal.ingest_page(session, scope_key="scope",
                                                messages=page, projector=_projector)
            assert result["recorded"] == []
    assert _counts(factory) == (401, 401)


def test_cursor_only_moves_forward(factory):
    with factory.begin() as session:
        assert intake_journal.advance_cursor(session, "scope", created_ms=10, row_id=2)
        assert not intake_journal.advance_cursor(session, "scope", created_ms=9, row_id=9)
        assert not intake_journal.advance_cursor(session, "scope", created_ms=10, row_id=2)
        assert intake_journal.cursor_of(session, "scope") == (10, 2)
        intake_journal.reset_cursor(session, "scope")
        assert intake_journal.cursor_of(session, "scope") is None


def test_event_key_is_deterministic_and_revision_bound():
    first = intake_journal.event_key("scope", "m-1", 1)
    assert first == intake_journal.event_key("scope", "m-1", 1)
    assert first != intake_journal.event_key("scope", "m-1", 2)
    assert first != intake_journal.event_key("scope", "m-1", 1, recalled=True)


def _idempotent_projector(session, message, event):
    key = f"intake:{message['message_id']}:{event.revision}:{int(event.recalled)}"
    existing = session.scalar(select(Task).where(Task.task_key == key))
    if existing is not None:
        return f"task:{existing.id}"
    return _projector(session, message, event)


def test_two_phase_enqueue_then_replay(factory):
    messages = [_message("m-1", create_time=1, row_id=1),
                _message("m-2", create_time=2, row_id=2)]
    with factory.begin() as session:
        result = intake_journal.enqueue_page(session, scope_key="scope",
                                             messages=messages)
        assert len(result["recorded"]) == 2
        assert result["cursor"] == (2, 2)
        assert len(intake_journal.pending_events(session, "scope")) == 2
    assert _counts(factory) == (2, 0)          # 相 A 只落意图，不写业务事实
    with factory.begin() as session:
        replay = intake_journal.replay_pending(
            session, scope_key="scope", projector=_idempotent_projector)
        assert len(replay["projected"]) == 2 and replay["remaining"] == 0
    assert _counts(factory) == (2, 2)


def test_replay_after_crash_does_not_duplicate(factory):
    messages = [_message("m-1", create_time=1, row_id=1)]
    with factory.begin() as session:
        intake_journal.enqueue_page(session, scope_key="scope", messages=messages)
        intake_journal.replay_pending(session, scope_key="scope",
                                      projector=_idempotent_projector)
    # 进程在回执后崩溃前重扫同一页：事件已存在 → 跳过；再 replay 无 pending
    with factory.begin() as session:
        result = intake_journal.enqueue_page(session, scope_key="scope",
                                             messages=messages)
        assert result["recorded"] == [] and len(result["skipped"]) == 1
        assert intake_journal.pending_events(session, "scope") == []
    assert _counts(factory) == (1, 1)


def test_replay_failure_rolls_back_and_retries(factory):
    class Boom(RuntimeError):
        pass

    def flaky(session, message, event):
        if message["message_id"] == "m-2":
            raise Boom("projector failed")
        return _idempotent_projector(session, message, event)

    messages = [_message("m-1", create_time=1, row_id=1),
                _message("m-2", create_time=2, row_id=2)]
    with factory.begin() as session:
        intake_journal.enqueue_page(session, scope_key="scope", messages=messages)
    with pytest.raises(Boom):
        with factory.begin() as session:
            intake_journal.replay_pending(session, scope_key="scope",
                                          projector=flaky)
    assert _counts(factory) == (2, 0)
    with factory.begin() as session:
        replay = intake_journal.replay_pending(
            session, scope_key="scope", projector=_idempotent_projector)
        assert len(replay["projected"]) == 2
    assert _counts(factory) == (2, 2)
