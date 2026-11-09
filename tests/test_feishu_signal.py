"""F07：飞书黄金回放信号必须让缺失、过期和严重错答可见。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eval import feishu_signal

GOLDEN = Path(__file__).resolve().parents[1] / "feishu-daily-agent" / "eval" / "golden_cases.json"
REPLAY = Path(__file__).resolve().parents[1] / "feishu-daily-agent" / "eval" / "replay.py"


def _report(**overrides) -> dict:
    report = {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "ok": True,
        "artifactDigest": "a" * 64,
        "goldenSha256": hashlib.sha256(GOLDEN.read_bytes()).hexdigest(),
        "replaySha256": hashlib.sha256(REPLAY.read_bytes()).hexdigest(),
        "strategies": {
            "approved": {
                "ok": True,
                "autoPrecision": 1.0,
                "autoCoverage": 1.0,
                "draftRecall": 1.0,
                "actualAuto": 4,
                "correctAuto": 4,
                "severeErrors": [],
            }
        },
        "failingCases": [],
    }
    report.update(overrides)
    return report


def test_missing_artifact_is_visible(tmp_path: Path):
    findings, summary = feishu_signal.findings_for(tmp_path / "missing.json")
    assert not summary["ok"]
    assert findings[0]["key"] == "feishu:artifact-missing"
    assert findings[0]["severity"] == "medium"


def test_stale_artifact_is_visible(tmp_path: Path):
    path = tmp_path / "stale.json"
    report = _report(generatedAt=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat())
    path.write_text(json.dumps(report), encoding="utf-8")
    findings, summary = feishu_signal.findings_for(path)
    assert "artifact_stale" in summary["issues"]
    assert findings and findings[0]["source"] == "feishu"


def test_severe_errors_produce_high_finding(tmp_path: Path):
    path = tmp_path / "severe.json"
    report = _report(ok=False)
    report["strategies"]["approved"]["severeErrors"] = [{"caseId": "c04"}]
    path.write_text(json.dumps(report), encoding="utf-8")
    findings, summary = feishu_signal.findings_for(path)
    assert not summary["ok"]
    assert findings[0]["severity"] == "high"
    assert "severe_errors" in summary["issues"]


def test_clean_artifact_has_no_findings(tmp_path: Path):
    path = tmp_path / "clean.json"
    path.write_text(json.dumps(_report()), encoding="utf-8")
    findings, summary = feishu_signal.findings_for(path)
    assert summary["ok"]
    assert findings == []


def test_finding_key_survives_until_fixed(tmp_path: Path):
    bad = tmp_path / "bad.json"
    report = _report(ok=False)
    report["strategies"]["approved"]["severeErrors"] = [{"caseId": "c04"}]
    bad.write_text(json.dumps(report), encoding="utf-8")
    bad_findings, _ = feishu_signal.findings_for(bad)
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_report()), encoding="utf-8")
    good_findings, _ = feishu_signal.findings_for(good)
    assert bad_findings and bad_findings[0]["key"].startswith("feishu:eval:")
    assert good_findings == []


def test_collector_crash_is_visible(monkeypatch):
    from eval import loop

    def boom():
        raise RuntimeError("collector crashed")

    monkeypatch.setitem(__import__("sys").modules, "feishu_signal", type(
        "Broken", (), {"collect": staticmethod(boom)}
    ))
    findings: list[dict] = []
    runs: dict = {}
    loop._collect_feishu(findings, runs)
    assert findings[0]["key"] == "feishu:collector-crashed"
    assert findings[0]["severity"] == "high"


def test_loop_collect_feishu_appends_findings(monkeypatch):
    from eval import loop

    class FakeSignal:
        @staticmethod
        def collect():
            return {
                "ok": False,
                "summary": {"ok": False, "issues": ["eval_failed"]},
                "findings": [{
                    "source": "feishu",
                    "key": "feishu:eval:eval_failed",
                    "severity": "high",
                    "title": "fake",
                    "detail": {},
                }],
            }

    monkeypatch.setattr(loop, "feishu_signal", FakeSignal, raising=False)
    findings: list[dict] = []
    runs: dict = {}
    # 函数内部按 import 解析；直接替换 sys.modules 中的模块属性
    import sys

    monkeypatch.setitem(sys.modules, "feishu_signal", FakeSignal)
    loop._collect_feishu(findings, runs)
    assert runs["feishu"]["ok"] is False
    assert findings[0]["source"] == "feishu"
