"""受控检索接缝：只传入飞书经验语料，不读取全局 Skill 状态。

优先复用 EvoHarness 的 BM25-lite 纯函数 `agents.skills.rank_corpus_for_query`；
该函数只做词项打分，不依赖 Agent 会话、工具权限或用户 home。若本机没有
EvoHarness 源码，降级到同口径的本地实现，并在返回值中记录引擎名，便于评测归因。
"""
from __future__ import annotations

import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

LAST_ENGINE = "local-bm25"

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+")
_STOP = {"的", "了", "是", "吗", "呢", "怎么", "如何", "什么", "请问", "一下", "可以", "需要"}


def _tokens(text: str) -> list[str]:
    raw = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens: list[str] = []
    for chunk in _TOKEN_RE.findall(raw):
        if "\u4e00" <= chunk[0] <= "\u9fff":
            tokens.append(chunk)
            if len(chunk) >= 2:
                tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
        else:
            tokens.append(chunk)
    return [token for token in tokens if token.strip() and token not in _STOP]


def _local_rank(query: str, corpus: dict[str, Iterable[str]]) -> list[tuple[str, float]]:
    query_tokens = set(_tokens(query))
    if not query_tokens or not corpus:
        return []
    docs = {key: list(tokens) for key, tokens in corpus.items()}
    df: Counter[str] = Counter()
    total_len = 0
    for tokens in docs.values():
        df.update(set(tokens))
        total_len += len(tokens)
    avg_len = total_len / max(1, len(docs))
    scored: list[tuple[str, float]] = []
    for key, tokens in docs.items():
        counts = Counter(tokens)
        overlap = query_tokens & set(tokens)
        if not overlap:
            continue
        raw = 0.0
        for token in overlap:
            tf = counts[token]
            idf = math.log(1 + (len(docs) - df[token] + 0.5) / (df[token] + 0.5))
            denom = tf + 1.4 * (1 - 0.75 + 0.75 * len(tokens) / max(1.0, avg_len))
            raw += idf * (tf * 2.4) / max(denom, 0.0001)
        scored.append((key, raw / max(3.0, len(query_tokens))))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def _evoharness_rank(query: str, corpus: dict[str, Iterable[str]]):
    root = os.environ.get("EVOHARNESS_ROOT", "").strip()
    if not root:
        candidate = Path(__file__).resolve().parents[3]
        if (candidate / "agents" / "skills.py").is_file():
            root = str(candidate)
    if not root or not (Path(root) / "agents" / "skills.py").is_file():
        return None
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from agents.skills import rank_corpus_for_query  # type: ignore

        return rank_corpus_for_query(query, {key: list(tokens) for key, tokens in corpus.items()})
    except Exception:
        return None


def rank(query: str, corpus: dict[str, Iterable[str]]) -> tuple[list[tuple[str, float]], str]:
    global LAST_ENGINE
    result = _evoharness_rank(query, corpus)
    if result is not None:
        LAST_ENGINE = "evoharness-bm25"
        return result, LAST_ENGINE
    LAST_ENGINE = "local-bm25"
    return _local_rank(query, corpus), LAST_ENGINE


def tokenize(text: str) -> list[str]:
    return _tokens(text)


def normalize_question(text: str) -> str:
    """精确模式匹配用：去掉空白、标点与大小写差异，保留中英文数字。"""
    return re.sub(r"[\W_]+", "", str(text or "").lower(), flags=re.UNICODE)
