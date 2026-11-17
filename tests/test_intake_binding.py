"""DE-04 根 WorkResolver：消息 → 唯一产品 Task 绑定（不猜归属）。

覆盖：来源引用命中、显式 work_item 绑定、跨租户/组织/空间拒绝、无候选/
多候选拒绝、已收口/缺验收拒绝、作用域缺失拒绝、session_factory 适配器。
"""
from pathlib import Path
from types import SimpleNamespace
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evoharness.models import Base, Task
from evoharness.work.intake_binding import (WorkBindingError,
                                           make_work_resolver,
                                           resolve_work_binding)

SCOPE = SimpleNamespace(tenant="t", org_ref="org", space_ref="space")


def _task(session, *, key="k1", refs=("m-1",), status="open", revision=2,
          org="org", space="space", acceptance=None, tenant="t"):
    task = Task(task_key=key, tenant=tenant, owner="alice", chat_id="chat",
                title="任务", action="fix", status=status,
                workflow_kind="delegated_delivery", goal="目标",
                acceptance_json=json.dumps(
                    acceptance if acceptance is not None else [{"kind": "code"}]),
                revision=revision, org_id=org, space_id=space,
                source_message_ids=json.dumps(list(refs)))
    session.add(task)
    session.flush()
    return task


@pytest.fixture()
def env():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def test_resolves_unique_task_by_source_ref(env):
    with env.begin() as session:
        task = _task(session)
        binding = resolve_work_binding(session, {"message_id": "m-1"}, SCOPE)
        assert binding["work_ref"] == f"work_item:{task.id}"
        assert binding["revision"] == 2
        assert binding["acceptance_ref"] == f"work_item:{task.id}@revision:2"
        assert binding["org_id"] == "org" and binding["space_id"] == "space"


def test_resolves_by_source_id_reference(env):
    with env.begin() as session:
        task = _task(session, refs=("source:41",))
        binding = resolve_work_binding(session, {"message_id": "m-9", "source_id": 41},
                                       SCOPE)
        assert binding["task_id"] == task.id


def test_explicit_work_item_binding(env):
    with env.begin() as session:
        task = _task(session, refs=())
        binding = resolve_work_binding(session, {"task_ref": f"work_item:{task.id}"},
                                       SCOPE)
        assert binding["task_id"] == task.id
        with pytest.raises(WorkBindingError, match="invalid_work_ref"):
            resolve_work_binding(session, {"task_ref": "work_item:abc"}, SCOPE)


def test_cross_scope_bindings_are_rejected(env):
    with env.begin() as session:
        other = _task(session, key="k-other", refs=("m-2",), org="org-B")
        with pytest.raises(WorkBindingError, match="missing_work_binding"):
            resolve_work_binding(session, {"message_id": "m-2"}, SCOPE)
        with pytest.raises(WorkBindingError, match="work_binding_scope_mismatch"):
            resolve_work_binding(session, {"task_ref": f"work_item:{other.id}"}, SCOPE)
        foreign = _task(session, key="k-tenant", refs=(), tenant="t2")
        with pytest.raises(WorkBindingError, match="work_binding_scope_mismatch"):
            resolve_work_binding(session, {"task_ref": f"work_item:{foreign.id}"}, SCOPE)


def test_missing_and_ambiguous_candidates_are_rejected(env):
    with env.begin() as session:
        with pytest.raises(WorkBindingError, match="missing_work_binding"):
            resolve_work_binding(session, {"message_id": "nope"}, SCOPE)
        _task(session, key="a", refs=("m-3",))
        _task(session, key="b", refs=("m-3",))
        with pytest.raises(WorkBindingError, match="ambiguous_work_binding"):
            resolve_work_binding(session, {"message_id": "m-3"}, SCOPE)


def test_closed_or_incomplete_tasks_are_rejected(env):
    with env.begin() as session:
        _task(session, key="done", refs=("m-4",), status="done")
        with pytest.raises(WorkBindingError, match="work_item_closed"):
            resolve_work_binding(session, {"message_id": "m-4"}, SCOPE)
        _task(session, key="no-accept", refs=("m-5",), acceptance=[])
        with pytest.raises(WorkBindingError, match="missing_acceptance"):
            resolve_work_binding(session, {"message_id": "m-5"}, SCOPE)


def test_missing_scope_fields_are_rejected(env):
    with env.begin() as session:
        _task(session)
        with pytest.raises(WorkBindingError, match="missing_context_binding"):
            resolve_work_binding(session, {"message_id": "m-1"},
                                 SimpleNamespace(tenant="t", org_ref="", space_ref="space"))


def test_session_factory_resolver_adapter(env):
    with env.begin() as session:
        task = _task(session)
        task_id = task.id
    resolver = make_work_resolver(env)
    binding = resolver({"message_id": "m-1"}, SCOPE)
    assert binding["task_id"] == task_id
