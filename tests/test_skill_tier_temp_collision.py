"""Concurrent tier writers remain independent when the wall clock repeats."""
import json
import threading
import pytest

from agents import skill_tiers


@pytest.mark.parametrize("writer_name", ["_write_tiers_atomic", "_atomic_write_text"])
def test_same_timestamp_writers_have_distinct_temporary_files(tmp_path, monkeypatch, writer_name):
    path = tmp_path / "deployment_tiers.json"
    monkeypatch.setattr(skill_tiers.time, "time_ns", lambda: 123456789)
    barrier = threading.Barrier(8)
    real_replace = skill_tiers.os.replace
    sources = []
    errors = []
    local = threading.local()

    def synchronized_replace(src, dst):
        sources.append(str(src))
        if not getattr(local, "synchronized", False):
            local.synchronized = True
            barrier.wait(timeout=10)
        return real_replace(src, dst)

    monkeypatch.setattr(skill_tiers.os, "replace", synchronized_replace)

    def write(index):
        try:
            payload = {"version": 1, "tiers": {f"s{index}": "experimental"}}
            value = payload if writer_name == "_write_tiers_atomic" else json.dumps(payload)
            getattr(skill_tiers, writer_name)(path, value)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert len(set(sources)) == 8
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["tiers"]) == 1
    assert set(payload["tiers"]).issubset({f"s{i}" for i in range(8)})
    assert not list(tmp_path.glob("*.tmp"))
