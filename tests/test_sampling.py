"""M4 采样科学回归测试：样本量公式自算期望值 + 单调性；漂移命中/不命中。

纯确定性：用 stdlib math.ceil 独立复算期望值，不依赖模型或网络。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.sampling import detect_drift, required_sample_size


def _expected(ci_pp, p=0.5, z=1.96):
    """按标准公式独立自算期望：n = ceil(z^2 * p(1-p) / (ci_pp/100)^2)。"""
    half = ci_pp / 100.0
    return math.ceil(z * z * p * (1.0 - p) / (half * half))


def test_required_sample_size_matches_standard_formula():
    for ci_pp in (0.5, 1.0, 2.0, 5.0, 10.0):
        assert required_sample_size(ci_pp) == _expected(ci_pp), ci_pp
    # 锚点抽查：±1pp、p=0.5、z=1.96 → 9604；±5pp → 385；±10pp → 97
    assert required_sample_size(1) == 9604
    assert required_sample_size(5) == 385
    assert required_sample_size(10) == 97


def test_required_sample_size_custom_p_and_z():
    assert required_sample_size(3, p=0.1, z=1.645) == _expected(3, p=0.1, z=1.645)
    assert required_sample_size(4, p=0.9) == required_sample_size(4, p=0.1)  # p(1-p) 对称


def test_required_sample_size_monotonic_as_ci_shrinks():
    # ci_pp 减小（更窄的置信区间）→ 样本量单调不减
    sizes = [required_sample_size(ci) for ci in (10.0, 5.0, 2.0, 1.0, 0.5)]
    assert sizes == sorted(sizes)
    assert all(a <= b for a, b in zip(sizes, sizes[1:]))


def test_required_sample_size_rejects_bad_input():
    try:
        required_sample_size(0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("ci_pp=0 should raise")


def test_detect_drift_flags_significant_jump():
    series = [
        {"t": "2026-10-01T00:00:00", "value": 0.90, "ci_half": 0.01},
        {"t": "2026-10-02T00:00:00", "value": 0.85, "ci_half": 0.01},
    ]
    alerts = detect_drift(series)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["t"] == "2026-10-02T00:00:00"
    assert alert["value"] == pytest.approx(0.85)
    assert alert["prev"] == pytest.approx(0.90)
    assert alert["delta"] == pytest.approx(-0.05)
    # 阈值改用平方和开方：sqrt(0.01^2 + 0.01^2)，替代旧的「两点相加」口径
    assert alert["threshold"] == pytest.approx(math.sqrt(0.01 * 0.01 + 0.01 * 0.01))


def test_detect_drift_within_overlapping_ci_is_quiet():
    series = [
        {"t": "2026-10-01T00:00:00", "value": 0.900, "ci_half": 0.02},
        {"t": "2026-10-02T00:00:00", "value": 0.920, "ci_half": 0.02},
        {"t": "2026-10-03T00:00:00", "value": 0.930, "ci_half": 0.02},
    ]
    # 阈值 = sqrt(0.02^2 + 0.02^2) ≈ 0.02828；差值 0.02、0.01 均 <= 阈值 → 不越界
    assert detect_drift(series) == []


def test_detect_drift_quadrature_is_stricter_than_sum():
    # 差值 0.03：旧「两点之和」阈值 0.04 会漏报；新平方和开方阈值 ≈ 0.02828 应命中
    series = [
        {"t": "t0", "value": 0.90, "ci_half": 0.02},
        {"t": "t1", "value": 0.93, "ci_half": 0.02},
    ]
    alerts = detect_drift(series)
    assert len(alerts) == 1
    assert alerts[0]["threshold"] == pytest.approx(math.sqrt(0.02 * 0.02 + 0.02 * 0.02))


def test_detect_drift_rejects_negative_or_non_numeric_ci_half():
    # 负 ci_half 旧实现会令阈值为负 → 恒告警（假阳性）；现应报错
    with pytest.raises(ValueError):
        detect_drift(
            [
                {"t": "t0", "value": 0.90, "ci_half": 0.01},
                {"t": "t1", "value": 0.85, "ci_half": -0.01},
            ]
        )
    with pytest.raises(ValueError):
        detect_drift(
            [
                {"t": "t0", "value": 0.90, "ci_half": 0.01},
                {"t": "t1", "value": 0.85, "ci_half": "0.01"},
            ]
        )


def test_detect_drift_exact_threshold_not_flagged_and_short_series():
    # 差值恰等于阈值 sqrt(0.1^2 + 0.1^2)：严格超过才算漂移，故不告警
    threshold = math.sqrt(0.1 * 0.1 + 0.1 * 0.1)
    series = [
        {"t": "t0", "value": 0.0, "ci_half": 0.1},
        {"t": "t1", "value": threshold, "ci_half": 0.1},
    ]
    assert detect_drift(series) == []
    assert detect_drift([]) == []
    assert detect_drift([{"t": "t0", "value": 1.0, "ci_half": 0.1}]) == []
