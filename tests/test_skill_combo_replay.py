"""W11 回归：Skill 组合重放（互斥阻断）与重复四路决策追溯。

对应验收：
- 组合重放能发现互斥指令且回归 Skill 被阻断，硬失败不能被均分覆盖
- 合成重复/矛盾案例可追溯到 add/merge/revise/discard 决策
- compute_skill_conflicts 此前零测试覆盖（grep 全仓 tests/ 无一处引用），本文件补上
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "eval"))

import eval.skill_combo_replay as scr


def test_conflict_pair_detected_and_blocked():
    a, b = scr.synthetic_conflict_pair()
    replay = scr.replay([a, b])
    assert replay["hardFailures"], "互斥对（json vs table）必须被静态检测发现"
    assert set(replay["blockedFromInjection"]) == {a["name"], b["name"]}, (
        "硬失败组合必须双双阻断，不得均分覆盖")
    assert all(f["severity"] == "hard-fail" for f in replay["hardFailures"])


def test_duplicate_pair_traced_to_four_way_decision():
    a, b = scr.synthetic_duplicate_pair()
    replay = scr.replay([a, b])
    assert replay["duplicates"], "语义重复对必须被审计发现"
    for item in replay["duplicates"]:
        assert item["suggestedAction"] in {"merge", "discard-one", "revise", "add"}, (
            "建议动作必须落在四路决策语义内")
        assert "trace" in item and "add-duplicate-detected" in item["trace"]


def test_clean_pair_not_flagged():
    replay = scr.replay([
        {"name": "python-sort", "description": "排序工具", "when_to_use": "sorting python lists",
         "instructions": "用 sorted()。"},
        {"name": "git-tag", "description": "打 tag", "when_to_use": "creating git tags",
         "instructions": "语义化版本。"},
    ])
    assert not replay["hardFailures"], "无冲突的独立 skill 不得误伤"
    assert not replay["duplicates"]


def test_replay_never_touches_real_evoharness():
    """评测器必须在内存 stub 上跑，不写真实 .evoharness（实验隔离）。"""
    source = Path(scr.__file__).read_text(encoding="utf-8")
    assert "discover_skills()" not in source.replace(
        "skills = skills if skills is not None else discover_skills()", ""), (
        "replay 路径不得触发 discover_skills() 读真实技能库")
    assert ".evoharness" not in Path(scr.__file__).read_text(encoding="utf-8").split("def replay")[1].split("def ")[0], (
        "replay 主路径不得写真实部署分区")


def test_main_report_all_assertions_hold(tmp_path):
    """main 的四项断言必须全真（直接构造报告，不经 argparse）。"""
    a, b = scr.synthetic_conflict_pair()
    d1, d2 = scr.synthetic_duplicate_pair()
    conflict_replay = scr.replay([a, b])
    duplicate_replay = scr.replay([d1, d2])
    clean = scr.replay([
        {"name": "python-sort", "description": "排序工具", "when_to_use": "sorting python lists",
         "instructions": "用 sorted()。"},
        {"name": "git-tag", "description": "打 tag", "when_to_use": "creating git tags",
         "instructions": "语义化版本。"},
    ])
    report = {
        "assertions": {
            "conflictDetected": bool(conflict_replay["hardFailures"]),
            "conflictBlocked": bool(conflict_replay["blockedFromInjection"]),
            "duplicateTraced": bool(duplicate_replay["duplicates"]),
            "cleanNotFlagged": not clean["hardFailures"] and not clean["duplicates"],
        },
    }
    report["ok"] = all(report["assertions"].values())
    assert report["ok"] is True
    assert all(report["assertions"].values())
