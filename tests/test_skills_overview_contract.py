"""/api/skills/overview 响应契约测试（2026-10-29）。

为什么补这个：该端点是**纯数据组装**（137 行主要是字典字面量），按 long-function
评估结论不该拆——但前端多个面板依赖它的字段结构。所以用契约测试**固定字段**，
比把它拆成几个函数更能防止"重构时悄悄改坏前端"。

这也回应"前端每个按钮都依赖后端响应"：契约变了按钮就白屏，而单测不覆盖就发现不了。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SRC = REPO / "server" / "app.py"


def _overview_source() -> str:
    text = SRC.read_text(encoding="utf-8")
    start = text.find("async def skills_overview")
    assert start > 0, "找不到 skills_overview"
    return text[start: start + 9000]


def test_skill_item_has_all_frontend_fields():
    """单个 skill 条目的字段是前端多处读取的契约。"""
    body = _overview_source()
    required = [
        '"skill"', '"status"', '"reasons"', '"lineage_id"', '"current_version"',
        '"source"', '"last_action"', '"last_time"',
        '"counts"', '"rates"', '"replay"', '"eval"', '"candidate_eval"',
        '"gates"', '"retrieval_weight"', '"champion"', '"last_reason"', '"last_score"',
    ]
    for field in required:
        assert field in body, f"响应缺少前端依赖的字段 {field}"


def test_counts_block_shape():
    body = _overview_source()
    for key in ('"retrieved"', '"relevant"', '"used"', '"created"',
                '"evolutions"', '"invocations"', '"feedback"'):
        assert key in body, f"counts 缺少 {key}"


def test_replay_dual_group_semantics_preserved():
    """replay 的 v2 双组（evidence=决策窗口 / score=成绩组）不得被合并。"""
    body = _overview_source()
    assert '"evidence"' in body and '"score"' in body
    assert "evidence_count" in body and "score_count" in body


def test_thresholds_fall_back_to_legacy_names():
    """旧报告用 min_replay_samples，新报告用 min_score_samples，必须兼容。"""
    body = _overview_source()
    assert "min_score_samples" in body
    assert "min_replay_samples" in body, "必须保留旧字段回退，否则旧报告读不出阈值"


def test_gate_meta_drives_gate_list():
    """gates 由 GATE_META 驱动（前端按 key 渲染），不能硬编码。"""
    body = _overview_source()
    assert "GATE_META" in body
    assert '"current"' in body and '"required"' in body and '"ok"' in body
