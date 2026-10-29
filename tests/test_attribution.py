"""M2 归因回归测试：全量 outcomes 的层×子项失败分布 + 正则前后对比。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import skill_eval_rubric as R  # noqa: E402
from eval import attribution  # noqa: E402


def test_macro_breakdown_counts_layer_and_sub_item():
    outcomes = [
        {"rule_id": "response_nonempty", "passed": True},
        {"rule_id": "json_parseable", "passed": False},
        {"rule_id": "json_parseable", "passed": False},
        {"rule_id": "must_cite_sources", "passed": True},
        {"rule_id": "no_unfounded_claims", "passed": False},
        {"rule_id": "safety_refusal", "passed": True},
    ]
    breakdown = attribution.macro_breakdown(outcomes)
    assert breakdown["total"] == 6
    assert breakdown["failed"] == 3
    cnt = breakdown["layers"][R.LAYER_CNT]
    assert cnt["total"] == 3
    assert cnt["failed"] == 2
    assert cnt["sub_items"][R.SUB_JSON]["failed"] == 2
    assert breakdown["layers"][R.LAYER_CTX]["failed"] == 0
    assert breakdown["layers"][R.LAYER_BOUND]["failed"] == 1
    assert breakdown["layers"][R.LAYER_RED]["failed"] == 0


def test_macro_breakdown_uses_full_outcomes_not_truncated_20():
    # 25 条失败全部计入（若误用 failures[:20] 会只剩 20）。
    outcomes = [{"rule_id": "json_parseable", "passed": False} for _ in range(25)]
    breakdown = attribution.macro_breakdown(outcomes)
    assert breakdown["total"] == 25
    assert breakdown["failed"] == 25
    assert breakdown["layers"][R.LAYER_CNT]["sub_items"][R.SUB_JSON]["failed"] == 25


def test_macro_breakdown_respects_explicit_layer_and_sub_item():
    outcomes = [{"rule_id": "whatever", "layer": "RED", "sub_item": "safety", "passed": False}]
    breakdown = attribution.macro_breakdown(outcomes)
    assert R.LAYER_RED in breakdown["layers"]
    assert breakdown["layers"][R.LAYER_RED]["sub_items"]["safety"]["failed"] == 1


def test_macro_breakdown_empty():
    breakdown = attribution.macro_breakdown([])
    assert breakdown["total"] == 0
    assert breakdown["failed"] == 0
    assert breakdown["pass_rate"] == 0.0
    assert breakdown["layers"] == {}


def test_reproducible_compare_counts_hits():
    before = ["answer without source", "no links here"]
    after = ["see https://a.com", "see https://b.com and https://c.com"]
    result = attribution.reproducible_compare(before, after, r"https?://\S+")
    assert result["before"] == 0
    assert result["after"] == 3
    assert result["delta"] == 3
    assert result["improved"] is True


def test_reproducible_compare_regression_and_single_string():
    result = attribution.reproducible_compare("a b c", "a", r"\b\w\b")
    assert result["before"] == 3
    assert result["after"] == 1
    assert result["delta"] == -2
    assert result["improved"] is False
