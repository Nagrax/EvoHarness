"""The first delegation never exposes a partially written starter SOP."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from evoharness.organization.access import ensure_delivery_skill


EMPLOYEE = "ai-" + "a" * 32
SPACE = "space-" + "b" * 32


@pytest.fixture
def starter_path(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOHARNESS_SKILLS_ROOT", str(tmp_path))
    return tmp_path / f"delivery-{EMPLOYEE}-{SPACE}" / "SKILL.md"


def test_interrupted_starter_write_is_retryable(starter_path, monkeypatch):
    original = Path.open

    class InterruptedWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()

        def write(self, text):
            self.stream.write(text[:12])
            raise OSError("write interrupted")

    def open_file(path, mode="r", *args, **kwargs):
        stream = original(path, mode, *args, **kwargs)
        if mode == "x" and path.parent == starter_path.parent:
            return InterruptedWrite(stream)
        return stream

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_file)
        with pytest.raises(OSError, match="write interrupted"):
            ensure_delivery_skill(EMPLOYEE, SPACE)

    assert list(starter_path.parent.iterdir()) == []
    ensure_delivery_skill(EMPLOYEE, SPACE)
    assert "name: delivery-review" in starter_path.read_text(encoding="utf-8")


def test_concurrent_starter_writers_preserve_adopted_skill(starter_path):
    launch = Barrier(2)

    def initialize():
        launch.wait(timeout=10)
        ensure_delivery_skill(EMPLOYEE, SPACE)

    with ThreadPoolExecutor(max_workers=2) as threads:
        futures = [threads.submit(initialize) for _ in range(2)]
        for future in futures:
            future.result()

    assert "name: delivery-review" in starter_path.read_text(encoding="utf-8")
    revised = "---\nname: delivery-review\nversion: 1.0.1\n---\nApproved revision\n"
    starter_path.write_text(revised, encoding="utf-8")
    ensure_delivery_skill(EMPLOYEE, SPACE)
    assert starter_path.read_text(encoding="utf-8") == revised
    assert list(starter_path.parent.iterdir()) == [starter_path]
