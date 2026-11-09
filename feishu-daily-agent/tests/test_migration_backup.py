"""迁移备份保留策略：每次启动都复制 49MB 备份会撑满磁盘。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from app.core.migrations import backup_database


def test_backup_prunes_old_copies(tmp_path):
    db = tmp_path / "agent.db"
    db.write_bytes(b"x" * 4096)
    old: list[Path] = []
    for index in range(5):
        path = tmp_path / f"agent.db.bak-2026010100000{index}"
        path.write_bytes(b"old")
        stamp = time.time() - (10 - index) * 60
        os.utime(path, (stamp, stamp))
        old.append(path)

    target = backup_database(f"sqlite:///{db.as_posix()}", directory=tmp_path)

    assert target is not None
    kept = sorted(tmp_path.glob("agent.db.bak-*"))
    assert len(kept) == 3
    assert Path(target) in kept
    assert old[4] in kept
    assert old[0] not in kept
