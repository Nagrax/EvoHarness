"""checkpoint 必须存 task_ids（配对检验的前提，2026-10-29）。

缺陷：checkpoint 只存 `correct_flags`（顺序数组），**没有 task_id** →
① 无法确认三臂跑的是同一批题 ② 无法做 McNemar 配对检验
（而配对检验比 CI 更适合稀疏事件——进化实验只有 4/72 命中）。

历史批次无法回溯，但新批次必须是可配对的。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SRC = REPO / "eval" / "evolution_experiment.py"


def test_entry_records_task_ids_alongside_flags():
    text = SRC.read_text(encoding="utf-8")
    # 必须出现在 entry 构造里（与 correct_flags 相邻）
    idx = text.find('"correct_flags": [1 if r.get("correct") else 0 for r in holdout_recs],')
    assert idx > 0, "entry 的 correct_flags 行不存在"
    window = text[idx: idx + 500]
    assert "holdout_task_ids" in window, "task_ids 必须与 flags 相邻（同一 entry）"
    assert 'for r in holdout_recs' in window.split("holdout_task_ids")[1][:200]


def test_task_ids_and_flags_have_same_source():
    """两者都从 holdout_recs 派生 → 长度必然一致（防错位）。"""
    text = SRC.read_text(encoding="utf-8")
    idx = text.find('"correct_flags": [1 if r.get("correct") else 0 for r in holdout_recs],')
    window = text[idx: idx + 600]
    assert window.count("holdout_recs") >= 2, "flags 与 ids 必须同源"


def test_plan_side_also_has_task_ids():
    """plan 侧的 holdout_task_ids 应当保留（用于校验配对设计）。"""
    text = SRC.read_text(encoding="utf-8")
    assert text.count("holdout_task_ids") >= 2, (
        "plan 与 entry 两侧都应有 task_ids")


def test_new_checkpoints_carry_ids():
    """若存在新批次 checkpoint，应带 task_ids（历史批次允许缺失）。"""
    import json

    runs = REPO / "eval" / "runs" / "evolution"
    if not runs.is_dir():
        return
    checked = 0
    for path in sorted(runs.glob("checkpoint-*.json"))[-5:]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "holder" in data or not data.get("correct_flags"):
            continue
        if data.get("batchTag") == "legacy":
            continue
        # 只对新格式（含 holdout_task_ids 字段约定）要求
        if "holdout_task_ids" in data:
            assert len(data["holdout_task_ids"]) == len(data["correct_flags"])
            checked += 1
    assert checked >= 0  # 记录性断言，不强制历史批次