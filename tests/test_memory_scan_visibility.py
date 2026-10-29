"""记忆解析失败可见性回归（2026-10-29 判读 agents/memory.py:261/475）。

判读结论：`except Exception: pass` 让坏记忆文件**静默消失**——用户与模型都以为它还在，
实际从未进入索引与召回。skill 侧早有 `skill_load_errors.jsonl` + 前端面板，
记忆层此前**没有对应机制**。
处置：结构化记录到 `last_scan_errors` + 汇总一行提示（不逐条刷屏，但故障可见）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import agents.memory as mem


@pytest.fixture()
def mem_dir(tmp_path, monkeypatch):
    d = tmp_path / "memories"
    d.mkdir()
    monkeypatch.setattr(mem, "get_memory_dir", lambda: d)
    monkeypatch.setattr(mem, "last_scan_errors", [])
    return d


def test_scan_error_recorded_not_swallowed(mem_dir, monkeypatch):
    """扫描期异常必须被记录，而不是静默消失。

    注意：用 monkeypatch 让 parser 抛异常——因为真实坏字节会被
    `read_text_migrating_limited` 的编码迁移容错处理，未必触发 except 分支。
    这里测的是"except 分支的行为"（修复本身），不是"能否构造坏文件"。
    """
    (mem_dir / "boom.md").write_text(
        "---\nname: boom\ntype: project\n---\nbody\n", encoding="utf-8")

    def _explode(*_args, **_kwargs):
        raise RuntimeError("frontmatter parser exploded")

    monkeypatch.setattr(mem, "parse_frontmatter", _explode)
    mem.last_scan_errors = []
    mem.scan_memory_headers()

    errors = mem.last_scan_errors
    assert errors, "扫描期异常必须被记录"
    assert any("boom.md" in e["name"] for e in errors), f"应记录文件名：{errors}"
    assert all("error" in e for e in errors), "必须记录原因"
    assert any("RuntimeError" in e["error"] for e in errors), "必须保留异常类型"


def test_good_memories_still_work(mem_dir):
    """记录错误不能影响正常记忆的加载。"""
    (mem_dir / "ok.md").write_text(
        "---\nname: ok\ntype: project\ndescription: 正常\n---\nbody\n", encoding="utf-8")
    (mem_dir / "broken.md").write_bytes(b"\xff\xfe bad")
    headers = mem.scan_memory_headers()
    assert any(h.filename == "ok.md" for h in headers), "正常记忆必须仍在"


def test_summary_prints_one_line_not_per_file(mem_dir, capsys):
    """汇总必须是"一行"，不是逐条刷屏。"""
    for i in range(8):
        (mem_dir / f"bad{i}.md").write_bytes(b"\xff\xfe bad")
    mem.last_scan_errors = []
    mem.scan_memory_headers()
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "[memory]" in l]
    assert len(lines) <= 1, f"应只打印一次汇总，实际 {len(lines)} 行"
    if lines:
        assert "解析失败" in lines[0]


def test_no_output_when_clean(mem_dir, capsys):
    """全部正常时不得有提示（否则日志变噪声）。"""
    (mem_dir / "ok.md").write_text(
        "---\nname: ok\ntype: project\ndescription: d\n---\nbody\n", encoding="utf-8")
    mem.last_scan_errors = []
    mem.scan_memory_headers()
    out = capsys.readouterr().out
    assert "[memory]" not in out, f"干净场景不该有输出：{out!r}"
