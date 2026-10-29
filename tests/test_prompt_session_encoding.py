"""CLAUDE.md/rules 与会话读取的编码回归测试。

背景：load_claude_md / _load_rules_dir / _resolve_includes 与 load_session /
list_sessions 原用默认 locale 读取（Path.read_text() 不带 encoding）。默认中文
Windows（cp936）下读 UTF-8 文件抛 UnicodeDecodeError，被 except 静默吞掉 →
项目规则整体丢失、会话文件乱码。现改为显式 encoding="utf-8"。

本文件用「read_text 必带 utf-8」守卫复现：修复前守卫断言失败，修复后通过。
零模型、零网络。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import prompt as P  # noqa: E402
from agents import session as S  # noqa: E402


def _guard_read_text_utf8(monkeypatch) -> None:
    """把 Path.read_text 换成只接受 encoding="utf-8" 的守卫版本。"""
    original = Path.read_text

    def guarded(self, *args, **kwargs):
        assert kwargs.get("encoding") == "utf-8", f"read_text without utf-8: {self}"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)


def test_load_claude_md_reads_utf8_explicitly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# 项目规则\n中文说明内容\n", encoding="utf-8")
    _guard_read_text_utf8(monkeypatch)

    text = P.load_claude_md()
    assert "中文说明内容" in text


def test_load_rules_dir_reads_utf8_explicitly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rules_dir = tmp_path / ".evoharness" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "a.md").write_text("- 规则一：中文内容\n", encoding="utf-8")
    _guard_read_text_utf8(monkeypatch)

    text = P.load_claude_md()
    assert "规则一：中文内容" in text


def test_resolve_includes_reads_utf8_explicitly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.md").write_text("@./child.md\n", encoding="utf-8")
    (tmp_path / "child.md").write_text("包含的中文内容\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("@./inc.md\n", encoding="utf-8")
    _guard_read_text_utf8(monkeypatch)

    text = P.load_claude_md()
    assert "包含的中文内容" in text


def test_load_session_reads_utf8_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SESSION_DIR", tmp_path)
    (tmp_path / "sess-1.json").write_text(
        json.dumps({"note": "会话中文内容"}, ensure_ascii=False), encoding="utf-8"
    )
    _guard_read_text_utf8(monkeypatch)

    assert S.load_session("sess-1") == {"note": "会话中文内容"}


def test_list_sessions_reads_utf8_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SESSION_DIR", tmp_path)
    (tmp_path / "sess-2.json").write_text(
        json.dumps({"metadata": {"title": "标题中文"}, "note": "内容"}, ensure_ascii=False),
        encoding="utf-8",
    )
    _guard_read_text_utf8(monkeypatch)

    rows = S.list_sessions()
    assert any(item.get("title") == "标题中文" for item in rows)
