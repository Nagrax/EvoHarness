"""M3 judge 可信度校准测试：合成判定对（零模型、零人工标注）断言手算期望值。

口径：全部输入均为合成/程序化近似，不与任何模型或人工标注耦合；
kappa 的期望值由测试内手算（比例式）给出，而非由被测函数反推。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.judge_calibration import (  # noqa: E402
    cohen_kappa,
    length_controlled_diff,
    swap_consistency,
)

GOLDEN_PATH = (
    Path(__file__).resolve().parents[1]
    / "eval"
    / "judge_calibration"
    / "golden_pairs.example.json"
)


# --------------------------------------------------------------------------- #
# cohen_kappa
# --------------------------------------------------------------------------- #
def test_kappa_empty_is_zero():
    assert cohen_kappa([]) == 0.0


def test_kappa_perfect_agreement_is_one():
    # po=1；边际 pass/fail 各 0.5 -> pe=0.5 -> kappa=(1-0.5)/(1-0.5)=1.0
    pairs = [("pass", "pass"), ("fail", "fail"), ("pass", "pass"), ("fail", "fail")]
    assert cohen_kappa(pairs) == 1.0


def test_kappa_partial_agreement_half():
    # po=3/4；边际 a=(3/4,1/4)，b=(1/2,1/2) -> pe=1/2 -> kappa=0.5
    pairs = [("pass", "pass"), ("pass", "pass"), ("fail", "fail"), ("pass", "fail")]
    assert cohen_kappa(pairs) == pytest.approx(0.5)


def test_kappa_three_samples_is_four_tenths():
    # po=2/3；边际 a=(2/3,1/3)，b=(1/3,2/3) -> pe=4/9 -> kappa=(2/9)/(5/9)=0.4
    pairs = [("x", "x"), ("x", "y"), ("y", "y")]
    assert cohen_kappa(pairs) == pytest.approx(0.4)


def test_kappa_negative_when_worse_than_chance():
    # po=1/3；边际 a=(2/3,1/3)，b=(1/3,2/3) -> pe=5/9 -> kappa=(-2/9)/(4/9)=-0.5
    pairs = [("pass", "fail"), ("fail", "pass"), ("pass", "pass")]
    assert cohen_kappa(pairs) == pytest.approx(-0.5)


def test_kappa_observed_equals_expected_is_zero():
    # po=0.5，pe=0.5 -> kappa=0.0（恰好等于机遇一致）
    pairs = [("a", "a"), ("a", "b"), ("b", "b"), ("b", "a")]
    assert cohen_kappa(pairs) == 0.0


def test_kappa_degenerate_expected_one_is_zero():
    # 全同标签：po=1 但 pe=1，1-pe=0 退化 -> 冻结为 0.0（不返回 NaN/1.0）
    assert cohen_kappa([("pass", "pass"), ("pass", "pass")]) == 0.0
    assert cohen_kappa([("fail", "fail"), ("fail", "fail"), ("fail", "fail")]) == 0.0


# --------------------------------------------------------------------------- #
# swap_consistency
# --------------------------------------------------------------------------- #
def test_swap_identical_orders_is_perfect():
    seq = ["pass", "fail", "pass", "fail"]
    out = swap_consistency(seq, list(seq))
    assert out == {"agreement": 1.0, "flip_rate": 0.0}


def test_swap_partial_flip():
    a = ["pass", "fail", "pass", "fail"]
    b = ["fail", "pass", "pass", "fail"]
    # 逐位：不同/不同/相同/相同 -> 一致 2/4，翻转 2/4
    out = swap_consistency(a, b)
    assert out == {"agreement": 0.5, "flip_rate": 0.5}


def test_swap_total_flip_binary():
    out = swap_consistency([True, True, True, True], [False, False, False, False])
    assert out == {"agreement": 0.0, "flip_rate": 1.0}


def test_swap_empty_is_zero():
    assert swap_consistency([], []) == {"agreement": 0.0, "flip_rate": 0.0}


def test_swap_length_mismatch_uses_common_prefix():
    # 只比较可配对的 2 个位置：1==1 同、2!=9 翻 -> 0.5 / 0.5
    out = swap_consistency([1, 2, 3], [1, 9])
    assert out == {"agreement": 0.5, "flip_rate": 0.5}


# --------------------------------------------------------------------------- #
# length_controlled_diff
# --------------------------------------------------------------------------- #
def test_length_control_exposes_confound():
    records = [
        {"passed": True, "length": 10, "group": "before"},
        {"passed": False, "length": 10, "group": "before"},
        {"passed": True, "length": 1000, "group": "before"},
        {"passed": True, "length": 1000, "group": "before"},
        {"passed": True, "length": 1000, "group": "before"},
        {"passed": True, "length": 10, "group": "after"},
        {"passed": True, "length": 10, "group": "after"},
        {"passed": False, "length": 1000, "group": "after"},
    ]
    out = length_controlled_diff(records)
    # raw: before=4/5=0.8，after=2/3
    # len10 层 (w=4): before 0.5 / after 1.0；len1000 层 (w=4): before 1.0 / after 0.0
    # 校正: before=(4*0.5+4*1.0)/8=0.75，after=(4*1.0+4*0.0)/8=0.5
    assert out["before_rate"] == pytest.approx(0.75)
    assert out["after_rate"] == pytest.approx(0.5)
    assert out["diff"] == pytest.approx(-0.25)
    assert out["raw_diff"] == pytest.approx(2 / 3 - 0.8)
    assert out["matched_strata"] == 2


def test_length_control_no_shared_strata_falls_back_to_raw():
    records = [
        {"passed": True, "length": 10, "group": "before"},
        {"passed": False, "length": 10, "group": "before"},
        {"passed": True, "length": 20, "group": "after"},
        {"passed": True, "length": 20, "group": "after"},
        {"passed": False, "length": 20, "group": "after"},
    ]
    out = length_controlled_diff(records)
    assert out["matched_strata"] == 0
    assert out["before_rate"] == pytest.approx(0.5)
    assert out["after_rate"] == pytest.approx(2 / 3)
    assert out["diff"] == pytest.approx(out["raw_diff"])
    assert out["diff"] == pytest.approx(1 / 6)


def test_length_control_matched_and_equal():
    records = [
        {"passed": True, "length": 5, "group": "before"},
        {"passed": True, "length": 5, "group": "after"},
        {"passed": True, "length": 7, "group": "before"},
        {"passed": False, "length": 7, "group": "after"},
    ]
    out = length_controlled_diff(records)
    # len5 (w=2): 1.0/1.0；len7 (w=2): 1.0/0.0 -> before=1.0，after=0.5，diff=-0.5
    assert out["before_rate"] == pytest.approx(1.0)
    assert out["after_rate"] == pytest.approx(0.5)
    assert out["diff"] == pytest.approx(-0.5)
    assert out["matched_strata"] == 2


def test_length_control_empty_is_zero():
    out = length_controlled_diff([])
    assert out["before_rate"] == 0.0
    assert out["after_rate"] == 0.0
    assert out["diff"] == 0.0
    assert out["raw_diff"] == 0.0
    assert out["matched_strata"] == 0


# --------------------------------------------------------------------------- #
# golden 注入 schema（程序化近似，非人标）
# --------------------------------------------------------------------------- #
def test_golden_pairs_schema_and_nonhuman_note():
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == "judge-golden-pairs/v1"
    assert data["kind"] == "golden_pairs"
    # 显式声明这是 golden/程序化近似，不是人标
    assert "非人标" in data["_note"] and "not human labels" in data["_note"].lower()
    assert data["labels"] == ["pass", "fail"]
    assert data["pairs"]
    for pair in data["pairs"]:
        assert pair["label_a"] in data["labels"]
        assert pair["label_b"] in data["labels"]
        assert isinstance(pair["sample_id"], str)


def test_golden_pairs_kappa_is_deterministic_one():
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    pairs = [(p["label_a"], p["label_b"]) for p in data["pairs"]]
    # 3 pass/pass + 3 fail/fail：po=1，pe=0.25+0.25=0.5 -> kappa=1.0
    assert cohen_kappa(pairs) == 1.0


# --------------------------------------------------------------------------- #
# 可选离线工具：模块 docstring 必须声明「默认关闭 / 显式 opt-in」接线
# --------------------------------------------------------------------------- #
def test_module_docstring_declares_optional_offline_wiring():
    import eval.judge_calibration.calib as calib

    doc = calib.__doc__ or ""
    assert "opt-in" in doc
    assert "--judge-calibration" in doc
    assert "默认" in doc  # 默认不接线 / 不改口径
