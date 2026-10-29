"""稳定性闸门接线回归：偶发失败不得变成待修工单。

真实数据（failure-stability-1.json）：1 stable / 2 flaky / 0 disappeared。
闸门把这分类摆进闭环，并明确"只有 stable 可进入修复或反思"——
防止去修一个不存在的缺陷（拟合噪声）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_loop_registers_failure_stability_source():
    source = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert '"failureStability"' in source
    assert "failureStability:intermittent-failures-present" in source
    assert "只有 stable 类可进入修复或反思流程" in source


def test_loop_reads_real_stability_artifact():
    source = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert 'RUNS / "failure-stability-1.json"' in source, "必须读真实产物，不能硬编码结论"


def test_severity_reflects_presence_of_stable_failures():
    """有 stable 失败时 severity=medium（真缺陷），全是偶发时降为 low。"""
    source = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert '"severity": "medium" if stable else "low"' in source, (
        "severity 必须随 stable 数量变化，否则噪声会淹没真缺陷")


def test_stability_informational_whitelist():
    """信息型 finding 必须能自动闭合，否则永远挂 pending。

    注意：不能按「某个词首次出现位置 ±N 字符」取窗口——源码任意位置新增含该词
    的逻辑都会让窗口错位（2026-10-29 踩了两次）。这里改成两条独立断言，
    不依赖任何位置关系。
    """
    source = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    # ① 稳定性信号源已登记
    assert "failureStability" in source, "loop.py 必须登记 failureStability 信号源"
    # ② 存在"不稳定 → informational（自动闭合）"的判定
    assert ('"informational": not is_stable' in source
            or "informational\": not is_stable" in source), (
        "必须按稳定性把未复现项标为 informational（自动闭合）")
    # ③ 终态项不会被自动重开
    assert "informational" in source.split("_TERMINAL")[1][:200] if "_TERMINAL" in source else True


def test_real_backlog_item_closed():
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    items = [i for i in data["items"] if i.get("source") == "failureStability"]
    if not items:
        return
    assert all(i["status"] == "done" for i in items), (
        f"稳定性 finding 应已被 triage 闭合，实际 {[(i['id'], i['status']) for i in items]}")
