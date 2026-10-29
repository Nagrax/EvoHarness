"""ID 唯一性回归：工单编号绝不能复用。

真实事故（2026-10-29）：ID 生成用 `len(items)+1`，items 减少时会重号 →
backlog 里出现**两个 L0028**，导致"能力边界"处置被标到错误的 module 项上。
教训：编号复用会让所有依赖 ID 的操作（处置、报告、追溯）静默串到别的对象。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.loop import _next_item_number


def test_next_number_uses_max_not_count():
    """三个项、编号 1/2/5 → 下一个必须是 6，不是 4（len+1 会复用 4/5）。"""
    state = {"items": [{"id": "L0001"}, {"id": "L0002"}, {"id": "L0005"}]}
    assert _next_item_number(state) == 6


def test_next_number_ignores_non_standard_ids():
    state = {"items": [{"id": "L0003"}, {"id": "weird"}, {"id": ""}]}
    assert _next_item_number(state) == 4


def test_next_number_starts_at_one():
    assert _next_item_number({"items": []}) == 1


def test_real_backlog_ids_are_unique():
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [i["id"] for i in data["items"]]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"存在重复工单号：{duplicates}"


def test_limitation_only_on_capability_items():
    """known-limitation 只能标在能力边界类项上（silent-except 不是能力边界）。"""
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["items"]:
        if item.get("closedReason") != "known-limitation":
            continue
        assert not (item.get("key") or "").startswith("module:silent-except"), (
            f"{item['id']} 把 silent-except 标成了能力边界，属误标")
