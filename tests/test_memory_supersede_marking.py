"""L1 硬标注回归：manifest 里显式标注被取代的旧版本。

背景（A/B 实测驱动的设计变更）：
v1 用软性 `[stale Nd]` 标注，repeats=3 时污染反号（off=0, on=1）；
升级为硬性 `[superseded by X]` 后 v2 方向转正（off=2, on=0）——
但同一配置 off 两次跑出 0 vs 2，噪声 ≥ 信号，**收益不可宣称**。
本文件固定的是"标注机制确实生效"，不是"收益已被证明"。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import agents.memory as mem


def _header(filename: str, subject: str, age_days: float, desc: str = "d") -> mem.MemoryHeader:
    return mem.MemoryHeader(
        filename=filename, file_path=f"/x/{filename}",
        mtime_ms=(time.time() - age_days * 86400) * 1000,
        description=desc, type="project", subject=subject)


def test_older_same_subject_marked_superseded(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "1")
    headers = [
        _header("deploy-old.md", "deploy-policy", 60, "手工发版"),
        _header("deploy-new.md", "deploy-policy", 2, "流水线自动发版"),
    ]
    manifest = mem.format_memory_manifest(headers)
    assert "[superseded by deploy-new.md]" in manifest, (
        f"旧版本必须被标为已被取代，实际：{manifest}")
    # 新版本不得被标注为 superseded（按行内文件名精确匹配，避免旧行的
    # "[superseded by deploy-new.md]" 把新版本行也匹配上）
    new_line = next(l for l in manifest.splitlines() if "deploy-new.md (" in l)
    assert "superseded" not in new_line, f"新版本行不该带 superseded：{new_line}"


def test_distinct_subjects_not_marked(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "1")
    headers = [
        _header("a.md", "python-sort", 5),
        _header("b.md", "git-tag", 5),
    ]
    manifest = mem.format_memory_manifest(headers)
    assert "superseded" not in manifest, "不同 subject 不得互相标注取代"


def test_superseded_marking_off_when_lifecycle_disabled(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "0")
    headers = [
        _header("deploy-old.md", "deploy-policy", 60),
        _header("deploy-new.md", "deploy-policy", 2),
    ]
    manifest = mem.format_memory_manifest(headers)
    assert "superseded" not in manifest, "关闭时必须完全回退旧行为"


def test_three_generations_only_oldest_two_marked(monkeypatch):
    """同 subject 三代：只有最新一代是有效版本，前两代都标 superseded。"""
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "1")
    headers = [
        _header("v1.md", "quota", 90),
        _header("v2.md", "quota", 30),
        _header("v3.md", "quota", 1),
    ]
    manifest = mem.format_memory_manifest(headers)
    assert manifest.count("[superseded by v3.md]") == 2, (
        f"两代旧版本都该标为被 v3 取代，实际：{manifest}")


def test_stale_marking_still_applies_to_unique_subjects(monkeypatch):
    """未被取代但很旧的记忆仍走 stale 标注（两种标注互斥且互补）。"""
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "1")
    monkeypatch.setenv("EVOHARNESS_MEMORY_STALE_DAYS", "30")
    headers = [_header("lonely.md", "unique-subject", 200)]
    manifest = mem.format_memory_manifest(headers)
    assert "[stale 200d]" in manifest
    assert "superseded" not in manifest


def test_header_subject_falls_back_to_filename(monkeypatch):
    """旧记忆没有 subject 字段：用文件名规范化兜底（不因缺字段失去生命周期保护）。"""
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "1")
    h = mem.MemoryHeader(filename="Deploy Policy.md", file_path="/x/Deploy Policy.md",
                         mtime_ms=time.time() * 1000, description="d", type="project")
    # scan_memory_headers 会给 subject；直接构造时为空 → 不做跨文件匹配（安全默认）
    assert h.subject is None
    assert mem._header_subject({"subject": " Deploy-Policy "}, "x") == "deploy-policy"
    assert mem._header_subject({}, "Deploy Policy") == "deploy-policy"
