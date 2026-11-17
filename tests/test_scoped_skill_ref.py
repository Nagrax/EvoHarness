"""DE-06 ScopedSkillRef：跨空间同名技能不串读，缓存键/命名空间携带完整作用域。"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agents.frontmatter import format_frontmatter
from agents.skill_scope import (ScopedSkillRef, SkillScopeError,
                                resolve_scoped_skill_file, skill_scope)


def _write_skill(root: Path, name: str, *, scope: dict | None, body: str = "steps"):
    directory = root / name
    directory.mkdir(parents=True)
    meta = {"name": name, "description": "d", "context": "inline"}
    if scope is not None:
        meta["scope-json"] = json.dumps(scope, sort_keys=True, ensure_ascii=False)
    (directory / "SKILL.md").write_text(format_frontmatter(meta, body), encoding="utf-8")
    return directory


@pytest.fixture()
def skills_root(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("EVOHARNESS_SKILLS_ROOT", str(root))
    return root


def test_same_name_in_two_scopes_does_not_cross_read(skills_root):
    _write_skill(skills_root, "deploy", scope={"tenant": "t1", "space": "s1",
                                                "owner": "alice"})
    ref_a = ScopedSkillRef(name="deploy", tenant="t1", space="s1", owner="alice")
    path = resolve_scoped_skill_file("deploy", ref=ref_a, target="project")
    assert path is not None and "deploy" in str(path)
    ref_b = ScopedSkillRef(name="deploy", tenant="t2", space="s2", owner="bob")
    with pytest.raises(SkillScopeError, match="skill_scope_mismatch:tenant"):
        resolve_scoped_skill_file("deploy", ref=ref_b, target="project")


def test_unscoped_legacy_requires_explicit_local_permission(skills_root):
    _write_skill(skills_root, "legacy", scope=None)
    ref = ScopedSkillRef(name="legacy", tenant="t1", space="s1", owner="alice")
    with pytest.raises(SkillScopeError, match="skill_scope_unbound"):
        resolve_scoped_skill_file("legacy", ref=ref, target="project")
    with pytest.raises(SkillScopeError, match="skill_scope_unbound"):
        resolve_scoped_skill_file(
            "legacy", ref=ref, target="project", allow_unscoped=True)
    local = ScopedSkillRef(name="legacy", tenant="local", owner="alice")
    assert resolve_scoped_skill_file(
        "legacy", ref=local, target="project", allow_unscoped=True) is not None


def test_malformed_scope_json_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    directory = root / "broken"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: broken\ndescription: d\nscope-json: '{bad json}'\n---\nsteps\n",
        encoding="utf-8")
    monkeypatch.setenv("EVOHARNESS_SKILLS_ROOT", str(root))
    with pytest.raises(SkillScopeError, match="scope_json_invalid"):
        skill_scope(directory / "SKILL.md")


def test_cache_key_and_namespace_include_full_scope():
    base = ScopedSkillRef(name="deploy", tenant="t1", space="s1", owner="alice",
                          producer="projection", source_revision=3)
    same = ScopedSkillRef(name="deploy", tenant="t1", space="s1", owner="alice",
                          producer="projection", source_revision=3)
    other_revision = ScopedSkillRef(name="deploy", tenant="t1", space="s1",
                                    owner="alice", producer="projection",
                                    source_revision=4)
    other_space = ScopedSkillRef(name="deploy", tenant="t1", space="s2",
                                 owner="alice", producer="projection",
                                 source_revision=3)
    assert base.cache_key() == same.cache_key()
    assert base.cache_key() != other_revision.cache_key()
    assert base.cache_key() != other_space.cache_key()
    assert base.namespace() == "t1/s1/alice/projection"


def test_ref_name_must_match_requested_skill():
    with pytest.raises(SkillScopeError, match="skill_ref_name_mismatch"):
        resolve_scoped_skill_file(
            "other", ref=ScopedSkillRef(name="deploy", tenant="t1", owner="alice"))
