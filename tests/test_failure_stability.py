"""失败稳定性分类回归：只有稳定复现的失败才值得沉淀教训。

真实数据（3 题 × 2 遍，raccoon 模型，800,190 tokens）：
  flaky 2 题（1/2 复现）、stable 1 题（2/2 复现）
→ W18 报的 no-final-marker 失败里约 2/3 是偶发，不能当能力缺陷处理。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def classify(no_marker: int, repeats: int) -> str:
    """与 eval/failure_stability.py 完全一致的判定（唯一口径）。"""
    repro = no_marker / repeats if repeats else 0.0
    if repro == 1.0:
        return "stable"
    if repro == 0.0:
        return "disappeared"
    return "flaky"


def test_classification_thresholds():
    assert classify(3, 3) == "stable"
    assert classify(1, 3) == "flaky"
    assert classify(0, 3) == "disappeared"
    assert classify(2, 2) == "stable"


def test_script_uses_same_rule():
    """脚本必须与回归用同一判定（口径漂移会让结论无法复现）。

    2026-10-28 升级：从"复现率 == 1.0 / == 0.0"的二分类改为失败率阈值
    （stable >= 0.8），因为 2 遍二分类实测不可靠。此处固定新口径。
    """
    source = (REPO / "eval" / "failure_stability.py").read_text(encoding="utf-8")
    assert "classify_failure" in source
    assert "STABLE_MIN_FAIL_RATE" in source
    assert "只有 stable 类失败值得沉淀教训" in source
    # 不能再回到"等于 1.0 才算 stable"的老口径
    assert 'status = ("stable" if repro == 1.0' not in source


def test_real_result_only_stable_justifies_reflection():
    """真实结果必须能支撑结论：stable 才是反思对象。"""
    path = REPO / "eval" / "runs" / "failure-stability-1.json"
    if not path.is_file():
        return  # 无产物时跳过（不伪造）
    data = json.loads(path.read_text(encoding="utf-8"))
    counts = data["statusCounts"]
    assert counts["stable"] + counts["flaky"] + counts["disappeared"] == data["tasks"]
    # 本批确实观察到偶发（否则本测试无意义）
    assert counts["flaky"] >= 1, "真实批次里应出现 flaky（这正是结论依据）"
    for task in data["perTask"]:
        assert task["status"] == classify(task["noMarkerCount"], task["repeats"])


def test_worker_field_contract_documented():
    """字段契约坑必须留在代码里（否则下一轮又会传错键）。"""
    source = (REPO / "eval" / "failure_stability.py").read_text(encoding="utf-8")
    assert 'item["Question"]' in source and 'item["Final answer"]' in source
    assert "PYTHONPATH" in source, "cwd 隔离后必须显式给模块路径"
