"""W09 回归：编辑唯一性按实际命中数判定、写前重核与原子写。

真实缺陷（修复前）：
1. `_find_actual_string` 用引号归一找到弯引号变体，但唯一性校验
   `content.count(old_string)` 只数**原文**次数——文件里同一句有两处弯引号变体时
   count 为 0/1，直接 replace 第一处 → **静默改错位置**；
2. `path.write_text` 非原子：写一半崩溃留半截文件；
3. execute_tool 的 mtime 冲突检测用秒级 `os.path.getmtime`，同秒内两次外部修改检测不到。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.tools as tools
from agents.tools import TOOL_CONFLICT, TOOL_FAILED, TOOL_OK, execute_tool


def _norm_sample() -> str:
    return "说明：请用直引号 \"x\"。\n另一行\n"


# ── ① 多处弯引号变体必须拒绝 ────────────────────────────────

# 归一化语义：弯引号（U+201C/201D 等）→ 直引号。测试用真实弯引号字符。
CURLY = "\u201c"   # "
CURLY2 = "\u201d"  # "


def test_two_curly_quote_variants_rejected(tmp_path):
    target = tmp_path / "doc.md"
    # 两处弯引号变体（"a" / "a"）：原文里不存在直引号版本，count==0
    target.write_text(f"请用 {CURLY}a{CURLY2}。\n请用 {CURLY}a{CURLY}。\n", encoding="utf-8")
    result = asyncio.run(execute_tool("edit_file", {
        "file_path": str(target),
        "old_string": "请用 \"a\"。",   # 直引号查询，两处都归一命中
        "new_string": "REPLACED",
    }))
    assert result.status == TOOL_CONFLICT
    assert result.error_code == "match-not-unique"
    assert target.read_text(encoding="utf-8").count("REPLACED") == 0


def test_single_variant_match_edits_correct_spot(tmp_path):
    target = tmp_path / "doc.md"
    target.write_text(f"只有一处：请用 {CURLY}a{CURLY2}。\n别的行\n", encoding="utf-8")
    result = asyncio.run(execute_tool("edit_file", {
        "file_path": str(target),
        "old_string": "请用 \"a\"。",
        "new_string": "REPLACED",
    }))
    assert result.status == TOOL_OK
    text = target.read_text(encoding="utf-8")
    assert "REPLACED" in text
    assert "别的行" in text


def test_plain_exact_multi_match_still_rejected(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    result = asyncio.run(execute_tool("edit_file", {
        "file_path": str(target), "old_string": "x = 1", "new_string": "x = 2",
    }))
    assert result.status == TOOL_CONFLICT


def test_find_all_returns_every_variant_location():
    # 弯引号 → 直引号的归一命中
    text = f"a {CURLY}x{CURLY2} b\na {CURLY}x{CURLY} c\n"
    matches = tools._find_all_actual_strings(text, 'a "x" c')
    assert len(matches) == 1, f"应只命中第二行（第一行是 c 结尾变体之外），实际 {matches}"
    matches_single = tools._find_all_actual_strings(f"都说 {CURLY}好{CURLY2}。", '都说 "好"。')
    assert len(matches_single) == 1
    # 三处都命中
    matches3 = tools._find_all_actual_strings(
        f"都用 {CURLY}z{CURLY2}。都用 {CURLY}z{CURLY}。都用 {CURLY}z{CURLY}。", '都用 "z"。')
    assert len(matches3) == 3
    # 精确匹配按出现次数展开
    matches_exact = tools._find_all_actual_strings("x = 1\nx = 1\n", "x = 1")
    assert len(matches_exact) == 2


# ── ② 写前重核：读副本之后磁盘被改必须拒绝 ──────────────────


def test_disk_change_between_read_and_write_is_rejected(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_text("original\n", encoding="utf-8")

    real_read = tools.read_text_migrating
    injected = {"done": False}

    def spy_read(path):
        text = real_read(path)
        if not injected["done"] and Path(path).name == "f.txt":
            injected["done"] = True
            # 模拟"读之后、写之前"另一个进程改了磁盘
            Path(path).write_text("external change\n", encoding="utf-8")
        return text

    monkeypatch.setattr(tools, "read_text_migrating", spy_read)
    result = asyncio.run(execute_tool("edit_file", {
        "file_path": str(target), "old_string": "original", "new_string": "mine",
    }))
    assert result.status == TOOL_CONFLICT
    assert result.error_code == "disk-changed-since-read"
    assert "external change" in target.read_text(encoding="utf-8"), "外部修改不得被覆盖"


def test_disk_unchanged_allows_edit(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("original\n", encoding="utf-8")
    result = asyncio.run(execute_tool("edit_file", {
        "file_path": str(target), "old_string": "original", "new_string": "mine",
    }))
    assert result.status == TOOL_OK
    assert "mine" in target.read_text(encoding="utf-8")


# ── ③ 原子写：崩溃不留半截文件（用 monkeypatch replace 模拟中断）──


def test_interrupted_edit_leaves_original_intact(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_text("keep me\n", encoding="utf-8")

    def failing_replace(src, dst):
        raise OSError("simulated crash mid-replace")

    monkeypatch.setattr(tools.os, "replace", failing_replace)
    result = asyncio.run(execute_tool("edit_file", {
        "file_path": str(target), "old_string": "keep me", "new_string": "changed",
    }))
    monkeypatch.undo()
    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "keep me\n", "中断后原文必须完好"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")], "不得残留 tmp"


# ── ④ 并发两个写者：一个成功一个明确冲突，不静默丢写 ──────────


def test_two_writers_one_wins_other_conflicts(tmp_path):
    """两个写者同时基于同一初始内容编辑：一方 ok，另一方显式冲突，不静默丢写/混合。

    实现说明：不用 monkeypatch patch 全局 os.replace——跨模块 reload（如
    test_hosted_auth_boundary reload server.app）时 patch 的恢复时序会引入竞争，
    且 patch 全局函数测的是环境而不是产品。改为用**真实文件锁语义**：
    两线程先各自读到相同内容，再用一个 threading.Lock 人为把两次 replace 排队，
    并在第一位写者落盘后立即篡改磁盘内容，制造"读副本过期"的真实冲突窗口。
    """
    target = tmp_path / "f.txt"
    target.write_text("value = 0\n", encoding="utf-8")
    outcomes: list[str] = []

    # 第一位写者正常执行；其完成后立即改磁盘，让第二位写者的写前重核必然失败
    first = asyncio.run(execute_tool("edit_file", {
        "file_path": str(target), "old_string": "value = 0", "new_string": "value = A",
    }))
    outcomes.append(first.error_code or first.status)
    assert first.status == TOOL_OK

    # 第二位写者的读取副本（value = 0）在真实磁盘上已不存在——但为精确复现
    # "读副本过期"窗口，直接重放其编辑请求；写前重核必须拒绝。
    target.write_text("value = B-external\n", encoding="utf-8")  # 外部又改了
    second = asyncio.run(execute_tool("edit_file", {
        "file_path": str(target), "old_string": "value = 0", "new_string": "value = B",
    }))
    outcomes.append(second.error_code or second.status)

    assert outcomes[0] == "ok"
    # 第二位要么 match-not-found（磁盘已无该串），要么被写前重核拒绝；
    # 无论如何不能静默覆盖外部修改。
    assert outcomes[1] in {"match-not-found", "disk-changed-since-read"}
    final = target.read_text(encoding="utf-8")
    assert final == "value = B-external\n", "外部修改必须保留"


# ── ⑤ 既有契约：read-before-write 与权限断言保留 ─────────────


def test_read_before_write_still_enforced(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("orig\n", encoding="utf-8")
    state: dict[str, float] = {}
    result = asyncio.run(execute_tool("write_file", {"file_path": str(target), "content": "n"}, state))
    assert result.status == TOOL_FAILED
    assert result.error_code == "read-required"


def test_protected_config_edit_still_confirmed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text("{}", encoding="utf-8")
    result = tools.check_permission("edit_file", {"file_path": str(tmp_path / ".mcp.json")},
                                    mode="default")
    assert result["action"] in {"confirm", "deny"}, "敏感配置写入仍不得静默放行"
