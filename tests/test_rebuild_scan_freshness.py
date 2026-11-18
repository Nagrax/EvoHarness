"""Historical scan files cannot keep edited or removed findings alive."""
import json

import pytest

from eval import module_scan, rebuild_occurrences


@pytest.mark.parametrize("remove_finding", [False, True])
def test_rebuild_uses_current_source_without_overwriting_archive(tmp_path, monkeypatch, remove_finding):
    monkeypatch.setattr(module_scan, "REPO", tmp_path)
    monkeypatch.setattr(rebuild_occurrences, "REPO", tmp_path)
    source = tmp_path / "agents" / "sample.py"
    source.parent.mkdir()
    source.write_text("def f():\n    try:\n        old_call()\n    except Exception:\n        pass\n", encoding="utf-8")
    original = module_scan.build_report()
    hits = [row for row in original["findings"] if row["rule"] == "silent-except"]
    assert len(hits) == 1
    cached = tmp_path / "scan.json"
    cached.write_text(json.dumps(original), encoding="utf-8")
    archived = cached.read_bytes()
    backlog = tmp_path / "BACKLOG.json"
    backlog.write_text(json.dumps({"items": [{
        "id": "L1", "key": "module:silent-except:agents/sample.py",
        "openOccurrences": [{"fingerprint": hits[0]["fingerprint"]}]}]}), encoding="utf-8")
    monkeypatch.setattr(rebuild_occurrences, "SCAN", cached)
    monkeypatch.setattr(rebuild_occurrences, "BACKLOG", backlog)
    source.write_text("def f():\n    return 42\n" if remove_finding else
        "def f():\n    try:\n        changed_call()\n    except Exception:\n        pass\n", encoding="utf-8")
    result = rebuild_occurrences.rebuild(apply=True)
    assert result["staleLocations"] == 0
    assert cached.read_bytes() == archived
    rows = json.loads(backlog.read_text(encoding="utf-8"))["items"][0]["openOccurrences"]
    if remove_finding:
        assert rows == []
    else:
        assert len(rows) == 1
        assert rows[0]["fingerprint"] != hits[0]["fingerprint"]
        assert rows[0]["locatedLine"] == 4
