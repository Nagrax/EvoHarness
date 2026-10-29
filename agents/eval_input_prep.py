"""M1 输入预检：粗略 token 估算、头/尾保留式截断与全文存档。

纯 stdlib。核心不变量：任何超长输入都不得退化成"静默空"——超过预算一律走
头 + 尾保留截断，返回的 text 始终非空，且 archive_hint 指向全文存档路径，
供调用方按需落盘全文（见 write_archive）。
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

DEFAULT_MAX_TOKENS = 6000
_CHARS_PER_TOKEN = 4
_HEAD_RATIO = 0.6
_TAIL_RATIO = 0.4
_ARCHIVE_SUBDIR = "truncated"


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：max(1, ceil(len(text) / 4))。"""
    return max(1, math.ceil(len(str(text or "")) / _CHARS_PER_TOKEN))


def _default_archive_dir() -> str:
    """确定性存档根目录（正斜杠形式，跨平台稳定）。"""
    try:
        from .skill_evolution import get_evolution_dir

        base = get_evolution_dir() / "online-eval" / "archive"
    except Exception:
        base = Path.cwd() / ".evoharness" / "skill-evolution" / "online-eval" / "archive"
    return base.as_posix()


def precheck_payload(text: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """预检文本负载。

    未超预算 → ok=True、truncated=False、text 原样、archive_hint 为空。
    超预算 → 头(约 60%) + 尾(约 40%) 字符预算保留式截断，truncated=True、
    text 为截断后非空文本、archive_hint 为确定性存档路径（sha1[:12].txt），
    ok=False 表示输入未按原样通过预检。
    """
    raw = str(text or "")
    tokens = estimate_tokens(raw)
    budget = int(max_tokens)
    if tokens <= budget:
        return {
            "ok": True,
            "tokens": tokens,
            "truncated": False,
            "text": raw,
            "archive_hint": "",
        }

    budget_chars = max(1, budget) * _CHARS_PER_TOKEN
    head_budget = int(budget_chars * _HEAD_RATIO)
    tail_budget = max(0, budget_chars - head_budget)
    head = raw[:head_budget]
    tail = raw[len(raw) - tail_budget:] if tail_budget > 0 else ""
    truncated_text = head + tail

    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    archive_hint = f"{_default_archive_dir()}/{_ARCHIVE_SUBDIR}/{digest}.txt"
    return {
        "ok": False,
        "tokens": tokens,
        "truncated": True,
        "text": truncated_text,
        "archive_hint": archive_hint,
    }


def write_archive(text: str, path: str) -> str:
    """UTF-8 写全文到 path（自动建父目录），返回 path。"""
    target = Path(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(text or ""), encoding="utf-8")
    return str(path)
