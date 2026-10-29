"""第三臂回归：硬过滤（EVOHARNESS_MEMORY_LIFECYCLE=filter）。

三档语义：
  off    旧行为（无标注、无过滤）
  soft   标注 stale + superseded（默认）
  filter 直接把被取代的旧版本从候选池剔除

硬过滤的风险与防线：若剔除后候选为空，必须回退原集（否则召回全灭）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import agents.memory as mem


def _h(filename: str, subject: str, age_days: float) -> mem.MemoryHeader:
    return mem.MemoryHeader(
        filename=filename, file_path=f"/x/{filename}",
        mtime_ms=(time.time() - age_days * 86400) * 1000,
        description="d", type="project", subject=subject)


def test_lifecycle_mode_three_values(monkeypatch):
    for raw, expected in (("0", "off"), ("off", "off"), ("false", "off"),
                          ("1", "soft"), ("soft", "soft"), ("", "soft"),
                          ("filter", "filter"), ("hard", "filter")):
        monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", raw)
        assert mem._lifecycle_mode() == expected, f"{raw!r} → {mem._lifecycle_mode()}"


def test_hard_filter_drops_superseded(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "filter")
    headers = [
        _h("quota-old.md", "quota", 90),
        _h("quota-new.md", "quota", 1),
        _h("other.md", "python-sort", 5),
    ]
    kept = mem._drop_superseded(headers)
    names = [h.filename for h in kept]
    assert "quota-old.md" not in names, "被取代的旧版本必须被剔除"
    assert "quota-new.md" in names and "other.md" in names


def test_hard_filter_falls_back_when_all_superseded(monkeypatch):
    """极端情况：全部候选都被判为 superseded（不应发生）→ 回退原集，召回不能全灭。"""
    headers = [_h("only.md", "quota", 5)]
    # 单条同 subject 不会被判 superseded，制造一个人为的 map 来验证回退
    monkey = mem._superseded_map
    try:
        mem._superseded_map = lambda hs: {h.filename: "x.md" for h in hs}
        kept = mem._drop_superseded(headers)
    finally:
        mem._superseded_map = monkey
    assert kept == headers, "剔除后为空必须回退原集"


def test_filter_mode_manifest_has_no_superseded_marker(monkeypatch):
    """filter 臂已剔除旧版，manifest 里自然不该再出现 superseded 标注。"""
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "filter")
    headers = [_h("quota-old.md", "quota", 90), _h("quota-new.md", "quota", 1)]
    kept = mem._drop_superseded(headers)
    manifest = mem.format_memory_manifest(kept)
    assert "superseded" not in manifest
    assert "quota-old.md" not in manifest


def test_soft_mode_keeps_both_with_marker(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "soft")
    headers = [_h("quota-old.md", "quota", 90), _h("quota-new.md", "quota", 1)]
    manifest = mem.format_memory_manifest(headers)
    assert "quota-old.md" in manifest and "quota-new.md" in manifest
    assert "[superseded by quota-new.md]" in manifest


def test_off_mode_no_marker_no_filter(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "0")
    headers = [_h("quota-old.md", "quota", 90), _h("quota-new.md", "quota", 1)]
    assert mem._lifecycle_enabled() is False
    manifest = mem.format_memory_manifest(headers)
    assert "superseded" not in manifest and "stale" not in manifest
