"""失败→工单稳定性闸门回归：只有稳定失败能变成待修工单。

这是闭环此前缺的一环——失败观测从不自动变成工单，稳定性闸门也就无处生效。
现在：benchmark 失败按稳定性分流，稳定失败建 pending 工单，其余自动闭合。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_loop_registers_benchmark_failure_source():
    source = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert '"benchmarkFailures"' in source
    assert "stable-failures" in source and "unstable-failures-filtered" in source
    assert "只有经稳定性检验" in source or "稳定失败" in source


def test_stability_index_built_from_artifact():
    source = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert "stability_index" in source
    assert 'stability_index.get(task_id)' in source


def test_informational_marked_findings_carry_to_backlog():
    """informational 必须带到 backlog 项上，否则 triage 无法自动闭合。"""
    source = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert '"informational": bool(finding.get("informational"))' in source
    assert 'existing["informational"]' in source


def test_triage_closes_informational():
    source = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert 'if item.get("informational") or' in source


def test_real_backlog_split_is_correct():
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    items = [i for i in data["items"] if i.get("source") == "benchmarkFailures"]
    if not items:
        return
    # 注意：不能用子串 "stable-failures" 判断——"unstable-failures-filtered"
    # 里也含这个子串（真实踩过），必须按后缀精确匹配。
    stable = [i for i in items if i["key"].endswith(":stable-failures")]
    filtered = [i for i in items if i["key"].endswith(":unstable-failures-filtered")]
    assert stable, "稳定失败应建工单"
    for item in stable:
        assert item["status"] in {"pending", "done"}
        assert not item.get("informational"), (
            f"稳定失败不得被标为 informational：{item['id']}")
    for item in filtered:
        assert item.get("informational") is True
        assert item["status"] == "done", (
            f"未通过稳定性检验的失败必须自动闭合，实际 {item['id']}={item['status']}")
