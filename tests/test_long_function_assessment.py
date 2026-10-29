"""long-function 评估结论回归（2026-10-29，含新增判据）。

评估标准 v2：
  该拆 ⇔ 有可独立命名的子步骤 ∧ 有复用意愿 ∧ 分支密度高 ∧ **有测试保护**
缺最后一条时应先补可测性（抽出纯逻辑），而不是直接拆执行流程。

`_cmd_run` 就是这条新判据的第一个适用对象：它的循环体要用真实模型才能验证，
而余额中止 / holdout 只读校验 / 批次隔离这些评测正确性核心都在里面 ——
硬拆等于在没有安全网时动核心。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

ASSESSMENT = REPO / "eval" / "runs" / "long-function-assessment.json"


def _data() -> dict:
    return json.loads(ASSESSMENT.read_text(encoding="utf-8"))


def test_assessment_file_exists_and_covers_18():
    data = _data()
    assert data["total"] == 18, f"应覆盖全部 18 处，实际 {data['total']}"


def test_categories_partition_all_entries():
    data = _data()
    assessment = data["assessment"]
    counts = {k: len(v) for k, v in assessment.items() if isinstance(v, list)}
    assert sum(counts.values()) == 18, f"分类必须覆盖 18 处：{counts}"


def test_standard_includes_test_protection():
    """新判据必须写进产物（否则下次又会在无安全网时硬拆）。"""
    data = _data()
    assert "有测试保护" in data["assessment"]["standard_v2"]
    assert "先补可测性" in data["assessment"]["standard_v2"]


def test_cmd_run_is_deferred_not_should_split():
    data = _data()
    deferred = data["assessment"].get("deferred") or []
    names = [d["func"] for d in deferred]
    assert any("_cmd_run" in n for n in names), f"_cmd_run 应在 deferred 里：{names}"
    should_split = [d["func"] for d in data["assessment"]["should_split"]]
    assert not any("_cmd_run" in n for n in should_split), "_cmd_run 不应还在该拆清单"


def test_deferred_entry_documents_reason():
    data = _data()
    for entry in data["assessment"].get("deferred") or []:
        assert entry.get("reassessed"), f"{entry['func']} 必须写明改判理由"
        assert "无测试保护" in entry["reassessed"] or "无安全网" in entry["reassessed"]


def test_should_not_split_entries_carry_reason():
    """每条"不该拆"必须写明理由（why 或 reassessed 均可）。"""
    data = _data()
    for entry in data["assessment"]["should_not_split"]:
        # 取较长的那个：改判项的 why 可能还是旧短文本，reassessed 才是真理由
        reason = max(entry.get("why") or "", entry.get("reassessed") or "", key=len)
        assert reason, f"{entry['func']} 必须写明为什么不该拆"
        assert len(reason) > 20, f"{entry['func']} 理由不能空泛：{reason!r}"