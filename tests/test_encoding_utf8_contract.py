"""E-01 回归：持久化文本的 UTF-8 契约（默认 Windows 本地编码下的读写）。

背景：项目在默认 Windows 环境（本机 ACP=936 → cp936）下，记忆/索引/工具读写的文本
若无显式编码，会走 locale 编码。由此产生三类真实缺陷：
  ① 以 UTF-8 写出的文件（新版工具、导入的 skill、测试数据）被按 cp936 读 → 解码失败，
     错误被上层 `except Exception` 静默吞掉 →「记忆未召回」；
  ② 两个写入路径编码不一致（一个 locale、一个 UTF-8）→ 同一 MEMORY.md 由任一方读都出错；
  ③ 跨进程/跨机器迁移后同一文件解码结果不一致。

修复：所有持久化文本读写显式 UTF-8；旧编码文件提供显式迁移路径并留诊断事件。
本文件只做纯函数 / 临时文件 / 子进程用例，零模型。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CJK = "用户偏好"                    # cp936 可编码；其 cp936 字节是非 UTF-8 序列。
CJK_BODY = "记忆内容：包含中文与特殊符号 → ★ …"


def _utf8_ok(path: Path) -> None:
    """断言文件可被严格按 UTF-8 解码（不得依赖 locale）。"""
    raw = path.read_bytes()
    raw.decode("utf-8")  # 失败即抛 UnicodeDecodeError，即回归。


# ── 写入侧：落盘必须是 UTF-8 ──────────────────────────────────────────────

def test_save_memory_writes_utf8_bytes(monkeypatch, tmp_path):
    import agents.memory as memory

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    monkeypatch.setattr(memory, "get_memory_dir", lambda: mem_dir)

    filename = memory.save_memory(CJK, "中文说明", "user", CJK_BODY)
    mem_file = mem_dir / filename
    assert mem_file.exists()
    _utf8_ok(mem_file)
    assert CJK in mem_file.read_text(encoding="utf-8")
    # 索引同样必须是 UTF-8。
    _utf8_ok(mem_dir / "MEMORY.md")


def test_auto_update_memory_index_writes_utf8_bytes(monkeypatch, tmp_path):
    import agents.memory as memory
    import agents.tools as tools

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    monkeypatch.setattr(memory, "get_memory_dir", lambda: mem_dir)
    monkeypatch.setattr(tools, "get_memory_dir", lambda: mem_dir)

    # 一个以 UTF-8 写出的记忆文件（模拟新版工具/外部导入）。
    (mem_dir / "user_seed.md").write_text(
        f"---\nname: {CJK}\ndescription: 中文说明\ntype: user\n---\n{CJK_BODY}\n",
        encoding="utf-8",
    )
    tools._auto_update_memory_index(str(mem_dir / "user_seed.md"))

    index = mem_dir / "MEMORY.md"
    assert index.exists()
    _utf8_ok(index)
    text = index.read_text(encoding="utf-8")
    assert CJK in text  # 若按 cp936 读 UTF-8 文件会解码失败并静默跳过，名不会出现。


# ── 读取侧：以 UTF-8 写出的文件必须被正确读回（修复前会静默丢记忆） ─────────

def test_list_memories_reads_utf8_file(monkeypatch, tmp_path):
    import agents.memory as memory

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "user_x.md").write_text(
        f"---\nname: {CJK}\ndescription: 说明\ntype: user\n---\n{CJK_BODY}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(memory, "get_memory_dir", lambda: mem_dir)

    entries = memory.list_memories()
    assert [e.name for e in entries] == [CJK]
    assert CJK_BODY in entries[0].content


def test_load_memory_index_reads_utf8_file(monkeypatch, tmp_path):
    import agents.memory as memory

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "MEMORY.md").write_text(
        f"# Memory Index\n\n- **[{CJK}](user_x.md)** (user) — 中文说明\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(memory, "get_memory_dir", lambda: mem_dir)
    assert CJK in memory.load_memory_index()


def test_read_file_tool_reads_gbk_content(tmp_path):
    """合法 GBK 文件在 cp936 机器上必须被正确读取（强制 utf-8+replace 会得到替换字符）。"""
    import agents.tools as tools

    target = tmp_path / "gbk.txt"
    target.write_bytes(CJK_BODY.encode("gbk"))
    out = tools._read_file({"file_path": str(target)})
    assert "Error" not in out
    assert CJK_BODY in out


def test_read_file_tool_reads_utf8_content(monkeypatch, tmp_path):
    import agents.tools as tools

    target = tmp_path / "note.txt"
    target.write_text(CJK_BODY, encoding="utf-8")
    out = tools._read_file({"file_path": str(target)})
    assert "Error" not in out
    assert CJK_BODY in out  # 按 cp936 读会得到替换字符，中文消失。


def test_write_then_read_file_tool_roundtrip_cjk(monkeypatch, tmp_path):
    import agents.tools as tools

    target = tmp_path / "out.txt"
    w = tools._write_file({"file_path": str(target), "content": CJK_BODY})
    assert "Error" not in w
    _utf8_ok(target)  # 落盘即 UTF-8 契约
    out = tools._read_file({"file_path": str(target)})
    assert CJK_BODY in out


# ── 旧编码文件：显式迁移路径 + 诊断事件 ───────────────────────────────────

def test_legacy_encoded_memory_is_recalled_without_destructive_write(monkeypatch, tmp_path):
    import agents.memory as memory

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    legacy = mem_dir / "user_legacy.md"
    original_bytes = (
        f"---\nname: {CJK}\ndescription: 说明\ntype: user\n---\n".encode("gbk")
        + CJK_BODY.encode("gbk")  # 旧安装里以本地编码写出的正文
    )
    legacy.write_bytes(original_bytes)
    monkeypatch.setattr(memory, "get_memory_dir", lambda: mem_dir)

    # 不再静默丢记忆：旧文件仍被召回，且内容正确（不是乱码）。
    entries = memory.list_memories()
    assert [e.name for e in entries] == [CJK]
    assert "记忆内容" in entries[0].content

    # 读取是只读的：绝不因读取而改写旧文件（避免把混合/残缺编码固化成乱码）。
    assert legacy.read_bytes() == original_bytes
    diag = mem_dir / "encoding_diagnostics.jsonl"
    events = [json.loads(l) for l in diag.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(e.get("event") == "legacy_encoding_read" for e in events)
    assert not any(e.get("event") == "legacy_encoding_migrated" for e in events)


def test_explicit_migration_rewrites_legacy_as_utf8(monkeypatch, tmp_path):
    import agents.memory as memory

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    legacy = mem_dir / "user_legacy.md"
    legacy.write_bytes("---\nname: a\n---\n".encode("gbk") + CJK_BODY.encode("gbk"))
    monkeypatch.setattr(memory, "get_memory_dir", lambda: mem_dir)

    assert memory.migrate_legacy_encoding(legacy) is True
    _utf8_ok(legacy)                                   # 迁移后为合法 UTF-8
    assert CJK_BODY in legacy.read_text(encoding="utf-8")
    # 幂等：已是 UTF-8 的文件不再改写。
    assert memory.migrate_legacy_encoding(legacy) is False


def test_read_text_migrating_never_rewrites_ambiguously_encoded_file(tmp_path):
    """混合编码文件（UTF-8 CJK + 尾部 gbk 字节）按严格 cp936 可解码，但不得被写回。"""
    import agents.memory as memory

    p = tmp_path / "mixed.md"
    original = "name: 用户\n".encode("utf-8") + "你".encode("gbk")
    p.write_bytes(original)
    text = memory.read_text_migrating(p)
    assert isinstance(text, str)
    assert p.read_bytes() == original  # 关键：读取不产生破坏性写回


def test_read_text_migrating_passthrough_utf8(tmp_path):
    import agents.memory as memory

    p = tmp_path / "plain.md"
    p.write_text(CJK_BODY, encoding="utf-8")
    assert memory.read_text_migrating(p) == CJK_BODY


# ── 跨进程：默认编码环境（PYTHONUTF8=0）下写入仍必须是 UTF-8 ──────────────

def test_cross_process_write_is_utf8(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = (
        "import sys\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from agents import memory\n"
        f"fn = memory.save_memory('{CJK}', '中文说明', 'user', '{CJK_BODY}')\n"
        "print(fn)\n"
    )
    env = dict(os.environ, PYTHONUTF8="0", HOME=str(home), USERPROFILE=str(home))
    env.pop("PYTHONIOENCODING", None)
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=str(ROOT), env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr

    # 子进程落盘的记忆必须可被父进程按 UTF-8 读回。
    mem_dirs = list(home.glob(".evoharness/projects/*/memory"))
    assert mem_dirs, "子进程未创建 memory 目录"
    files = [f for f in mem_dirs[0].glob("*.md") if f.name != "MEMORY.md"]
    assert files, "子进程未写入记忆文件"
    for f in files:
        _utf8_ok(f)
    index = mem_dirs[0] / "MEMORY.md"
    assert index.exists()
    _utf8_ok(index)


def test_index_rebuild_consistent_after_reload(monkeypatch, tmp_path):
    import agents.memory as memory

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    monkeypatch.setattr(memory, "get_memory_dir", lambda: mem_dir)

    f1 = memory.save_memory("用户偏好", "d1", "user", "内容一")
    f2 = memory.save_memory("项目目标", "d2", "project", "内容二")

    idx = memory.load_memory_index()
    assert "用户偏好" in idx and "项目目标" in idx
    # 索引重建幂等且与 list_memories 一致。
    memory._update_memory_index()
    names = {e.filename for e in memory.list_memories()}
    assert names == {f1, f2}
    for name in names:
        assert name in memory.load_memory_index()
