"""eval-workflow.v1 自身回归（L9050）：评测器/信号源/趋势检测。

覆盖：
- collab_eval：golden 加载、五组评测在已知状态下的判定、artifact 结构
- collab_signal：missing/stale/failed/safety 四类 finding 映射（冻结门语义）
- kpi_trend：record/check/趋势摘要（临时历史文件，不污染真实数据）
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "integration", REPO / "eval"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import collab_signal  # noqa: E402
from integration.eval import collab_eval, kpi_trend  # noqa: E402


def test_golden_and_gates_load_and_match():
    golden = collab_eval._golden()
    gates = collab_eval._gates()
    assert golden["version"].startswith("collab-golden")
    for group in ("intake", "nudge", "ask", "audit", "red"):
        assert group in golden["groups"], f"golden 缺组 {group}"
        assert group in ("intake", "nudge", "ask", "audit", "red")
    assert gates["gates"]["severeAllow"] == 0


def test_run_produces_green_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(collab_eval, "RUNS", tmp_path)
    monkeypatch.setattr(collab_eval, "ARTIFACT", tmp_path / "collab-eval.json")
    report = collab_eval.run()
    assert report["ok"] is True
    assert report["safetyFailed"] is False
    assert report["overallRate"] == 1.0
    assert set(report["groups"]) == {"intake", "nudge", "ask", "audit", "red"}
    written = json.loads((tmp_path / "collab-eval.json").read_text(encoding="utf-8"))
    assert written["goldenSha256"] == report["goldenSha256"]


def test_intake_group_detects_bad_theme(monkeypatch):
    broken = [{"id": "x1", "text": "主题：A 任务：B", "expect": {"theme": "错误答案"}}]
    result = collab_eval.eval_intake(broken)
    assert result["passRate"] == 0.0
    assert result["caseResults"][0]["reasons"]


def test_signal_missing_artifact_is_medium(tmp_path):
    findings, summary = collab_signal.findings_for(tmp_path / "nope.json")
    assert summary["ok"] is False
    assert findings[0]["severity"] == "medium"
    assert "artifact_missing" in summary["issues"]


def test_signal_failed_and_safety_are_high(tmp_path):
    base = {"generatedAt": datetime.now(timezone.utc).isoformat(),
            "goldenSha256": "x", "cases": 16, "overallRate": 0.9,
            "failingCases": []}
    failed = {**base, "ok": False}
    safety = {**base, "ok": True, "safetyFailed": True}
    for report in (failed, safety):
        findings, summary = collab_signal.findings_for(
            _write(tmp_path, json.dumps(report)), now=datetime.now(timezone.utc))
        assert summary["ok"] is False
        assert findings[0]["severity"] == "high"


def test_signal_stale_is_medium(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    report = {"generatedAt": old, "ok": True, "goldenSha256": "x",
              "overallRate": 1.0, "cases": 16, "failingCases": []}
    findings, summary = collab_signal.findings_for(
        _write(tmp_path, json.dumps(report)), now=datetime.now(timezone.utc))
    assert "artifact_stale" in summary["issues"]
    assert findings[0]["severity"] == "medium"


def test_kpi_trend_records_and_flags_degradation(tmp_path, monkeypatch):
    monkeypatch.setattr(kpi_trend, "HISTORY", tmp_path / "kpi-history.jsonl")
    rows = [
        {"at": "t1", "source": "collab", "metrics": {"overallRate": 1.0,
                                                     "safetyFailed": 0, "severe": 0}},
        {"at": "t2", "source": "collab", "metrics": {"overallRate": 0.75,
                                                     "safetyFailed": 0, "severe": 4}},
    ]
    (tmp_path / "kpi-history.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    result = kpi_trend.check()
    assert result["ok"] is False
    keys = {f["key"] for f in result["findings"]}
    assert "kpi-trend:collab.overallRate" in keys
    assert "kpi-trend:collab.severe" in keys


def test_kpi_trend_insufficient_sample_is_neither_clean_nor_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(kpi_trend, "HISTORY", tmp_path / "kpi-history.jsonl")
    (tmp_path / "kpi-history.jsonl").write_text(
        json.dumps({"at": "t1", "source": "collab",
                    "metrics": {"overallRate": 1.0, "safetyFailed": 0, "severe": 0}}) + "\n",
        encoding="utf-8")
    result = kpi_trend.check()
    assert result["ok"] is True and result["findings"] == []


def _write(tmp: Path, text: str) -> Path:
    path = tmp / "collab-eval.json"
    path.write_text(text, encoding="utf-8")
    return path
