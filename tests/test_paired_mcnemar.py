"""McNemar 配对检验回归（2026-10-29）。

设计意图：**缺 task_ids 时拒绝配对**，而不是拿"顺序相同"当同题。
历史 checkpoint 没有 ids → 工具必须返回 `not-pairable`，不能编出 p 值。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.paired_mcnemar as mc


def _row(round_no: int, flags: list[int], ids: list[str] | None = None) -> dict:
    return {"file": f"r{round_no}", "round": round_no, "flags": flags,
            "ids": ids if ids is not None else []}


def test_refuses_without_task_ids():
    arm = [_row(1, [1, 0, 0])]
    base = [_row(1, [0, 0, 0])]
    table, meta = mc.pair(arm, base)
    assert meta["idAvailable"] is False
    assert table == {}
    assert "task_ids" in meta["reason"] or "holdout" in meta["reason"]


def test_pairs_by_task_id_not_position():
    """顺序不同但 task_id 相同 → 必须按 id 配对（这正是"顺序不等于同题"的防护）。"""
    # 两臂 flags 相同、ids 顺序相反：
    # 按 id  → t1(arm 1, base 0)=armOnly；t2(arm 0, base 1)=baseOnly
    # 按位置 → t1(1,1)=bothCorrect；t2(0,0)=bothWrong（这是错误做法会得到的）
    arm = [_row(1, [1, 0], ids=["t1", "t2"])]
    base = [_row(1, [1, 0], ids=["t2", "t1"])]
    table, meta = mc.pair(arm, base)
    assert meta["idAvailable"] is True
    assert meta["sharedTasks"] == 2
    assert table["armOnly"] == 1, f"按 id 配对应得 armOnly=1，实际 {table}"
    assert table["baseOnly"] == 1, f"按 id 配对应得 baseOnly=1，实际 {table}"
    assert table["bothCorrect"] == 0 and table["bothWrong"] == 0, (
        "若出现 bothCorrect/bothWrong，说明是按键位而非 task_id 配对")


def test_pairs_across_rounds_independently():
    arm = [_row(1, [1], ids=["t1"]), _row(2, [0], ids=["t1"])]
    base = [_row(1, [0], ids=["t1"]), _row(2, [0], ids=["t1"])]
    table, meta = mc.pair(arm, base)
    assert meta["sharedTasks"] == 2, "round 作为配对键的一部分（同题不同轮的观测算两次）"
    assert table["armOnly"] == 1
    assert table["bothWrong"] == 1


def test_mcnemar_exact_known_values():
    # b=4, c=0 → p = 2 * (1/16) = 0.125
    assert abs(mc.mcnemar_exact(4, 0) - 0.125) < 1e-9
    # b=5, c=0 → 2 * (1/32) = 0.0625
    assert abs(mc.mcnemar_exact(5, 0) - 0.0625) < 1e-9
    # b=c → p = 1.0
    assert mc.mcnemar_exact(3, 3) == pytest.approx(1.0)
    # n=0 → p = 1.0（没有不一致的观测）
    assert mc.mcnemar_exact(0, 0) == 1.0


def test_mcnemar_is_symmetric():
    for b, c in ((2, 5), (7, 1), (0, 9)):
        assert mc.mcnemar_exact(b, c) == pytest.approx(mc.mcnemar_exact(c, b))


def test_mcnemar_monotonic_in_imbalance():
    """不平衡越大，p 越小（方向一致性检查）。"""
    assert mc.mcnemar_exact(6, 0) < mc.mcnemar_exact(3, 0)
    assert mc.mcnemar_exact(10, 0) < mc.mcnemar_exact(6, 0)


def test_real_report_is_fail_closed():
    """真实产物：历史数据缺 ids，报告必须是 not-pairable。"""
    import json

    path = REPO / "eval" / "runs" / "paired-mcnemar.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("idAvailable"):
        assert data["ok"] is False
        assert data["verdict"] == "not-pairable:missing-task-ids"
        assert "mcnemarP" not in data, "不得在无法配对时给出 p 值"