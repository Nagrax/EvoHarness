"""occurrence 重建回归：位置必须可定位（指纹 + 当前行号）。

真实背景：20 个 high finding 因只存过期行号而永久挂 pending。重建后每个 occurrence
带 fingerprint 与 locatedLine，staleLocation 标记无法定位的项。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.rebuild_occurrences as rebuild_mod


@pytest.fixture()
def temp_backlog(tmp_path, monkeypatch):
    """在临时副本上跑重建，绝不动真实 BACKLOG。"""
    source = REPO / "eval" / "loop" / "BACKLOG.json"
    if not source.is_file():
        pytest.skip("BACKLOG 不存在")
    target = tmp_path / "BACKLOG.json"
    shutil.copyfile(source, target)
    monkeypatch.setattr(rebuild_mod, "BACKLOG", target)
    return target


def test_dry_run_does_not_write(temp_backlog):
    before = temp_backlog.read_text(encoding="utf-8")
    result = rebuild_mod.rebuild(apply=False)
    assert result["applied"] is False
    assert temp_backlog.read_text(encoding="utf-8") == before, "dry-run 不得写文件"
    assert "backup" not in result


def test_apply_writes_backup_and_updates(temp_backlog):
    result = rebuild_mod.rebuild(apply=True)
    assert result["applied"] is True
    backup = Path(result["backup"])
    assert backup.is_file(), "必须留备份"
    data = json.loads(temp_backlog.read_text(encoding="utf-8"))
    module_items = [i for i in data["items"] if (i.get("key") or "").startswith("module:")]
    assert module_items, "测试样本里应有 module 项"
    for item in module_items:
        for occ in item.get("openOccurrences") or []:
            assert "fingerprint" in occ, "重建后的 occurrence 必须带指纹"
            assert "locatedLine" in occ, "必须记录重定位结果"


def test_rebuild_reports_stale_locations_count(temp_backlog):
    result = rebuild_mod.rebuild(apply=True)
    assert "staleLocations" in result
    assert isinstance(result["staleLocations"], int)


def test_real_backlog_occurrences_are_locatable():
    """真实 backlog：重建后不得有失效定位（0 stale）。"""
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    stale = [occ for item in data["items"]
             for occ in (item.get("openOccurrences") or [])
             if occ.get("staleLocation")]
    assert not stale, f"存在无法定位的位置：{stale[:3]}"


def test_rebuilt_occurrences_locate_to_real_code():
    """抽查：locatedLine 必须指向真实的 except 代码附近。

    注意：指纹窗口是 except 行 ±2；若文件里有**完全相同的 except 块**（例如多处
    `except OSError: pass`），滑窗只能定位到第一处——这是指纹法的固有局限，
    所以断言检查 ±2 行范围而不是精确等于原行号。
    """
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    checked = 0
    for item in data["items"]:
        for occ in (item.get("openOccurrences") or []):
            if not occ.get("fingerprint") or checked >= 5:
                continue
            located = occ.get("locatedLine") or 0
            if located <= 0:
                continue
            target = REPO / str(occ.get("file"))
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            assert 1 <= located <= len(lines), "locatedLine 越界"
            window = lines[max(0, located - 3): min(len(lines), located + 2)]
            assert any("except" in line for line in window), (
                f"{occ.get('file')}:{located} 附近没有 except：{window[:1]}")
            checked += 1
    assert checked > 0, "应至少抽查到一条可定位的 occurrence"
