"""eval-workflow.v1：桥接层信号必须让缺失、过期、失败和安全失败可见（对齐 F07 语义）。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eval import collab_signal

GOLDEN = Path(__file__).resolve().parents[1] / "integration" / "eval" / "golden_cases.json"


def _report(**overrides) -> dict:
    report = {
        "version": "collab-eval.v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "ok": True,
        "safetyFailed": False,
        "severe": 0,
        "cases": 16,
        "overallRate": 1.0,
        "goldenSha256": hashlib.sha256(GOLDEN.read_bytes()).hexdigest(),
        "failingCases": [],
    }
    report.update(overrides)
    return report


def _write(tmp_path: Path, report: dict) -> Path:
    path = tmp_path / "collab-eval.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


def test_missing_artifact_is_visible(tmp_path: Path):
    findings, summary = collab_signal.findings_for(tmp_path / "absent.json")
    assert summary["ok"] is False
    assert findings[0]["severity"] == "medium"
    assert "artifact_missing" in summary["issues"]


def test_failed_or_safety_maps_high(tmp_path: Path):
    now = datetime.now(timezone.utc)
    for report in (_report(ok=False), _report(safetyFailed=True), _report(severe=2)):
        findings, summary = collab_signal.findings_for(
            _write(tmp_path, report), now=now)
        assert summary["ok"] is False
        assert findings[0]["severity"] == "high"


def test_stale_artifact_is_visible(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    findings, summary = collab_signal.findings_for(
        _write(tmp_path, _report(generatedAt=old)), now=datetime.now(timezone.utc))
    assert "artifact_stale" in summary["issues"]
    assert findings[0]["severity"] == "medium"


def test_golden_change_detected(tmp_path: Path):
    findings, summary = collab_signal.findings_for(
        _write(tmp_path, _report(goldenSha256="0" * 64)), now=datetime.now(timezone.utc))
    assert "golden_changed" in summary["issues"]


def test_green_artifact_is_ok(tmp_path: Path):
    findings, summary = collab_signal.findings_for(
        _write(tmp_path, _report()), now=datetime.now(timezone.utc))
    assert summary["ok"] is True
    assert findings == []


def test_cli_exit_codes(tmp_path: Path, monkeypatch, capsys):
    # collect(path=ARTIFACT) 的默认参数在定义时绑定，patch 模块属性不影响它；
    # 直接替换 collect 指向临时 artifact（与生产调用面一致）。
    from functools import partial

    orig = collab_signal.collect
    green = _write(tmp_path, _report())
    monkeypatch.setattr(collab_signal, "collect", partial(orig, green))
    assert collab_signal.main(["--check"]) == 0
    failed = _write(tmp_path, _report(ok=False))
    monkeypatch.setattr(collab_signal, "collect", partial(orig, failed))
    assert collab_signal.main(["--check"]) == 1
