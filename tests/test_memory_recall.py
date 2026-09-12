"""P0/P1 回归测试：memory 召回的规范化匹配与可配置上限。

背景：LoCoMo 基准评测（见 MiniMem 仓库 benchmarks/locomo_bearcode/RESULTS.md）
发现 select_relevant_memories 对选择器返回值做精确文件名比较，而 LLM 常照抄
manifest 整行、包代码围栏或带路径前缀，导致这部分召回被静默丢弃（conv0 上
F1 从 13.96 修复到 33.01）。本文件固化该修复的证明链。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.memory import MAX_MEMORY_RECALL, SELECT_MEMORIES_PROMPT, _normalize_selector_ref

# 评测中实测遇到的选择器回指形态：第一种原版可匹配，后三种原版全部静默丢失。
CASES = {
    "裸文件名": "user_caroline_career.md",
    "照抄 manifest 整行": "user_caroline_career.md (2026-05-08T13:56:00)",
    "带代码围栏": '```json "user_caroline_career.md"',
    "带路径前缀": "memory/user_caroline_career.md",
}


def test_selector_ref_normalization_matches_all_observed_forms():
    key = _normalize_selector_ref("user_caroline_career.md")
    for name, raw in CASES.items():
        norm = _normalize_selector_ref(raw)
        assert key in norm or norm in key, f"{name}: 丢失召回 ({raw!r})"


def test_selector_ref_normalization_rejects_different_file():
    key = _normalize_selector_ref("user_caroline_career.md")
    other = _normalize_selector_ref("user_melanie_painting.md")
    assert not (key in other or other in key)


def test_prompt_cap_matches_configured_limit():
    # 提示词中的上限必须与 MAX_MEMORY_RECALL 同步，否则模型认知与实际截断不一致。
    assert "{max_select}" in SELECT_MEMORIES_PROMPT


def test_max_memory_recall_default():
    assert MAX_MEMORY_RECALL >= 1


def test_max_memory_recall_env_override(monkeypatch):
    import agents.memory as memory

    monkeypatch.setenv("EVOHARNESS_MAX_MEMORY_RECALL", "15")
    reloaded = importlib.reload(memory)
    assert reloaded.MAX_MEMORY_RECALL == 15
    monkeypatch.delenv("EVOHARNESS_MAX_MEMORY_RECALL")
    reloaded = importlib.reload(memory)
    assert reloaded.MAX_MEMORY_RECALL == 5
    # 结束后再 reload 一次，避免其他测试看到 monkeypatch 中的临时模块状态。
    importlib.reload(memory)


def test_recall_loop_end_to_end(monkeypatch, tmp_path):
    """端到端：模拟 side_query 返回'照抄 manifest 整行'的形态，召回不应丢失。"""
    import agents.memory as memory

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "user_caroline_career.md").write_text(
        "---\nname: caroline-career\ndescription: career facts\ntype: user\n---\n"
        "Caroline wants to pursue counseling.",
        encoding="utf-8",
    )
    (memory_dir / "user_melanie_painting.md").write_text(
        "---\nname: melanie-painting\ndescription: painting facts\ntype: user\n---\n"
        "Melanie painted a sunrise in 2022.",
        encoding="utf-8",
    )

    headers = [
        memory.MemoryHeader(
            filename=f.name,
            file_path=str(memory_dir / f.name),
            mtime_ms=1000.0,
            description=desc,
            type="user",
        )
        for f, desc in [
            (memory_dir / "user_caroline_career.md", "career facts"),
            (memory_dir / "user_melanie_painting.md", "painting facts"),
        ]
    ]

    async def fake_side_query(system: str, user: str) -> str:
        # 模拟真实选择器：返回 manifest 行而非裸文件名（原 bug 的触发形态）。
        return '{"selected_memories": ["user_caroline_career.md (2026-05-08T13:56:00)"]}'

    # 指向临时目录，不依赖真实 home 下的 memory 目录。
    monkeypatch.setattr(memory, "get_memory_dir", lambda: memory_dir)

    async def run():
        return await memory.select_relevant_memories(
            query="What does Caroline want to pursue?",
            side_query=fake_side_query,
            already_surfaced=set(),
        )

    import asyncio

    selected = asyncio.run(run())
    assert len(selected) == 1
    assert "caroline" in selected[0].path.lower()
