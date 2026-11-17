"""Status cache must follow content and scope, even with identical file metadata."""
import json
import os
from pathlib import Path

import pytest
from agents import skill_status as status

FIXED_NS = 1_700_000_000_000_000_000


def write_map(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"statuses": {"skill": value}, "certified_statuses": {"skill": value}})
    path.write_bytes(text.ljust(256).encode("utf-8"))
    os.utime(path, ns=(FIXED_NS, FIXED_NS))


@pytest.fixture(autouse=True)
def reset_cache():
    status.reset_status_cache()
    yield
    status.reset_status_cache()


@pytest.mark.parametrize("reader", [status.get_trusted_status_map, status.get_certified_status_map])
def test_same_size_same_timestamp_rewrite_is_visible(tmp_path, monkeypatch, reader):
    path = tmp_path / "status_map.json"
    monkeypatch.setattr(status, "status_map_path", lambda: path)
    write_map(path, "healthy")
    assert reader() == {"skill": "healthy"}
    original = path.stat()
    write_map(path, "watch")
    assert path.stat().st_size == original.st_size
    assert path.stat().st_mtime_ns == original.st_mtime_ns
    assert reader() == {"skill": "watch"}


@pytest.mark.parametrize("reader", [status.get_trusted_status_map, status.get_certified_status_map])
def test_switching_files_with_equal_metadata_preserves_scope(tmp_path, monkeypatch, reader):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    write_map(first, "healthy")
    write_map(second, "watch")
    monkeypatch.setattr(status, "status_map_path", lambda: first)
    assert reader() == {"skill": "healthy"}
    monkeypatch.setattr(status, "status_map_path", lambda: second)
    assert reader() == {"skill": "watch"}


@pytest.mark.parametrize("reader", [status.get_trusted_status_map, status.get_certified_status_map])
def test_invalid_snapshot_fails_closed_and_repair_is_visible(tmp_path, monkeypatch, reader):
    path = tmp_path / "status_map.json"
    monkeypatch.setattr(status, "status_map_path", lambda: path)
    write_map(path, "healthy")
    assert reader() == {"skill": "healthy"}
    path.write_bytes(b"{".ljust(256))
    os.utime(path, ns=(FIXED_NS, FIXED_NS))
    assert reader() == {}
    write_map(path, "watch")
    assert reader() == {"skill": "watch"}
