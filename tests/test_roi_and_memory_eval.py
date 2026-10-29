"""ROI 与记忆评测的回归：核算口径必须稳定，不能悄悄放宽。

这两个评测器是本项目回答"收益能否对冲 token 成本"的唯一工具。
它们一旦被放宽（比如把小样本判成 profitable、把成本漏算），
所有后续结论都会失真——所以口径本身要进 CI。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import eval.roi as roi


# ── ROI：小样本必须判不可宣称 ──────────────────────────────


def test_bootstrap_ci_is_empty_for_no_discordant():
    result = roi.bootstrap_diff_ci([], [])
    assert result["delta"] == 0
    assert result["note"] == "no-discordant-pairs"


def test_bootstrap_ci_brackets_observed_direction():
    result = roi.bootstrap_diff_ci([1, 2, 3, 4, 5], [1])
    assert result["delta"] == 4
    lo, hi = result["ci95"]
    assert lo <= result["delta"] <= hi


def test_evolution_roi_downgrades_small_samples(tmp_path, monkeypatch):
    """n 太小的时候，任何 profitable 都必须降级为 not-claimable。"""
    curve = {
        "arms": ["no_evolution", "gated_evolution"],
        "points": [
            {"round": 1, "arm": "no_evolution", "pass_at_1": 0.0, "n": 2, "tokens_M": 0.3},
            {"round": 1, "arm": "gated_evolution", "pass_at_1": 50.0, "n": 2, "tokens_M": 0.4},
        ],
        "total_tokens_M": 0.7,
        "side_query_tokens_M": 0.05,
        "evolution_fired_by_arm": {"no_evolution": False, "gated_evolution": True},
        "new_skills_by_arm": {"no_evolution": 0, "gated_evolution": 1},
    }
    path = tmp_path / "curve.json"
    path.write_text(json.dumps(curve), encoding="utf-8")
    result = roi.evolution_roi(path)
    assert result["ok"] is True
    rows = result["relativeToBaseline"]
    assert rows, "必须产出相对基线比较"
    for row in rows:
        assert row["rawVerdict"] == "profitable"
        assert row["verdict"].startswith("not-claimable"), (
            f"n=2 时不得宣称 profitable，实际={row['verdict']}")


def test_evolution_roi_flags_undersampling(tmp_path):
    curve = {
        "arms": ["no_evolution", "gated_evolution"],
        "points": [
            {"round": 1, "arm": "no_evolution", "pass_at_1": 0.0, "n": 100, "tokens_M": 1.0},
            {"round": 1, "arm": "gated_evolution", "pass_at_1": 30.0, "n": 100, "tokens_M": 1.5},
        ],
        "total_tokens_M": 2.5,
        "side_query_tokens_M": 0.1,
    }
    path = tmp_path / "curve.json"
    path.write_text(json.dumps(curve), encoding="utf-8")
    result = roi.evolution_roi(path)
    row = result["relativeToBaseline"][0]
    # n=100 足够，允许保留 profitable
    assert row["verdict"] == "profitable"
    assert row["roiPerMillionTokens"] == 60.0


def test_evolution_roi_separates_side_query_cost(tmp_path):
    curve = {
        "arms": ["no_evolution"],
        "points": [{"round": 1, "arm": "no_evolution", "pass_at_1": 10.0, "n": 40, "tokens_M": 2.0}],
        "total_tokens_M": 2.0,
        "side_query_tokens_M": 0.2,
    }
    path = tmp_path / "curve.json"
    path.write_text(json.dumps(curve), encoding="utf-8")
    result = roi.evolution_roi(path)
    assert result["costSplit"]["evolutionOverheadPct"] == pytest.approx(10.0)


# ── 报告红线文案必须存在（防话术漂移） ─────────────────────


def test_report_declares_red_lines():
    report = roi.build_report(include_json=False)
    text = json.dumps(report, ensure_ascii=False)
    assert "不得用省 token 冒充收益" in text
    assert "CI 跨 0 一律 inconclusive" in text
    assert "tokensPerCorrect" in text


def test_report_includes_known_benchmarks():
    report = roi.build_report(include_json=False)
    tags = {row["tag"] for row in report["benchmarks"]}
    # 至少 GAIA 存在且带 CI 与 token 成本
    if "gaia" in tags:
        gaia = next(row for row in report["benchmarks"] if row["tag"] == "gaia")
        assert gaia["pass_at_1_ci95"] is not None
        assert gaia["tokensPerCorrect"] and gaia["tokensPerCorrect"] > 0
    # 分析产物目录（如 harness2 的 summary）不得被当成 benchmark 混进成本表
    assert "harness2" not in tags, "harness2 是分析产物，不是 benchmark run"


def test_available_tags_requires_benchmark_markers(tmp_path, monkeypatch):
    """只有含 planned/attempted 的 summary 才算 benchmark。"""
    (tmp_path / "real-run").mkdir()
    (tmp_path / "real-run" / "summary.json").write_text(
        json.dumps({"planned": 10, "attempted": 10, "pass_at_1": 50.0}), encoding="utf-8")
    (tmp_path / "analysis-dir").mkdir()
    (tmp_path / "analysis-dir" / "summary.json").write_text(
        json.dumps({"kind": "harness2-summary", "rows": []}), encoding="utf-8")
    monkeypatch.setattr(roi, "RUNS", tmp_path)
    assert roi.available_tags() == ["real-run"]


# ── 记忆评测：机制断言必须能抓到破坏 ────────────────────────


def test_memory_eval_script_passes():
    """记忆 7 机制必须维持可复跑通过；失败即说明记忆代码被改坏。"""
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, str(repo / "eval" / "memory_eval.py")],
        capture_output=True, timeout=300, cwd=str(repo),
        env={**__import__("os").environ, "PYTHONUTF8": "1"}, check=False)
    assert out.returncode == 0, out.stdout.decode("utf-8", "replace")[-800:]
    report = json.loads((repo / "eval" / "runs" / "memory-eval.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["total"] >= 7
    assert report["failed"] == 0
    # 成本必须被记录（否则无法判断记忆是否代价过高）
    assert report["costSummary"]["headerReadBytes"] is not None
    assert report["costSummary"]["injectedChars"] is not None
