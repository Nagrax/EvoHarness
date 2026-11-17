"""DE-06 搜索入口守卫装配：撤权清理发现/usage/status 缓存；无库时离线不接。"""
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agents.skill_search import scope_guard
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


def test_cleanup_clears_all_skill_caches():
    import agents.skills as skills
    import agents.skill_evolution as evo
    import agents.skill_status as status
    skills._cached_skills = [SimpleNamespace(name="x")]
    evo._usage_index_cache = {"x": {}}
    evo._usage_index_cache_key = ("x",)
    status._cached_status_map = {"x": "healthy"}
    status._cached_certified_map = {"x": "healthy"}
    scope_guard.cleanup_skill_caches()
    assert skills._cached_skills is None
    assert evo._usage_index_cache is None and evo._usage_index_cache_key is None
    assert status._cached_status_map is None and status._cached_certified_map is None


def test_guard_denies_and_cleans_caches(factory):
    import agents.skills as skills
    with factory.begin() as session:
        scope.record_scope_event(session, skill_name="deploy", tenant="t1",
                                 space="s1", action="revoked",
                                 source_revision=1)
    guard = scope_guard.build_scope_guard(factory)
    skills._cached_skills = [SimpleNamespace(name="deploy")]
    denial = guard(SimpleNamespace(skill_name="deploy", tenant="t1", space="s1"))
    assert denial["error"] == "skill_scope_revoked"
    assert skills._cached_skills is None


def test_build_without_product_db_returns_none(monkeypatch):
    def boom():
        raise RuntimeError("database_not_configured")

    import evoharness.config as config
    monkeypatch.setattr(config, "resolve_config", boom)
    assert scope_guard.build_scope_guard() is None


def test_search_eval_wires_opt_in_guard():
    source = (Path(__file__).resolve().parents[1] / "eval" /
              "skill_search_eval.py").read_text(encoding="utf-8")
    assert "EVOHARNESS_SCOPE_GUARD" in source
    assert "build_scope_guard()" in source
    assert "scope_guard=scope_guard" in source
