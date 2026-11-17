"""DE-06 受域加载：skill 名称解析处拒绝跨空间技能，Agent 工具传递作用域。"""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import agents.skills as skills
from agents.skill_scope import ScopedSkillRef, SkillScopeError


def _skill(name, *, scope=None, prompt="p"):
    return skills.SkillDefinition(name=name, description="d",
                                  prompt_template=prompt, scope=scope)


@pytest.fixture()
def registry(monkeypatch):
    skills._cached_skills = [
        _skill("legacy"),
        _skill("deploy", scope={"tenant": "t1", "space": "s1", "owner": "alice"},
               prompt="scoped-A"),
        _skill("deploy", scope={"tenant": "t2", "space": "s2", "owner": "bob"},
               prompt="scoped-B"),
    ]
    yield
    skills._cached_skills = None


def test_scoped_lookup_selects_matching_scope(registry):
    ref = ScopedSkillRef(name="deploy", tenant="t1", space="s1", owner="alice")
    assert skills.get_skill_by_name("deploy", scope_ref=ref).prompt_template == "scoped-A"
    other = ScopedSkillRef(name="deploy", tenant="t2", space="s2", owner="bob")
    assert skills.get_skill_by_name("deploy", scope_ref=other).prompt_template == "scoped-B"


def test_cross_scope_and_unscoped_are_rejected(registry):
    mismatch = ScopedSkillRef(name="deploy", tenant="t3", space="s1", owner="alice")
    with pytest.raises(SkillScopeError, match="skill_scope_mismatch:tenant"):
        skills.get_skill_by_name("deploy", scope_ref=mismatch)
    legacy_ref = ScopedSkillRef(name="legacy", tenant="t1", space="s1", owner="alice")
    with pytest.raises(SkillScopeError, match="skill_scope_unbound"):
        skills.get_skill_by_name("legacy", scope_ref=legacy_ref)


def test_unscoped_callers_keep_legacy_behavior(registry):
    assert skills.get_skill_by_name("deploy").prompt_template in {"scoped-A", "scoped-B"}
    assert skills.get_skill_by_name("legacy") is not None


def test_execute_skill_passes_scope(registry):
    ref = ScopedSkillRef(name="deploy", tenant="t1", space="s1", owner="alice")
    result = skills.execute_skill("deploy", "args", scope_ref=ref)
    assert result and result["prompt"] == "scoped-A"
    with pytest.raises(SkillScopeError):
        skills.execute_skill(
            "deploy", "args",
            scope_ref=ScopedSkillRef(name="deploy", tenant="t9", space="s1",
                                     owner="alice"))


def test_agent_skill_tool_forwards_scope(monkeypatch):
    from agents.agent import Agent
    captured = {}

    def fake_execute(name, args, *, scope_ref=None):
        captured["name"] = name
        captured["ref"] = scope_ref
        return {"prompt": "ok", "allowed_tools": [], "context": "inline",
                "source": "project", "skill_dir": ""}

    monkeypatch.setattr(skills, "execute_skill", fake_execute)
    agent = Agent(api_base="http://127.0.0.1:9/v1", api_key="synthetic",
                  sdk_client=SimpleNamespace())
    agent.skill_scope = {"tenant": "t1", "space": "s1", "owner": "alice"}
    out = asyncio.run(agent._execute_skill_tool({"skill_name": "deploy",
                                                 "args": "x"}))
    assert "[Skill" in out
    ref = captured["ref"]
    assert (ref.tenant, ref.space, ref.owner, ref.name) == \
        ("t1", "s1", "alice", "deploy")


def test_agent_skill_tool_reports_scope_refusal(monkeypatch):
    from agents.agent import Agent

    def fake_execute(name, args, *, scope_ref=None):
        raise SkillScopeError("skill_scope_mismatch:space")

    monkeypatch.setattr(skills, "execute_skill", fake_execute)
    agent = Agent(api_base="http://127.0.0.1:9/v1", api_key="synthetic",
                  sdk_client=SimpleNamespace())
    agent.skill_scope = {"tenant": "t1", "space": "s1", "owner": "alice"}
    out = asyncio.run(agent._execute_skill_tool({"skill_name": "deploy"}))
    assert "refused" in out and "skill_scope_mismatch:space" in out
