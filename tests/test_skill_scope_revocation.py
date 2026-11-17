"""DE-06 撤权同版本失效：显式 revoked/restored 事件 + 搜索就绪守卫。"""
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evoharness.knowledge import skill_scope_events as scope
from evoharness.models import Base


@pytest.fixture()
def factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        engine.dispose()


def _revoke(session, **overrides):
    kwargs = {"skill_name": "deploy", "tenant": "t1", "space": "s1",
              "action": "revoked", "owner": "alice", "source_revision": 3,
              "actor": "admin", "reason": "source revoked"}
    kwargs.update(overrides)
    return scope.record_scope_event(session, **kwargs)


def test_latest_event_wins_and_restore_clears_denial(factory):
    with factory.begin() as session:
        _revoke(session)
        assert scope.scope_denial(session, skill_name="deploy", tenant="t1",
                                  space="s1", owner="alice")["error"] == "skill_scope_revoked"
        _revoke(session, action="restored", reason="restored")
        assert scope.scope_denial(session, skill_name="deploy", tenant="t1",
                                  space="s1", owner="alice") is None


def test_denial_respects_scope_and_revision(factory):
    with factory.begin() as session:
        _revoke(session, source_revision=5)
        assert scope.scope_denial(session, skill_name="deploy", tenant="t1",
                                  space="s1", revision=5) is not None
        assert scope.scope_denial(session, skill_name="deploy", tenant="t1",
                                  space="s1", revision=6) is None
        assert scope.scope_denial(session, skill_name="deploy", tenant="t2",
                                  space="s1") is None
        assert scope.scope_denial(session, skill_name="other", tenant="t1",
                                  space="s1") is None


def test_guard_factory_denies_with_manifest_scope(factory):
    with factory.begin() as session:
        _revoke(session)
    guard = scope.make_scope_guard(factory)
    manifest = SimpleNamespace(skill_name="deploy", tenant="t1", space="s1")
    denial = guard(manifest)
    assert denial["error"] == "skill_scope_revoked"
    assert denial["skill"] == "deploy" and denial["source_revision"] == 3
    assert guard(SimpleNamespace(skill_name="deploy", tenant="t2", space="s1")) is None


def test_search_readiness_checks_scope_guard():
    from agents.skill_search.controller import (REQUIRED_PREREQUISITES,
                                                SearchController)
    controller = SearchController.__new__(SearchController)
    controller.config = SimpleNamespace(
        prerequisites={key: True for key in REQUIRED_PREREQUISITES})
    controller.manifest = SimpleNamespace(
        protocol={"splits": {"fingerprint": "fp"}},
        verify_integrity=lambda: {"ok": True})
    controller.freeze = SimpleNamespace(fingerprint="fp", roles={"S": ["case-1"]})
    controller.scope_guard = lambda manifest: {"error": "skill_scope_revoked",
                                               "skill": "deploy"}
    ready = controller.readiness()
    assert ready["ok"] is False and ready["error"] == "skill_scope_revoked"
    controller.scope_guard = None
    assert controller.readiness()["ok"] is True


def test_event_validation(factory):
    with factory.begin() as session:
        with pytest.raises(scope.SkillScopeError, match="skill_name_required"):
            scope.record_scope_event(session, skill_name="", tenant="t",
                                     space="s", action="revoked")
        with pytest.raises(scope.SkillScopeError, match="invalid_scope_action"):
            scope.record_scope_event(session, skill_name="x", tenant="t",
                                     space="s", action="deleted")
