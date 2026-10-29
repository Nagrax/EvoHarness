"""W19 ③ 回滚演练回归：坏 Skill 硬失败必须被阻断并留证。

验收要求："回滚恢复旧版本与状态，坏 Skill/RED 注入阻断有日志"。
演练用真实接口验证：预发区 + watch + 硬失败 → 自动回退实验区 + 审计日志有
trigger/actor/evidence；关闭开关时不得回退（防默认误伤）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.rollback_drill import run_drill


@pytest.fixture()
def isolated_home(monkeypatch):
    home = Path(tempfile.mkdtemp(prefix="evo-drill-test-"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("EVOHARNESS_TIER_AUTO_ROLLBACK", raising=False)
    # **cwd 也必须隔离**：get_evolution_dir() = cwd/.evoharness/skill-evolution，
    # 只隔离 HOME 会让演练把审计写进真实仓库（实测留下 20 行）。
    monkeypatch.chdir(home)
    from agents import skill_tiers

    skill_tiers.reset_tier_cache()
    yield home
    skill_tiers.reset_tier_cache()
    os.environ.pop("EVOHARNESS_TIER_AUTO_ROLLBACK", None)
    shutil.rmtree(home, ignore_errors=True)


def test_drill_passes_all_steps(isolated_home):
    result = run_drill()
    failed = [s for s in result["steps"] if not s["ok"]]
    assert result["ok"], f"演练有失败步骤：{failed}"
    assert len(result["steps"]) >= 9, f"演练步骤偏少：{len(result['steps'])}"


def test_bad_skill_is_rolled_back(isolated_home):
    result = run_drill()
    steps = {s["step"]: s for s in result["steps"]}
    assert steps["bad-rolled-back"]["ok"], steps["bad-rolled-back"]
    assert steps["bad-listed"]["ok"], "回退的技能必须出现在 rolled_back 列表"


def test_protected_zone_not_rolled_back(isolated_home):
    """实验区技能本来就在最低档，不得被"回退"到不存在的位置。"""
    result = run_drill()
    steps = {s["step"]: s for s in result["steps"]}
    assert steps["good-untouched"]["ok"]


def test_audit_trail_has_trigger_actor_evidence(isolated_home):
    """阻断必须有可核对日志——这是验收原文要求。"""
    result = run_drill()
    steps = {s["step"]: s for s in result["steps"]}
    assert steps["audit-logged"]["ok"], "回退必须写入审计日志"
    assert steps["audit-has-evidence"]["ok"], "审计必须带证据"
    assert steps["audit-actor-is-system"]["ok"], "自动回退的 actor 必须是 system"


def test_switch_off_means_no_rollback(isolated_home):
    """默认关闭 → 不得自动回退（防误伤生产技能）。"""
    result = run_drill()
    steps = {s["step"]: s for s in result["steps"]}
    assert steps["disabled-means-no-rollback"]["ok"]


def test_drill_artifact_written():
    path = REPO / "eval" / "runs" / "rollback-drill.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["steps"], "产物里必须有步骤结果"
