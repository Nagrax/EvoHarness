"""L1/L2 接进写记忆入口的回归（移植闭环第③段）。

写记忆后钩子（_auto_update_memory_index）必须：
- 发现同 subject 覆盖 / 高相似重复 / 陈旧 / 解析失败时**给出可见提示**
- **绝不静默改文件**（程序只提示，语义判断留给人或模型）
- 开关关闭时不提示（A/B 对照可用）
- 健康扫描失败不能拖垮索引重建（降级但留原因）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import agents.tools as tools


def _write_memory(directory: Path, filename: str, body: str, *, age_days: float = 0.0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    if age_days:
        stamp = time.time() - age_days * 86400
        import os
        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def mem_dir(tmp_path, monkeypatch):
    directory = tmp_path / ".evoharness" / "memories"
    directory.mkdir(parents=True)
    monkeypatch.setattr(tools, "get_memory_dir", lambda: directory)
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "1")
    return directory


def test_conflict_is_surfaced_after_write(mem_dir):
    """同 subject 两条记忆（文件名不同）→ 提示覆盖，但不删文件。"""
    _write_memory(mem_dir, "old-rule.md",
                  "---\nname: old-rule\nsubject: deploy-policy\ntype: project\n"
                  "description: 旧部署约定\n---\n旧规则\n", age_days=10)
    new_path = _write_memory(mem_dir, "new-rule.md",
                             "---\nname: new-rule\nsubject: deploy-policy\ntype: project\n"
                             "description: 新部署约定\n---\n新规则\n")

    result = tools._auto_update_memory_index(str(new_path))
    assert result.status == tools.TOOL_OK
    health = result.metadata.get("memoryHealth")
    assert health is not None, "写后必须给出健康提示"
    assert health["summary"]["conflicts"] >= 1
    hint = result.metadata.get("memoryHealthHint", "")
    assert "覆盖" in hint
    # 关键：不静默改文件——两条都还在
    assert (mem_dir / "old-rule.md").exists()
    assert (mem_dir / "new-rule.md").exists()


def test_duplicate_pair_is_surfaced(mem_dir):
    """不同 subject 但高相似描述 → 提示疑似重复。"""
    _write_memory(mem_dir, "a.md",
                  "---\nname: commit-a\nsubject: git-commit-message\ntype: project\n"
                  "description: write git commit message conventions and format rules\n---\nA\n")
    path_b = _write_memory(mem_dir, "b.md",
                           "---\nname: commit-b\nsubject: git-commit-msg-rules\ntype: project\n"
                           "description: writing git commit message conventions and format rules\n---\nB\n")

    result = tools._auto_update_memory_index(str(path_b))
    health = result.metadata.get("memoryHealth") or {}
    assert health.get("summary", {}).get("duplicates", 0) >= 1, (
        f"高相似对必须提示，实际 {health}")


def test_stale_memory_is_surfaced(mem_dir):
    _write_memory(mem_dir, "old.md",
                  "---\nname: old\ntype: project\ndescription: 很旧的记忆\n---\nbody\n",
                  age_days=45)
    path = _write_memory(mem_dir, "fresh.md",
                         "---\nname: fresh\ntype: project\ndescription: 新记忆\n---\nbody\n")
    result = tools._auto_update_memory_index(str(path))
    health = result.metadata.get("memoryHealth") or {}
    assert health.get("summary", {}).get("stale", 0) >= 1


def test_unparsed_memory_is_surfaced(mem_dir):
    """无 frontmatter 的文件必须在健康提示里可见（不静默忽略）。"""
    _write_memory(mem_dir, "broken.md", "没有 frontmatter 的内容\n")
    path = _write_memory(mem_dir, "ok.md",
                         "---\nname: ok\ntype: project\ndescription: d\n---\nbody\n")
    result = tools._auto_update_memory_index(str(path))
    health = result.metadata.get("memoryHealth") or {}
    assert health.get("summary", {}).get("unparsed", 0) >= 1
    assert "broken.md" in health.get("unparsed", [])


def test_clean_memories_emit_no_hint(mem_dir):
    """健康记忆不产生噪音提示（否则模型会学会忽略提示）。"""
    path = _write_memory(mem_dir, "only.md",
                         "---\nname: only\ntype: project\ndescription: 唯一记忆\n---\nbody\n")
    result = tools._auto_update_memory_index(str(path))
    assert "memoryHealth" not in result.metadata


def test_lifecycle_off_suppresses_hint(mem_dir, monkeypatch):
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "0")
    _write_memory(mem_dir, "old-rule.md",
                  "---\nname: old-rule\nsubject: deploy-policy\ntype: project\n"
                  "description: 旧部署约定\n---\n旧规则\n", age_days=10)
    path = _write_memory(mem_dir, "new-rule.md",
                         "---\nname: new-rule\nsubject: deploy-policy\ntype: project\n"
                         "description: 新部署约定\n---\n新规则\n")
    result = tools._auto_update_memory_index(str(path))
    assert "memoryHealth" not in result.metadata, "关闭时必须完全回退旧行为"


def test_health_failure_does_not_break_index(mem_dir, monkeypatch):
    """健康扫描抛错 → 索引仍然重建（降级但不静默：留 error 字段）。"""
    import agents.memory_lifecycle as lifecycle

    def boom(*_args, **_kwargs):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(lifecycle, "scan_memory_health", boom)
    path = _write_memory(mem_dir, "m.md",
                         "---\nname: m\ntype: project\ndescription: d\n---\nbody\n")
    result = tools._auto_update_memory_index(str(path))
    assert result.status == tools.TOOL_OK
    assert "memoryHealthError" in result.metadata
    assert "scan exploded" in result.metadata["memoryHealthError"]
    # 索引真的写出来了
    assert (mem_dir / "MEMORY.md").exists()


def test_index_content_unchanged_by_health_layer(mem_dir, monkeypatch):
    """提示层不得改变索引内容（对照腿与开启腿的 MEMORY.md 必须一致）。"""
    _write_memory(mem_dir, "a.md",
                  "---\nname: a\ntype: project\ndescription: 描述A\n---\nbody\n")
    path_b = _write_memory(mem_dir, "b.md",
                           "---\nname: b\ntype: project\ndescription: 描述B\n---\nbody\n")

    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "1")
    tools._auto_update_memory_index(str(path_b))
    with_hint = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")

    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "0")
    tools._auto_update_memory_index(str(path_b))
    without_hint = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")

    assert with_hint == without_hint, "健康提示是 metadata 层的东西，不得渗进索引正文"
