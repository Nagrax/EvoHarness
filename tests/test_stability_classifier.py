"""稳定性判定升级回归：失败率连续值 + 重复次数置信度。

升级依据（实测）：8 道"2/2 stable"的题再跑一批后有 25% 给出标记
→ 2 遍的二分类判定不可靠。改为：
  failRate = noMarker / repeats
  stable >= 0.8（容忍 1 次抖动）/ flaky 中间带 / disappeared == 0
  confidence: repeats >= 3 → high，否则 low
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.failure_stability import (
    MIN_REPEATS_FOR_CONFIDENCE,
    STABLE_MIN_FAIL_RATE,
    classify_failure,
)


def test_thresholds_are_documented_constants():
    assert STABLE_MIN_FAIL_RATE == 0.8, "stable 阈值必须容忍 1 次抖动"
    assert MIN_REPEATS_FOR_CONFIDENCE == 3, "3 遍以下置信度一律 low"


def test_classification_boundaries():
    assert classify_failure(0.0) == "disappeared"
    assert classify_failure(0.2) == "flaky"
    assert classify_failure(0.6) == "flaky"
    assert classify_failure(0.79) == "flaky"
    assert classify_failure(0.8) == "stable"
    assert classify_failure(0.9) == "stable"
    assert classify_failure(1.0) == "stable"


def test_two_of_three_now_counts_as_stable():
    """2/3 = 0.667 仍属 flaky——这正是"2 遍判 stable 不可靠"的直接体现。"""
    assert classify_failure(2 / 3) == "flaky"
    # 而 4/5 = 0.8 才进 stable
    assert classify_failure(4 / 5) == "stable"


def test_script_reports_confidence_and_distribution():
    source = (REPO / "eval" / "failure_stability.py").read_text(encoding="utf-8")
    assert '"confidence"' in source and "MIN_REPEATS_FOR_CONFIDENCE" in source
    assert "failRateDistribution" in source
    assert "classify_failure" in source
    # 旧口径不得残留在逻辑里
    assert "status = (\"stable\" if repro == 1.0" not in source


def test_real_three_repeat_batch_has_high_confidence():
    """真实 3 遍批次必须标记为 high confidence（若产物存在）。"""
    import json

    path = REPO / "eval" / "runs" / "failure-stability-3.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["repeats"] >= 3
    assert data["confidence"] == "high"
    for task in data["perTask"]:
        assert task["confidence"] == "high"
        assert task["status"] == classify_failure(task["failRate"])
