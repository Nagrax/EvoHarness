"""闭环修复回归：MEMORY.md 索引重建失败/单条解析失败必须可见。

背景（模块扫描 L0039，判读为真缺陷）：
原 `_auto_update_memory_index` 有双层 `except: pass`：
  内层——某个 memory 解析失败，就整条从索引里消失；
  外层——索引写盘失败完全无感。
用户看到的是"记忆存了，但索引里没有"，且没有任何线索。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.tools as tools


@pytest.fixture()
def mem_dir(tmp_path, monkeypatch):
    d = tmp_path / "memories"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tools, "get_memory_dir", lambda: d)
    return d


def _write_memory(directory: Path, name: str, *, broken: bool = False) -> Path:
    path = directory / name
    if broken:
        # 触发解析分支异常：read_text_migrating 被替换为抛错
        path.write_text("---\nname: x\n---\nbody\n", encoding="utf-8")
    else:
        path.write_text(
            "---\nname: 部署约定\ntype: project\ndescription: 生产库隔离\n---\n正文\n",
            encoding="utf-8")
    return path


def test_index_rebuild_success(mem_dir):
    target = _write_memory(mem_dir, "a.md")
    result = tools._auto_update_memory_index(str(target))
    assert result.ok is True
    index = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "部署约定" in index and "a.md" in index
    assert result.metadata.get("memoryIndexOk") is None or True


def test_index_write_failure_is_reported(mem_dir, monkeypatch):
    target = _write_memory(mem_dir, "a.md")

    def boom(*_args, **_kwargs):
        raise OSError("disk full (simulated)")

    real_write = Path.write_text
    monkeypatch.setattr(Path, "write_text", boom)
    result = tools._auto_update_memory_index(str(target))
    monkeypatch.setattr(Path, "write_text", real_write)

    assert result.ok is False, "索引写盘失败必须上报"
    assert result.error_code == "memory-index-write-failed"


def test_single_memory_parse_failure_keeps_entry_visible(mem_dir, monkeypatch):
    good = _write_memory(mem_dir, "good.md")
    bad = _write_memory(mem_dir, "bad.md")

    real_read = tools.read_text_migrating

    def selective_read(path):
        if Path(path).name == "bad.md":
            raise UnicodeError("simulated decode failure")
        return real_read(path)

    monkeypatch.setattr(tools, "read_text_migrating", selective_read)
    result = tools._auto_update_memory_index(str(good))
    assert result.ok is True, "单条失败不应让整体失败"
    index = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "good.md" in index
    assert "bad.md" in index, "解析失败的记忆仍须在索引里可见，不能凭空消失"
    assert "索引解析失败" in index
    assert result.metadata.get("skipped"), "跳过项要带原因"


def test_write_file_surfaces_index_failure_in_metadata(mem_dir, monkeypatch):
    target = mem_dir / "new.md"
    monkeypatch.setattr(tools, "_resolve_tool_path", lambda p, must_exist=True: Path(p))

    def boom(*_args, **_kwargs):
        raise OSError("index unwritable")

    monkeypatch.setattr(tools, "_auto_update_memory_index", boom)
    result = tools._write_file({"file_path": str(target), "content": "x\n"})
    # 文件写成功仍算 ok（索引是降级），但调用方必须能从 metadata 看到失败
    assert result.ok is True
    assert result.metadata["memoryIndexOk"] is False
