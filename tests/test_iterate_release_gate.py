"""DE-13：iterate 把 collab 评测的冻结门禁结论显式带进轮次结果。

缺产物不冒充通过（ok=None）；有产物按 ok/gatesSha256 原样呈现，
同一 JSON 可作为验收 gate_report 证据。
"""
from pathlib import Path
import json
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "integration"))

import integration.iterate as iterate


def _write(tmp_path, payload):
    artifact = tmp_path / "collab-eval.json"
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    return artifact


def test_release_gate_step_reads_passed_artifact(tmp_path, monkeypatch):
    artifact = _write(tmp_path, {"ok": True, "overallRate": 1.0,
                                 "gatesSha256": "c" * 64})
    monkeypatch.setattr(iterate, "COLLAB_EVAL_ARTIFACT", artifact)
    assert iterate._release_gate_step() == {
        "name": "releaseGate", "ok": True, "overallRate": 1.0,
        "gatesSha256": "c" * 64}


def test_release_gate_step_reports_failed_gate(tmp_path, monkeypatch):
    artifact = _write(tmp_path, {"ok": False, "overallRate": 0.5,
                                 "gatesSha256": "d" * 64})
    monkeypatch.setattr(iterate, "COLLAB_EVAL_ARTIFACT", artifact)
    step = iterate._release_gate_step()
    assert step["ok"] is False and step["overallRate"] == 0.5


def test_release_gate_step_missing_artifact_is_not_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(iterate, "COLLAB_EVAL_ARTIFACT", tmp_path / "missing.json")
    step = iterate._release_gate_step()
    assert step["ok"] is None and step["reason"] == "artifact_missing"
