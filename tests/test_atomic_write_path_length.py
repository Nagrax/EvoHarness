"""合法目标路径不应因为原子写临时文件重复目标长名而失败。"""
import os
from pathlib import Path

import pytest
from agents.session import atomic_write_bytes, atomic_write_text


@pytest.mark.parametrize("binary", [False, True])
def test_atomic_write_uses_short_unique_sibling_for_long_destination(tmp_path, monkeypatch, binary):
    target = tmp_path / ("x" * max(1, 240 - len(str(tmp_path.resolve()))))
    replaced = []
    original_replace = os.replace

    def record_replace(source, destination):
        replaced.append(Path(source))
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", record_replace)
    if binary:
        atomic_write_bytes(target, b"verified")
    else:
        atomic_write_text(target, "verified")
    assert target.read_bytes() == b"verified"
    assert len(replaced) == 1
    assert replaced[0].parent == target.parent
    assert len(replaced[0].name) < 24
    assert not replaced[0].exists()
