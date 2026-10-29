"""回归测试：修复三类遗留缺陷。

- memory 的"字节上限"（MAX_INDEX_BYTES / MAX_MEMORY_BYTES_PER_FILE）原先实为字符
  切片（content[:N]），CJK 记忆会让注入/索引体量约为声明上限的 3 倍；现按 UTF-8 字节
  截断且不切断多字节字符。
- save_memory 的 type 未净化，含 "../" 可越出 memory 目录；现降级为合法类型。
- parse_frontmatter 不处理 BOM 与引号：带 BOM 文件首行非 "---" 导致 meta 全丢；引号
  包裹的 value（如 allowed-tools: "read_file, grep_search"）会残留引号使工具集为空。

全部为纯函数 / 临时文件用例，零模型。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.frontmatter import parse_frontmatter
from agents.memory import (
    MAX_INDEX_BYTES,
    MAX_MEMORY_BYTES_PER_FILE,
    _truncate_utf8,
)

CJK = "\u8bb0"  # 单字符 UTF-8 占 3 字节。


def test_truncate_utf8_caps_bytes_without_splitting_cjk():
    out = _truncate_utf8(CJK * 2000, 100)
    assert len(out.encode("utf-8")) <= 100
    # 不切断多字节字符：结果是完整字符的重复。
    assert out == CJK * 33
    assert _truncate_utf8("abc", 100) == "abc"


def test_load_memory_index_caps_bytes_not_chars(monkeypatch, tmp_path):
    import agents.memory as memory

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    # 字符数（20000）小于 MAX_INDEX_BYTES，但字节数（60000）远超上限。
    (memory_dir / "MEMORY.md").write_text(CJK * 20000, encoding="utf-8")
    monkeypatch.setattr(memory, "get_memory_dir", lambda: memory_dir)

    result = memory.load_memory_index()
    payload = result.split("\n\n[... truncated, index too large ...]")[0]
    capped = len(payload.encode("utf-8"))
    assert capped <= MAX_INDEX_BYTES
    assert capped > MAX_INDEX_BYTES - 3  # 贴近上限，未过度截断


def test_selected_memory_caps_bytes_not_chars(monkeypatch, tmp_path):
    import agents.memory as memory

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    big = "---\nname: big\ndescription: big\ntype: user\n---\n" + CJK * 3000
    (memory_dir / "user_big.md").write_text(big, encoding="utf-8")
    monkeypatch.setattr(memory, "get_memory_dir", lambda: memory_dir)

    async def fake_side_query(system: str, user: str) -> str:
        return '{"selected_memories": ["user_big.md"]}'

    async def run():
        return await memory.select_relevant_memories(
            query="tell me the big memory",
            side_query=fake_side_query,
            already_surfaced=set(),
        )

    selected = asyncio.run(run())
    assert len(selected) == 1
    payload = selected[0].content.split(
        "\n\n[... truncated, memory file too large ...]"
    )[0]
    assert len(payload.encode("utf-8")) <= MAX_MEMORY_BYTES_PER_FILE


def test_save_memory_sanitizes_type_path_traversal(monkeypatch, tmp_path):
    import agents.memory as memory

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    monkeypatch.setattr(memory, "get_memory_dir", lambda: memory_dir)

    filename = memory.save_memory("My Name", "d", "../evil", "hi")
    # 保留可读前缀，并附加完整原名的短哈希后缀以保证全局唯一。
    assert filename.startswith("project_my_name_") and filename.endswith(".md")
    assert (memory_dir / filename).exists()
    # 目录外没有被写入。
    assert not any(tmp_path.glob("evil_*"))


def test_parse_frontmatter_strips_bom():
    result = parse_frontmatter("\ufeff---\nname: a\ndescription: b\n---\nbody")
    assert result.meta == {"name": "a", "description": "b"}
    assert result.body == "body"


def test_parse_frontmatter_strips_matching_quotes():
    result = parse_frontmatter('---\nallowed-tools: "read_file, grep_search"\n---\nx')
    assert result.meta["allowed-tools"] == "read_file, grep_search"
    # 未配对的引号保留原样，避免误删。
    assert parse_frontmatter("---\nname: a'b\n---\nx").meta["name"] == "a'b"


# ── _slugify：纯中文名不再被压成空串，且归一化结果全局唯一 ─────────────────────

def test_slugify_cjk_name_is_stable_and_unique():
    import agents.memory as memory

    a = memory._slugify("用户偏好")
    b = memory._slugify("项目目标")
    # 非空、不同中文名得到不同 slug、同名结果稳定。
    assert a and b and a != b
    assert memory._slugify("用户偏好") == a
    # ASCII 名保留可读前缀，并附加完整原名的区分后缀。
    ascii_slug = memory._slugify("My Name")
    assert ascii_slug.startswith("my_name_")
    assert memory._slugify("My Name") == ascii_slug


def test_slugify_normalization_collisions_get_distinct_slugs():
    import agents.memory as memory

    # 'foo bar' 与 'foo-bar' 归一化到同一 'foo_bar'，修复前会落同一文件互相覆盖。
    assert memory._slugify("foo bar") != memory._slugify("foo-bar")
    # 不同中文名（归一化都为空串）也不会因兜底哈希而互相覆盖。
    assert memory._slugify("用户偏好") != memory._slugify("项目目标")


def test_save_memory_distinct_chinese_names_do_not_collide(monkeypatch, tmp_path):
    import agents.memory as memory

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    monkeypatch.setattr(memory, "get_memory_dir", lambda: memory_dir)

    f1 = memory.save_memory("用户偏好", "d", "user", "a")
    f2 = memory.save_memory("项目目标", "d", "project", "b")
    assert f1 != f2
    assert f1 != "user_.md"  # 修复前纯中文名会退化成 `{type}_.md`
    assert (memory_dir / f1).exists() and (memory_dir / f2).exists()


# ── MAX_MEMORY_RECALL：非法 env 安全回退 ─────────────────────────────────────

def test_env_int_falls_back_on_invalid_value(monkeypatch):
    import agents.memory as memory

    monkeypatch.setenv("EVOHARNESS_MAX_MEMORY_RECALL", "abc")
    assert memory._env_int("EVOHARNESS_MAX_MEMORY_RECALL", 5) == 5
    monkeypatch.setenv("EVOHARNESS_MAX_MEMORY_RECALL", "15")
    assert memory._env_int("EVOHARNESS_MAX_MEMORY_RECALL", 5) == 15
    monkeypatch.delenv("EVOHARNESS_MAX_MEMORY_RECALL")
    assert memory._env_int("EVOHARNESS_MAX_MEMORY_RECALL", 5) == 5


def test_import_memory_with_invalid_env_does_not_crash():
    env = dict(os.environ, EVOHARNESS_MAX_MEMORY_RECALL="not-a-number")
    out = subprocess.run(
        [sys.executable, "-c", "import agents.memory as m; print(m.MAX_MEMORY_RECALL)"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "5"
