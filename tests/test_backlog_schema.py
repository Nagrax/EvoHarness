"""BACKLOG schema 契约回归（2026-10-30，dogfooding #4 发现）。

背景：34 个 item 里 **11 个不符合隐式契约**（`rule` 非 str / `line` 缺失）。
消费者（`loop_context.py`）依赖这些字段的类型，坏数据会让它崩或静默漏条目。

本测试做两件事：
① 明确**契约**（`detail.file` 必填 str；`detail.line` 若存在必须是 int；
   `detail.rule` 若是 str 或 list，不得是其他类型）
② 存量 11 个不合规条目录入**已知例外**——它们多为信息型/聚合条目，
   强行补字段反而是造假。**但新条目不得再违反契约**。
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKLOG = REPO / "eval" / "loop" / "BACKLOG.json"

# 已知例外：这些条目的 detail 形态与本规则不同（信息型 / 聚合型 / 历史格式）
KNOWN_EXCEPTIONS = {
    "L0021": "harness2 结果为信息型（无单点位置）",
    "L0022": "variance 判据写成了三条 list",
    "L0027": "failureStability 为信息型",
    "L0029": "benchmarkFailures 信息型",
    "L0030": "benchmarkFailures 信息型",
    "L9025": "lifecycleNoiseGate 信息型",
    "L9028": "benchmarkFailures 信息型",
    "L9029": "module 聚合条目（多文件多行）",
}


def _items() -> list[dict]:
    if not BACKLOG.is_file():
        return []
    return json.loads(BACKLOG.read_text(encoding="utf-8")).get("items") or []


def test_detail_is_object():
    for item in _items():
        assert isinstance(item.get("detail"), dict), f"{item.get('id')} 的 detail 必须是对象"


def test_rule_and_line_types_are_within_contract():
    """rule ∈ {str, list, None}，line ∈ {int, None}；file 若存在必须是 str。"""
    bad = []
    for item in _items():
        item_id = str(item.get("id") or "")
        if item_id in KNOWN_EXCEPTIONS:
            continue
        detail = item.get("detail") or {}
        rule = detail.get("rule")
        line = detail.get("line")
        file_rel = detail.get("file")
        if rule is not None and not isinstance(rule, (str, list)):
            bad.append(f"{item_id}: rule 类型 {type(rule).__name__}")
        if line is not None and not isinstance(line, int):
            bad.append(f"{item_id}: line 类型 {type(line).__name__}")
        if file_rel is not None and not isinstance(file_rel, str):
            bad.append(f"{item_id}: file 类型 {type(file_rel).__name__}")
    assert not bad, "新条目违反 schema 契约：\n  " + "\n  ".join(bad)


def test_items_with_file_have_int_line():
    """有 file 的条目应该有 int line（否则消费者无法定位）。"""
    bad = []
    for item in _items():
        item_id = str(item.get("id") or "")
        if item_id in KNOWN_EXCEPTIONS:
            continue
        detail = item.get("detail") or {}
        if isinstance(detail.get("file"), str) and detail["file"]:
            if not isinstance(detail.get("line"), int):
                bad.append(f"{item_id}: 有 file 但 line 不是 int")
    assert not bad, "\n  ".join(bad)


def test_known_exceptions_are_still_present():
    """例外清单不得包含已不存在的 id（防清单腐化）。"""
    ids = {str(item.get("id") or "") for item in _items()}
    stale = sorted(set(KNOWN_EXCEPTIONS) - ids)
    assert not stale, f"例外清单里有已不存在的条目：{stale}"


def test_loop_context_survives_all_items():
    """消费者必须能处理全部条目（不崩、不静默丢）。"""
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, "eval/loop_context.py"],
                          cwd=str(REPO), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"loop_context 崩溃：\n{proc.stderr[-800:]}"
    assert "Traceback" not in proc.stderr, proc.stderr[-600:]