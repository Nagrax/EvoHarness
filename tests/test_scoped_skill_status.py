"""DE-06 作用域状态：A 空间试用状态不污染 B 空间/base，fork 门按域裁决。"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import agents.skill_status as status
import agents.skill_tiers as tiers

SCOPE_A = {"tenant": "t1", "space": "s1", "owner": "alice"}
SCOPE_B = {"tenant": "t2", "space": "s2", "owner": "bob"}


@pytest.fixture()
def evolution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "skill-evolution"
    (directory / "online-eval").mkdir(parents=True)
    (directory / "online-eval" / "status_map.json").write_text(
        json.dumps({"statuses": {"deploy": "healthy"}}), encoding="utf-8")
    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(directory))
    status._cached_status_map = None
    status._cached_status_mtime = -1.0
    tiers.reset_tier_cache()
    tiers.set_tier("deploy", "core", trigger="test")
    yield directory
    status._cached_status_map = None
    tiers.reset_tier_cache()


def test_scoped_status_overrides_only_its_scope(evolution):
    assert status.get_effective_status_map()["deploy"] == "healthy"
    assert status.set_status_scoped("deploy", "incubating", scope=SCOPE_A,
                                    reason="trial")["ok"] is True
    assert status.get_status_scoped_map(SCOPE_A)["deploy"] == "incubating"
    assert status.get_status_scoped_map(SCOPE_B)["deploy"] == "healthy"
    assert status.get_effective_status_map()["deploy"] == "healthy"


def test_scoped_overlay_accessor_exposes_only_its_scope(evolution):
    """EVO-01：作用域视图读取端只用覆盖层条目，base 状态不混入（无 base 回落）。"""
    status.set_status_scoped("deploy", "incubating", scope=SCOPE_A)
    assert status.get_status_scoped_overlay(SCOPE_A) == {"deploy": "incubating"}
    assert status.get_status_scoped_overlay(SCOPE_B) == {}
    assert status.get_status_scoped_overlay(None) == {}
    # base 表里同名 skill 的 healthy 不得经覆盖层读取端外泄
    assert status.get_effective_status_map()["deploy"] == "healthy"
    assert status.get_status_scoped_overlay(SCOPE_A) == {"deploy": "incubating"}


def test_fork_gate_uses_scoped_status(evolution):
    status.set_status_scoped("deploy", "unobserved", scope=SCOPE_A)
    assert "trial period" in tiers.fork_blocked_reason("deploy", scope=SCOPE_A)
    assert tiers.fork_blocked_reason("deploy", scope=SCOPE_B) == ""


def test_scoped_status_requires_scope_and_audits(evolution):
    assert status.set_status_scoped("deploy", "incubating", scope={})["error"] \
        == "scope_required"
    status.set_status_scoped("deploy", "watch", scope=SCOPE_A, actor="alice")
    last = json.loads((evolution / "skill_status_scoped_audit.jsonl").read_text(
        encoding="utf-8").splitlines()[-1])
    assert last["scope"] == {"tenant": "t1", "space": "s1", "owner": "alice"}
    assert last["status"] == "watch"
