"""DE-06 作用域分区：同域覆盖不影响其他空间/base tier；fork 门按作用域裁决。"""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import agents.agent as agent_mod
import agents.skills as skills
import agents.skill_tiers as tiers

SCOPE_A = {"tenant": "t1", "space": "s1", "owner": "alice"}
SCOPE_B = {"tenant": "t2", "space": "s2", "owner": "bob"}


@pytest.fixture()
def evolution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "skill-evolution"
    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(directory))
    tiers.reset_tier_cache()
    yield directory
    tiers.reset_tier_cache()


def test_scoped_tier_overlay_falls_back_to_base(evolution):
    assert tiers.set_tier("deploy", "core", trigger="test")["ok"] is True
    assert tiers.get_tier_scoped("deploy", SCOPE_A) == "core"
    assert tiers.set_tier_scoped("deploy", "experimental", scope=SCOPE_A,
                                 trigger="test")["ok"] is True
    assert tiers.get_tier_scoped("deploy", SCOPE_A) == "experimental"
    assert tiers.get_tier_scoped("deploy", SCOPE_B) == "core"
    assert tiers.get_tier("deploy") == "core"          # base 未被改动


def test_scoped_tier_scope_key_is_exact(evolution):
    tenant_space = {"tenant": "t1", "space": "s1"}
    tiers.set_tier_scoped("deploy", "staging", scope=tenant_space, trigger="test")
    assert tiers.get_tier_scoped("deploy", tenant_space) == "staging"
    assert tiers.get_tier_scoped("deploy", SCOPE_A) == "core"


def test_scoped_tier_requires_scope_and_audits(evolution):
    assert tiers.set_tier_scoped("deploy", "staging", scope={},
                                 trigger="test")["error"] == "scope_required"
    tiers.set_tier_scoped("deploy", "experimental", scope=SCOPE_A,
                          trigger="test", actor="alice")
    last = json.loads((evolution / "tier_audit.jsonl").read_text(
        encoding="utf-8").splitlines()[-1])
    assert last["scope"] == {"tenant": "t1", "space": "s1", "owner": "alice"}
    assert last["to_tier"] == "experimental"


def test_fork_gate_uses_scoped_tier(evolution, monkeypatch):
    tiers.set_tier_scoped("deploy", "experimental", scope=SCOPE_A, trigger="test")

    def fake_execute(name, args, *, scope_ref=None):
        return {"prompt": "p", "allowed_tools": [], "context": "fork",
                "source": "project", "skill_dir": ""}

    monkeypatch.setattr(skills, "execute_skill", fake_execute)
    agent = agent_mod.Agent(api_base="http://127.0.0.1:9/v1",
                            api_key="synthetic", sdk_client=SimpleNamespace())
    agent.skill_scope = SCOPE_A
    out = asyncio.run(agent._execute_skill_tool({"skill_name": "deploy"}))
    assert "deployment tier experimental (scoped)" in out
    assert "fork execution is disabled" in out

    # B 域回落到 base core：不被 A 的实验区禁令挡住
    assert tiers.fork_blocked_reason("deploy", scope=SCOPE_B) == ""
