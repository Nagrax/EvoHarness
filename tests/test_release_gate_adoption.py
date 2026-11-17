"""DE-13 release_gate → 认证/采用：失败门不可被认证绕过，严格模式要求门结果。"""
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agents.skill_search import certification as cert


def _certificate(**overrides):
    certificate = {
        "certificate_id": "cert-test",
        "state": cert.STATE_CERTIFIED_CANDIDATE,
        "skill_name": "deploy",
        "artifact": {"release_bundle_sha256": ""},
        "release_gate": {"passed": False, "violations": [{"metric": "accuracy"}]},
    }
    certificate.update(overrides)
    return certificate


def test_certify_rejects_failed_release_gate_before_other_checks():
    result = cert.certify_candidate(
        manifest=SimpleNamespace(), locked={}, packaged={},
        release_gate={"ok": False, "rejected_reason": "champion_empty"})
    assert result["ok"] is False and result["error"] == "release_gate_not_passed"


def test_adopt_rejects_failed_release_gate():
    result = cert.adopt_certified_candidate(certificate=_certificate())
    assert result["ok"] is False and result["error"] == "release_gate_not_passed"
    assert result["violations"] == [{"metric": "accuracy"}]


def test_adopt_passing_gate_moves_to_next_requirement():
    result = cert.adopt_certified_candidate(
        certificate=_certificate(release_gate={"passed": True}))
    assert result["ok"] is False and result["error"] == "bundle_required"


def test_strict_mode_requires_release_gate(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_REQUIRE_RELEASE_GATE", "1")
    certificate = _certificate(release_gate={})
    result = cert.adopt_certified_candidate(certificate=certificate)
    assert result["ok"] is False and result["error"] == "release_gate_required"
