"""DE-06 按域 usage 归集与撤权缓存清理。"""
from pathlib import Path
from types import SimpleNamespace
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import agents.skills as skills
import agents.skill_evolution as evo

SCOPE_A = {"tenant": "t1", "space": "s1", "owner": "alice"}
SCOPE_B = {"tenant": "t2", "space": "s2", "owner": "bob"}


@pytest.fixture()
def evolution_dir(tmp_path, monkeypatch):
    directory = tmp_path / "skill-evolution"
    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(directory))
    evo.reset_usage_cache()
    yield directory
    evo.reset_usage_cache()


def test_scoped_usage_isolated_between_spaces(evolution_dir):
    for _ in range(2):
        evo.record_skill_invocation(skill_name="deploy", source="project",
                                    context="inline", scope=SCOPE_A)
    evo.record_skill_invocation(skill_name="deploy", source="project",
                                context="inline", scope=SCOPE_B)
    evo.record_skill_invocation(skill_name="deploy", source="project",
                                context="inline")          # 无作用域旧事件
    evo.record_skill_feedback(skill_name="deploy", rating="useful")
    stats_a = evo.load_skill_stats_scoped(SCOPE_A)
    stats_b = evo.load_skill_stats_scoped(SCOPE_B)
    assert stats_a["deploy"]["invocations"] == 2
    assert stats_b["deploy"]["invocations"] == 1
    assert "feedback" not in stats_a["deploy"] or stats_a["deploy"]["feedback"] == 0
    # 全量聚合仍包含旧的无作用域事件
    assert evo.load_skill_stats()["deploy"]["invocations"] == 4


def test_execute_skill_records_scope_in_usage(evolution_dir, monkeypatch):
    from agents.skill_scope import ScopedSkillRef
    monkeypatch.setattr(skills, "_cached_skills", [
        skills.SkillDefinition(name="deploy", description="d",
                               prompt_template="p",
                               scope={"tenant": "t1", "space": "s1",
                                      "owner": "alice"})])
    ref = ScopedSkillRef(name="deploy", tenant="t1", space="s1", owner="alice")
    skills.execute_skill("deploy", "x", scope_ref=ref)
    line = (evolution_dir / "usage.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    row = json.loads(line)
    assert row["scope"] == {"tenant": "t1", "space": "s1", "owner": "alice",
                            "source_revision": 0}
    assert evo.load_skill_stats_scoped(SCOPE_A)["deploy"]["invocations"] == 1
    monkeypatch.setattr(skills, "_cached_skills", None)


def test_reset_usage_cache_clears_global(evolution_dir):
    evo.record_skill_invocation(skill_name="deploy", source="project",
                                context="inline", scope=SCOPE_A)
    evo.load_skill_stats()
    assert evo._usage_index_cache is not None
    evo.reset_usage_cache()
    assert evo._usage_index_cache is None and evo._usage_index_cache_key is None


def test_scope_guard_runs_denial_cleanup_callback(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from evoharness.knowledge import skill_scope_events as scope
    from evoharness.models import Base
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    calls = []
    with factory.begin() as session:
        scope.record_scope_event(session, skill_name="deploy", tenant="t1",
                                 space="s1", action="revoked",
                                 source_revision=2)
    guard = scope.make_scope_guard(factory, on_denial=calls.append)
    manifest = SimpleNamespace(skill_name="deploy", tenant="t1", space="s1")
    assert guard(manifest)["error"] == "skill_scope_revoked"
    assert len(calls) == 1 and calls[0]["skill"] == "deploy"
    with factory.begin() as session:
        scope.record_scope_event(session, skill_name="deploy", tenant="t1",
                                 space="s1", action="restored")
    assert guard(manifest) is None
    assert len(calls) == 1
    engine.dispose()
