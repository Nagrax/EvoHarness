"""skill_tiers 可见性回归（2026-10-29 判读 agents/skill_tiers.py）。

两处判读结论：
- 132 `_record_tier_fault` 自身失败 = **诊断通道自己吞故障**（元故障），
  不能再调自己（递归），降级到 stderr 一行。
- 457 `_maybe_auto_rollback` 失败 = 该退的技能还在预发区，错误必须进返回值。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SRC = REPO / "agents" / "skill_tiers.py"


def test_tier_fault_falls_back_to_stderr_not_recursion():
    text = SRC.read_text(encoding="utf-8")
    assert "[tier-fault] cannot record" in text, "诊断失败必须留痕"
    idx = text.find("[tier-fault] cannot record")
    window = text[max(0, idx - 500): idx + 300]
    # 不得在 except 里再调 _record_tier_fault（会递归）
    assert "_record_tier_fault(" not in window.split("except Exception as error")[-1], (
        "诊断自身失败时不能再调自己（递归）")
    assert "stderr.write" in window


def test_tier_fault_inner_failure_does_not_raise():
    """记录故障失败不得让调用方失败（诊断不能反噬主流程）。"""
    text = SRC.read_text(encoding="utf-8")
    idx = text.find("[tier-fault] cannot record")
    tail = text[idx: idx + 500]
    assert "raise" not in tail.split("pass")[0]


def test_rollback_errors_surface_in_return():
    text = SRC.read_text(encoding="utf-8")
    assert '"rollback_errors": rollback_errors' in text, "回退失败必须进返回值"
    assert 'rollback_errors: list[dict[str, Any]] = []' in text, "必须初始化列表"
    assert "rollback_errors.append({" in text


def test_rollback_failure_keeps_loop_going():
    """回退失败不能让整个评估循环中断（fail-open 保留）。"""
    text = SRC.read_text(encoding="utf-8")
    idx = text.find("rollback_errors.append({")
    tail = text[idx: idx + 400]
    assert "continue" in tail or "if get_tier(name)" in tail, (
        "记录后应继续处理后续技能")
