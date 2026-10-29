"""W10 回归：中文整句触发记忆召回、候选头部有界读取。

历史缺陷（已修）：
- `start_memory_prefetch` 用 `re.search(r"\\s", query)` 判断：中文整句无空格 →
  **永不召回**（实测 "上次说的生产库部署约定是什么" prefetchStarted=false）；
- `scan_memory_headers` 整读正文再切前 30 行：2 个候选即读 1,560,133 字节（100% 全量）。

本文件只测触发条件与读取量，不联网。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.memory as mem


@pytest.fixture()
def memory_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    memdir = home / ".evoharness" / "memories"
    memdir.mkdir(parents=True)
    monkeypatch.setattr(mem, "get_memory_dir", lambda: memdir)
    return memdir


def _side_query_returning(names):
    async def side_query(_system, _user):
        import json

        return json.dumps({"selected_memories": list(names)})
    return side_query


# ─── ① 触发条件：中文整句必须召回 ────────────────────────────


def test_chinese_sentence_triggers_recall(memory_dir):
    (memory_dir / "deploy.md").write_text(
        "---\ndescription: 部署约定\ntype: project\n---\n生产库用隔离容器。\n", encoding="utf-8")
    query = "上次说的生产库部署约定是什么"
    assert mem.re.search(r"\s", query.strip()) is None  # 确实没有空白

    async def run():
        handle = mem.start_memory_prefetch(query, _side_query_returning(["deploy.md"]), set(), 0)
        assert handle is not None, "中文整句必须触发召回"
        return await handle.task

    recalled = asyncio.run(run())
    assert any("部署约定" in item.content for item in recalled)


def test_should_prefetch_reasons():
    assert mem.should_prefetch_memory("上次说的生产库部署约定是什么") == (True, "cjk")
    assert mem.should_prefetch_memory("what is the deployment convention") == (True, "multiword")
    assert mem.should_prefetch_memory("ls") == (False, "low-value-command")
    assert mem.should_prefetch_memory("   ") == (False, "no-query")
    assert mem.should_prefetch_memory("好") == (False, "too-short")
    assert mem.should_prefetch_memory("cat") == (False, "low-value-command")


def test_low_value_command_does_not_trigger(memory_dir):
    (memory_dir / "a.md").write_text("---\ndescription: x\n---\nbody\n", encoding="utf-8")

    async def run():
        return mem.start_memory_prefetch("git status", _side_query_returning([]), set(), 0)

    assert asyncio.run(run()) is None


def test_english_multiword_still_triggers(memory_dir):
    (memory_dir / "a.md").write_text("---\ndescription: x\n---\nbody\n", encoding="utf-8")

    async def run():
        handle = mem.start_memory_prefetch(
            "what is the deployment convention", _side_query_returning(["a.md"]), set(), 0)
        assert handle is not None
        await handle.task
        return True

    assert asyncio.run(run()) is True


# ─── ② 候选扫描必须有界读取 ──────────────────────────────────


def test_header_scan_reads_only_head_of_huge_files(memory_dir, monkeypatch):
    big = "---\ndescription: 大文件\ntype: project\n---\n" + ("filler line\n" * 60000)
    (memory_dir / "big.md").write_text(big, encoding="utf-8")
    (memory_dir / "small.md").write_text(
        "---\ndescription: 小文件\ntype: project\n---\ntiny\n", encoding="utf-8")

    observed = {"bytes": 0}

    def counting_read(path):
        raw = Path(path).read_bytes()
        observed["bytes"] += len(raw)
        return raw.decode("utf-8", errors="replace")

    monkeypatch.setattr(mem, "read_text_migrating", counting_read)
    headers = mem.scan_memory_headers()

    assert {h.filename for h in headers} == {"big.md", "small.md"}
    # frontmatter 必须仍被正确解析
    big_header = next(h for h in headers if h.filename == "big.md")
    assert big_header.description == "大文件"
    total = (memory_dir / "big.md").stat().st_size + (memory_dir / "small.md").stat().st_size
    assert observed["bytes"] <= 2 * mem.HEADER_READ_BYTES, (
        f"读取量必须有界：读了 {observed['bytes']} 而全量是 {total}")
    assert observed["bytes"] < total


def test_header_scan_bounded_helper_preserves_encoding_contract(memory_dir):
    # 旧编码（GBK）头部仍须可解析：有界读取不能破坏既有解码契约
    path = memory_dir / "legacy.md"
    path.write_bytes("---\ndescription: 旧编码记忆\ntype: project\n---\n正文\n".encode("gbk"))
    raw = mem.read_text_migrating_limited(path, mem.HEADER_READ_BYTES)
    assert "旧编码记忆" in raw


def test_headers_still_parse_only_first_lines(memory_dir):
    (memory_dir / "m.md").write_text(
        "---\ndescription: 只在头部\ntype: user\n---\n第二行内容\ndescription: 不该被采用\n",
        encoding="utf-8")
    headers = mem.scan_memory_headers()
    assert headers[0].description == "只在头部"
    assert headers[0].type == "user"


# ─── ③ 既有契约不变：预算与文件上限仍生效 ────────────────────


def test_session_budget_still_blocks(memory_dir):
    (memory_dir / "a.md").write_text("---\ndescription: x\n---\nbody\n", encoding="utf-8")

    async def run():
        return mem.start_memory_prefetch(
            "what is the deployment convention" * 3,
            _side_query_returning([]), set(), mem.MAX_SESSION_MEMORY_BYTES)

    assert asyncio.run(run()) is None


def test_no_memories_returns_none(memory_dir):
    async def run():
        return mem.start_memory_prefetch(
            "what is the deployment convention", _side_query_returning([]), set(), 0)

    assert asyncio.run(run()) is None


# ─── ④ L3 过期降权：候选超限时按新鲜度排序再截断 ──────────────


def test_lifecycle_sorts_candidates_by_freshness_when_over_limit(memory_dir):
    """候选超限时截断 = 最新优先（L3 显式定序）。

    事实核查：scan_memory_headers 本就按 mtime 降序（既有隐式行为），所以此处
    显式排序今天的输出与旧行为一致——它的价值是**防漂移**（上游排序将来若改变，
    截断语义不再跟着变），不是当天收益。真正的行为改变在 manifest 陈旧标注。
    """
    import os as _os
    import time as _time

    now = _time.time()
    names = []
    for index in range(9):
        name = f"m{index}.md"
        path = memory_dir / name
        path.write_text(f"---\nname: m{index}\ntype: project\ndescription: 记忆{index}\n---\n内容\n",
                        encoding="utf-8")
        stamp = now - (100 - index) * 86400  # m0 最旧 … m8 最新
        _os.utime(path, (stamp, stamp))
        names.append(name)

    async def run():
        handle = mem.start_memory_prefetch(
            "what are the memory conventions here",
            _side_query_returning(names), set(), 0)
        assert handle is not None
        return [Path(item.path).name for item in await handle.task]

    recalled = asyncio.run(run())
    assert len(recalled) == mem.MAX_MEMORY_RECALL
    assert recalled == ["m8.md", "m7.md", "m6.md", "m5.md", "m4.md"], (
        f"截断必须显式按 mtime 降序保留最新 5 条，实际 {recalled}")


def test_lifecycle_toggle_does_not_change_truncation_order(memory_dir, monkeypatch):
    """开关不应改变截断顺序：显式定序与既有隐式排序一致，
    否则说明截断语义被改动（A/B 两腿必须只差 manifest 标注）。"""
    import os as _os
    import time as _time

    now = _time.time()
    names = []
    for index in range(9):
        name = f"m{index}.md"
        path = memory_dir / name
        path.write_text(f"---\nname: m{index}\ntype: project\ndescription: 记忆{index}\n---\n内容\n",
                        encoding="utf-8")
        stamp = now - (100 - index) * 86400
        _os.utime(path, (stamp, stamp))
        names.append(name)

    async def run():
        handle = mem.start_memory_prefetch(
            "what are the memory conventions here",
            _side_query_returning(names), set(), 0)
        return [Path(item.path).name for item in await handle.task]

    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "1")
    on = asyncio.run(run())
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "0")
    off = asyncio.run(run())
    assert on == off, f"开关不得改变截断顺序：on={on} off={off}"


# ─── ⑤ L3 的真实行为改变：manifest 陈旧标注 ────────────────


def test_manifest_marks_stale_memories_when_lifecycle_on(monkeypatch):
    """陈旧记忆在 manifest 里带 [stale Nd]，让选择器能降权——这是行为改变本身。"""
    import time as _time

    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "1")
    now_ms = _time.time() * 1000
    headers = [
        mem.MemoryHeader(filename="fresh.md", file_path="/x/fresh.md",
                         description="新记忆", type="project",
                         mtime_ms=now_ms - 2 * 86_400_000),
        mem.MemoryHeader(filename="old.md", file_path="/x/old.md",
                         description="旧记忆", type="project",
                         mtime_ms=now_ms - 45 * 86_400_000),
    ]
    manifest = mem.format_memory_manifest(headers)
    assert "[stale 45d]" in manifest, f"45 天的记忆必须被标注，实际：{manifest}"
    assert "fresh.md" in manifest
    assert "[stale 2d]" not in manifest, "2 天的记忆未过 30 天阈值，不应标注"


def test_manifest_no_stale_marker_when_lifecycle_off(monkeypatch):
    """对照腿：关闭后 manifest 与旧行为逐字一致（只差这一个标注）。"""
    import time as _time

    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "0")
    now_ms = _time.time() * 1000
    headers = [
        mem.MemoryHeader(filename="old.md", file_path="/x/old.md",
                         description="旧记忆", type="project",
                         mtime_ms=now_ms - 45 * 86_400_000),
    ]
    manifest = mem.format_memory_manifest(headers)
    assert "stale" not in manifest
    assert "old.md" in manifest and "旧记忆" in manifest


def test_stale_threshold_is_configurable(monkeypatch):
    import time as _time

    monkeypatch.setenv("EVOHARNESS_MEMORY_STALE_DAYS", "1")
    monkeypatch.setenv("EVOHARNESS_MEMORY_LIFECYCLE", "1")
    now_ms = _time.time() * 1000
    headers = [
        mem.MemoryHeader(filename="d2.md", file_path="/x/d2.md", description="d",
                         type="project", mtime_ms=now_ms - 2 * 86_400_000),
    ]
    assert "[stale 2d]" in mem.format_memory_manifest(headers), (
        "阈值可配：设成 1 天后 2 天的记忆必须被标注")
