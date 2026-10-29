"""成本结构回归：side_query 开销必须单列并可算占比。

为什么重要：ROI 讨论的核心是"进化自身开销能否被收益覆盖"。
只看总 tokens 会把 side_query（进化调用）与主线调用混在一起，
导致"进化很便宜"或"进化很贵"都说不清。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.batch_results import load_batch

CUTOFF = "2026-10-29T01:30"


def _batch() -> dict:
    return load_batch(since=CUTOFF)


def test_summary_reports_side_query_and_overhead():
    result = _batch()
    if not result.get("ok"):
        return
    for arm, summary in result["arms"].items():
        assert "sideQueryM" in summary, f"{arm} 缺 side_query 单列"
        assert "evolutionOverheadPct" in summary, f"{arm} 缺开销占比"


def test_no_evolution_has_zero_overhead():
    """基线臂不进化 → side_query 必须为 0、占比 0（否则说明串了数据）。"""
    result = _batch()
    if not result.get("ok"):
        return
    baseline = result["arms"].get("no_evolution")
    if baseline is None:
        return
    assert baseline["sideQueryM"] == 0, f"基线臂不该有进化开销：{baseline['sideQueryM']}"
    assert baseline["evolutionOverheadPct"] == 0.0


def test_evolving_arms_have_positive_overhead():
    result = _batch()
    if not result.get("ok"):
        return
    for arm in ("ungated_evolution", "gated_evolution"):
        summary = result["arms"].get(arm)
        if summary is None:
            continue
        assert summary["sideQueryM"] > 0, f"{arm} 声明在进化却没有 side_query 开销"
        assert 0 < summary["evolutionOverheadPct"] < 100


def test_overhead_math_is_consistent():
    result = _batch()
    if not result.get("ok"):
        return
    for arm, summary in result["arms"].items():
        if not summary["tokensM"]:
            continue
        expected = round(summary["sideQueryM"] / summary["tokensM"] * 100, 2)
        assert abs(summary["evolutionOverheadPct"] - expected) < 0.02, (
            f"{arm} 占比算错：{summary['evolutionOverheadPct']} vs {expected}")
