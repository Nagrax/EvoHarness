"""DE-06 作用域历史：快照/恢复按 tenant/space/owner 分目录，不串历史。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import agents.skill_evolution as evo
import agents.skill_tiers as tiers

SCOPE_A = {"tenant": "t1", "space": "s1", "owner": "alice"}
SCOPE_B = {"tenant": "t2", "space": "s2", "owner": "bob"}

SKILL = """---
name: deploy
description: d
version: 1.0.0
producer-kind: executable_sop
---

ORIGINAL BODY
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    evolution = tmp_path / "evolution"
    skills = tmp_path / "skills"
    (skills / "deploy").mkdir(parents=True)
    (skills / "deploy" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(evolution))
    monkeypatch.setenv("EVOHARNESS_SKILLS_ROOT", str(skills))
    evo.reset_usage_cache()
    yield skills
    evo.reset_usage_cache()


def test_scoped_snapshot_isolated_per_scope(env):
    result = evo.evolve_skill_file(skill_name="deploy", lesson="A lesson",
                                   scope=SCOPE_A)
    assert result["ok"] is True, result
    scoped = (env.parent / "evolution" / "history_scoped" / "t1" / "s1" /
              "alice" / "deploy.jsonl")
    assert scoped.is_file()
    assert not (env.parent / "evolution" / "history" / "deploy.jsonl").exists()
    stats_a = evo.load_skill_stats_scoped(SCOPE_A)
    stats_b = evo.load_skill_stats_scoped(SCOPE_B)
    assert stats_a["deploy"]["snapshots"] == 1
    assert "deploy" not in stats_b or not stats_b["deploy"].get("snapshots")


def test_restore_only_sees_its_scope_history(env):
    evo.evolve_skill_file(skill_name="deploy", lesson="A lesson", scope=SCOPE_A)
    missing = tiers.restore_content("deploy", scope=SCOPE_B)
    assert missing["ok"] is False and missing["error"] == "no_history_snapshot"
    restored = tiers.restore_content("deploy", scope=SCOPE_A)
    assert restored["ok"] is True, restored
    assert restored["restored_version"] == "1.0.0"
    text = (env / "deploy" / "SKILL.md").read_text(encoding="utf-8")
    assert "ORIGINAL BODY" in text


def test_legacy_unscoped_history_still_global(env):
    evo.evolve_skill_file(skill_name="deploy", lesson="legacy")
    assert (env.parent / "evolution" / "history" / "deploy.jsonl").is_file()
