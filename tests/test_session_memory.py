"""agents/session_memory 纯函数回归测试（零模型、零 I/O）。

覆盖三个遗留缺陷：
- _content_text：tool_result.content 为 block 列表时不得折叠出 Python repr；
- _clip：输出长度必须 <= limit（limit 很小时旧实现会超限）；
- _extract_json_text：只取首个完整（平衡）JSON 对象，不跨对象/不吞尾部散文。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.session_memory import (  # noqa: E402
    _clip,
    _content_text,
    _extract_json_text,
    parse_folded_memory,
)


# --------------------------------------------------------------------------- #
# _content_text：tool_result.content block 列表
# --------------------------------------------------------------------------- #
def test_content_text_tool_result_block_list_is_readable():
    content = [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [
                {"type": "text", "text": "inner"},
                {"type": "text", "text": "second"},
            ],
        }
    ]
    out = _content_text(content)
    assert "inner" in out and "second" in out
    # 旧实现 str(list) 会折叠出 Python repr；修复后不应出现
    assert "[{" not in out
    assert "'type'" not in out


def test_content_text_tool_result_str_content_unchanged():
    content = [{"type": "tool_result", "tool_use_id": "t1", "content": "plain"}]
    assert "plain" in _content_text(content)


# --------------------------------------------------------------------------- #
# _clip：输出长度 <= limit
# --------------------------------------------------------------------------- #
def test_clip_output_never_exceeds_limit():
    text = "x" * 5000
    for limit in (0, 5, 40, 100, 200, 279, 280, 1000, 12000):
        assert len(_clip(text, limit)) <= limit, limit
    big = _clip(text, 1000)
    assert big.startswith("x") and big.endswith("x")
    assert "clipped" in big


def test_clip_short_text_returned_verbatim():
    assert _clip("short", 100) == "short"
    assert _clip("", 100) == ""


# --------------------------------------------------------------------------- #
# _extract_json_text：平衡匹配首个对象
# --------------------------------------------------------------------------- #
def test_extract_json_text_takes_first_balanced_object():
    text = '{"a": {"b": 1}, "c": 2}\nprose trailing {"d": 3}'
    # 旧贪婪正则会吞成 '{"a": {...}, "c": 2}\nprose trailing {"d": 3}' → 非法 JSON
    assert json.loads(_extract_json_text(text)) == {"a": {"b": 1}, "c": 2}


def test_extract_json_text_handles_braces_inside_strings():
    text = '{"note": "use { and } literally", "n": 1} tail prose'
    assert json.loads(_extract_json_text(text)) == {"note": "use { and } literally", "n": 1}


def test_extract_json_text_from_fenced_block():
    text = 'prefix\n```json\n{"ok": true}\n```\nsuffix'
    assert json.loads(_extract_json_text(text)) == {"ok": True}


def test_parse_folded_memory_from_text_with_trailing_prose():
    payload = {
        "episode_memory": {"task_description": "t"},
        "working_memory": {},
        "tool_memory": {},
    }
    text = json.dumps(payload) + "\n\nNote: this is the compacted memory."
    parsed = parse_folded_memory(text)
    assert parsed["episode_memory"]["task_description"] == "t"
