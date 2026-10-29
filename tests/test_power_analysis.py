"""功效分析回归（2026-10-29）。

核心结论必须被固定住，因为它推翻了一个直觉：
> 对"单向改善"检验，**基线越确定（stable-fail）功效越高**；
> flaky 题的双向噪声在 McNemar 里互相抵消，反而需要更大样本。

若哪天有人（或我）又想"用 flaky 题提高信息密度"，这组测试会拦住。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.power_analysis as pa


def test_mcnemar_matches_known_values():
    assert abs(pa.mcnemar_exact(4, 0) - 0.125) < 1e-9
    assert abs(pa.mcnemar_exact(5, 0) - 0.0625) < 1e-9
    assert pa.mcnemar_exact(0, 0) == 1.0


def test_stable_fail_has_higher_power_than_flaky():
    """核心反直觉结论：同样的相对改善，stable-fail 的功效显著更高。"""
    n = 60
    power_stable = pa.simulate_power(1.00, 0.90, n, trials=600)
    power_flaky = pa.simulate_power(0.50, 0.45, n, trials=600)
    assert power_stable > power_flaky, (
        f"stable-fail({power_stable}) 应高于 flaky({power_flaky})；"
        "若相反，说明模拟逻辑或结论有误")


def test_power_increases_with_sample_size():
    powers = [pa.simulate_power(1.0, 0.9, n, trials=400) for n in (20, 60, 120)]
    assert powers == sorted(powers), f"power 应随样本单调上升：{powers}"


def test_larger_effect_needs_fewer_samples():
    """效应越大，达到同等 power 所需样本越少。"""
    small = pa.simulate_power(0.5, 0.45, 100, trials=500)   # -10%
    large = pa.simulate_power(0.5, 0.30, 100, trials=500)   # -40%
    assert large > small


def test_report_structure_and_conclusion():
    path = REPO / "eval" / "runs" / "power-analysis.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert "powerTable" in data and "n_for_80pct_power" in data
    n_stable = data["n_for_80pct_power"].get("stable-fail 1.00 -> 0.90（W14 现状）")
    n_flaky = data["n_for_80pct_power"].get("flaky 0.50 -> 0.40（相对 -20%）")
    if n_stable and n_flaky:
        assert n_stable < n_flaky, (
            f"stable-fail 需要的样本({n_stable})应少于 flaky({n_flaky})")