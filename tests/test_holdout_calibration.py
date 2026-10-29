"""holdout 难度校准回归：防止"全 0 分"被误读成"配置无差异"。

真踩过的坑（本轮实测）：
`predictions.jsonl` 里**没有** `correct` 字段（只有 gold/prediction/tokens），
初版校准脚本直接读 `row["correct"]`，把 GAIA 165 题全部当失败，得出
"GAIA 全 0 分"这种与 summary.json（49.1%）完全矛盾的结论。

正确做法是调用仓库真实判分函数 `eval.run_benchmark.classify_record`。
本文件固定这一事实，并固定难度分层口径。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import eval.holdout_calibrate as hc


def test_predictions_have_no_correct_field():
    """事实：预测文件不含 correct 字段——这正是初版误判的根因。"""
    repo = Path(__file__).resolve().parents[1]
    path = repo / "eval" / "runs" / "gaia" / "predictions.jsonl"
    if not path.is_file():
        import pytest

        pytest.skip("GAIA predictions 不在本机")
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "correct" not in first, "若预测文件新增 correct 字段，本校准器可简化；需同步更新"


def test_calibration_matches_summary_scale():
    """校准后的 alwaysPass 必须与 summary 的 correct 量级一致（不能全判失败）。"""
    repo = Path(__file__).resolve().parents[1]
    path = repo / "eval" / "runs" / "gaia" / "predictions.jsonl"
    summary_path = repo / "eval" / "runs" / "gaia" / "summary.json"
    if not path.is_file() or not summary_path.is_file():
        import pytest

        pytest.skip("GAIA 产物不在本机")
    correct = hc.load_correct("gaia")
    hit = sum(1 for v in correct.values() if v)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    expected = int(summary.get("correct") or 0)
    assert hit == expected, (
        f"逐题判对={hit} 必须等于 summary.correct={expected}；"
        "不等说明用了错误的判据（例如读不存在的 correct 字段）")


def test_calibration_buckets_partition_common_tasks():
    result = hc.calibrate(["gaia", "gaia-fold03"])
    if not result.get("ok"):
        import pytest

        pytest.skip(f"calibration unavailable: {result.get('reason')}")
    total = result["alwaysFail"] + result["alwaysPass"] + result["discriminating"]
    assert total == result["commonTasks"], "三层必须互斥且完备"
    assert 0.0 <= result["discriminatingRate"] <= 100.0


def test_discriminating_rate_is_meaningful():
    """若区分度极低，必须被判为 no-signal，避免用无信号题池下结论。"""
    result = hc.calibrate(["gaia", "gaia-fold03"])
    if not result.get("ok"):
        import pytest

        pytest.skip("calibration unavailable")
    assert result["verdict"] in {"usable-with-stratification", "no-signal:all-configs-fail",
                                 "no-signal:all-configs-pass"}
    if result["discriminating"] == 0:
        assert result["verdict"].startswith("no-signal")
    else:
        assert result["verdict"] == "usable-with-stratification"
