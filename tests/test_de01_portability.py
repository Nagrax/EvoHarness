"""Regression checks for writes escaping the tested checkout and fixture roots."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import docs_consistency, git_preflight, loop, mark_triaged, rebuild_occurrences


def test_audit_tools_use_their_own_checkout():
    root = Path(__file__).resolve().parents[1]
    assert docs_consistency.REPO == root
    assert git_preflight.REPO == root


def test_ledger_guard_distinguishes_sibling_directories(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for module in (loop, mark_triaged):
        monkeypatch.setattr(module, "REPO", root)
        assert module._is_real_ledger(root / "eval" / "BACKLOG.json")
        assert not module._is_real_ledger(tmp_path / "repo-gate" / "BACKLOG.json")


def test_triage_guard_precedes_backup_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(mark_triaged, "REPO", tmp_path)
    ledger = tmp_path / "BACKLOG.json"
    ledger.write_text('{"items": []}\n', encoding="utf-8")
    backup = ledger.with_suffix(".json.bak-before-triage-mark")
    backup.write_text("existing backup", encoding="utf-8")
    monkeypatch.setattr(mark_triaged, "BACKLOG", ledger)
    monkeypatch.setattr(mark_triaged, "_is_under_test", lambda: True)
    assert mark_triaged.main() == 0
    assert ledger.read_text(encoding="utf-8") == '{"items": []}\n'
    assert backup.read_text(encoding="utf-8") == "existing backup"


def test_rebuild_without_cached_scan_keeps_dry_run_read_only(tmp_path, monkeypatch):
    ledger = tmp_path / "BACKLOG.json"
    ledger.write_text('{"items": []}\n', encoding="utf-8")
    scan = tmp_path / "absent-scan.json"
    monkeypatch.setattr(rebuild_occurrences, "BACKLOG", ledger)
    monkeypatch.setattr(rebuild_occurrences, "SCAN", scan)
    calls = []
    monkeypatch.setattr(rebuild_occurrences, "build_report", lambda: calls.append(True) or {"findings": []})
    assert rebuild_occurrences.rebuild(apply=False)["applied"] is False
    assert calls == [True]
    assert not scan.exists()
    assert ledger.read_text(encoding="utf-8") == '{"items": []}\n'
    assert not ledger.with_suffix(".json.bak-before-rebuild").exists()
