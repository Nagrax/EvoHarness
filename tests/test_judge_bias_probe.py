"""W16 回归：Judge 反偏差探针（位置/长度/注入/schema）+ kappa 来源标注。

对应验收：
- 位置交换、长度、schema坏输出、注入探针均留原始输入输出与版本
- Kappa退化/单类/小n返回不可用及区间，不能把proxy kappa命名为人类一致性
- 缺评/解析失败不能当pass
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "eval"))

import eval.judge_bias_probe as probe


def test_fair_judge_passes_all_probes():
    report = asyncio.run(probe.run_all(probe.fair_judge))
    assert report["position"]["verdict"] == "unbiased"
    assert report["length"]["verdict"] == "unbiased"
    assert report["injection"]["verdict"] == "robust"
    assert report["schema"]["verdict"] == "robust"


def test_probe_has_discrimination_power():
    """探针必须能区分公平与偏置 judge——否则探针本身无效。"""
    biased = asyncio.run(probe.run_all(probe.length_biased_judge))
    assert biased["length"]["verdict"] == "biased", (
        "长度偏置 judge 必须被 P2 捕获")
    fair = asyncio.run(probe.run_all(probe.fair_judge))
    assert fair["length"]["verdict"] == "unbiased"


def test_probe_keeps_raw_io_and_version():
    """每个探针必须保留原始输入/输出与版本（验收硬要求）。"""
    report = asyncio.run(probe.run_all(probe.fair_judge))
    for key in ("position", "length", "injection"):
        section = report[key]
        assert section.get("version") == probe.PROBE_VERSION
        assert section.get("raw"), f"{key} 探针必须保留 raw 输入输出"
        for row in section["raw"]:
            assert "input" in row and "output" in row
    assert report["schema"]["raw"]


def test_schema_malformed_output_never_passes():
    """缺评/解析失败绝不当 pass（验收硬要求）。"""
    result = probe.probe_schema_robustness()
    assert result["misjudgedAsPass"] == 0
    assert result["verdict"] == "robust"


def test_proxy_kappa_is_not_human_agreement():
    """proxy kappa 必须显式标注来源，不得命名为人类一致性。"""
    result = probe.kappa_with_provenance(
        [("pass", "pass"), ("fail", "fail")], "proxy:judge-vs-rules")
    assert result["isHumanAgreement"] is False
    assert result["source"].startswith("proxy")
    # 小 n → unusable
    assert result["usable"] is False


def test_proxy_kappa_degenerate_single_class_unusable():
    result = probe.kappa_with_provenance(
        [("pass", "pass"), ("pass", "pass"), ("pass", "pass")], "proxy:same-judge")
    assert result["usable"] is False, "单类退化必须返回 unusable"
    # 数值冻结为 0.0（calib.py 的确定性约定），不得返回 NaN
    assert result["kappa"] == 0.0


def test_human_kappa_with_enough_n_is_usable():
    pairs = [("a", "a"), ("b", "b"), ("a", "b"), ("b", "a")] * 10  # n=40，两类
    result = probe.kappa_with_provenance(pairs, "human:two-annotators")
    assert result["isHumanAgreement"] is True
    assert result["usable"] is True
