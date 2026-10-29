"""M1 输入预检回归测试：估算、边界、头尾截断、存档落盘。"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.eval_input_prep import estimate_tokens, precheck_payload, write_archive  # noqa: E402


def test_estimate_tokens_ceil_and_floor():
    assert estimate_tokens("") == 1  # 空串仍返回 1，绝不出现 0（防"静默空"）
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("x" * 4000) == 1000
    assert estimate_tokens("x" * 4001) == math.ceil(4001 / 4)


def test_short_text_not_truncated():
    text = "简短的用户消息"
    result = precheck_payload(text, max_tokens=6000)
    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["text"] == text
    assert result["archive_hint"] == ""
    assert result["tokens"] == estimate_tokens(text)


def test_long_text_truncated_non_empty_with_hint():
    text = "A" * 10000 + "B" * 10000  # 20000 字符 → 5000 tokens
    result = precheck_payload(text, max_tokens=1000)  # 预算 4000 字符
    assert result["truncated"] is True
    assert result["ok"] is False
    assert result["text"]  # 绝不静默空
    assert len(result["text"]) < len(text)
    assert result["text"].startswith("A")  # 头保留
    assert result["text"].endswith("B")  # 尾保留
    assert result["archive_hint"]
    assert result["archive_hint"].endswith(".txt")
    assert "/truncated/" in result["archive_hint"]


def test_truncation_is_deterministic():
    text = "X" * 30000
    first = precheck_payload(text, max_tokens=1000)
    second = precheck_payload(text, max_tokens=1000)
    assert first == second


def test_exact_max_tokens_boundary_not_truncated():
    max_tokens = 100
    text = "z" * (max_tokens * 4)  # ceil(len/4) == max_tokens
    result = precheck_payload(text, max_tokens=max_tokens)
    assert result["tokens"] == max_tokens
    assert result["truncated"] is False
    assert result["text"] == text

    # 多一个字符即越过边界 → 截断
    over = text + "z"
    result_over = precheck_payload(over, max_tokens=max_tokens)
    assert result_over["tokens"] == max_tokens + 1
    assert result_over["truncated"] is True


def test_empty_string():
    result = precheck_payload("", max_tokens=6000)
    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["text"] == ""
    assert result["tokens"] == 1
    assert result["archive_hint"] == ""


def test_write_archive_creates_parent_and_full_content(tmp_path):
    text = "全文内容\n第二行\n" + "尾巴" * 100
    target = tmp_path / "nested" / "deep" / "archived.txt"
    returned = write_archive(text, str(target))
    assert returned == str(target)
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == text


def test_archive_hint_matches_written_full_text_location(tmp_path):
    # archive_hint 与 write_archive 配对使用：把全文写到 hint 路径后内容逐字完整。
    text = "Q" * 40000
    result = precheck_payload(text, max_tokens=100)
    hint_path = tmp_path / Path(result["archive_hint"]).name
    write_archive(text, str(hint_path))
    assert hint_path.read_text(encoding="utf-8") == text
