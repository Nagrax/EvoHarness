"""泄漏检查的假阳性回归（2026-10-29 真实事件）。

事件：我给 `/api/skills` 加的安全用例用 `"sk-" in payload` 做子串匹配，
结果真实仓库里 **`task-specific` 的 `ta-sk--specific`** 被当成凭据泄漏 →
契约测试报 1 个 high 失败（21 → 20 通过）。

教训（与 `skill 名/前缀匹配` 那几次同源）：**子串不是模式**。
凭据要按形态匹配（长度、前缀、赋值结构），否则 `risk-` / `disk-` / `task-` 全中。

本测试固定：① `task-specific` 这类不含真 key 的元素不报 ② 真 key 形态必须报。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# 与 backend_contract.py 保持一致的凭据形态
SECRET_PATTERNS = (
    r"sk-[A-Za-z0-9_-]{20,}",
    r"sk-ant-[A-Za-z0-9_-]{10,}",
    r'"api_key"\s*:\s*"[^"]{8,}"',
    r"anthropic_api_key\s*=\s*\S{8,}",
)


def _leaks(payload: str) -> list[str]:
    return [p for p in SECRET_PATTERNS if re.search(p, payload)]


def test_task_specific_is_not_a_secret():
    """`ta-sk--specific` 曾造成一次假 high 失败。"""
    payload = '{"description": "conversations and requiring skill invocation, not used in this task-specific execution."}'
    assert not _leaks(payload), f"task-specific 被误报：{_leaks(payload)}"


def test_other_sk_prefix_words_not_secrets():
    for word in ("risk-free", "disk-io", "mask-mode", "task-skill"):
        assert not _leaks(f"prefix {word} suffix"), f"{word} 被误报"


def test_real_openai_style_key_is_detected():
    fake = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    assert _leaks(f'{{"key": "{fake}"}}'), "真 OpenAI 风格 key 必须被检出"


def test_real_anthropic_key_is_detected():
    fake = "sk-ant-api03-" + "x" * 30
    assert _leaks(f'{{"k": "{fake}"}}')


def test_api_key_field_with_value_is_detected():
    assert _leaks('{"api_key": "abcdefgh12345678"}')


def test_empty_or_short_api_key_not_flagged():
    """字段名出现但值为空/很短，不算泄漏（避免又一类误报）。"""
    assert not _leaks('{"api_key": ""}')
    assert not _leaks('{"api_key": "short"}')


def test_contract_uses_patterns_not_bare_substring():
    src = (REPO / "eval" / "backend_contract.py").read_text(encoding="utf-8")
    assert "_secret_patterns" in src, "必须用形态模式"
    assert '"sk-" in payload' not in src, "不得回退到裸子串匹配"