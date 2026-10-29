"""跨批次噪声门回归：逐指标判定可宣称性（不能整包宣称）。

真实数据支撑：soft 臂召回差异跨批次反号（+3.3/−6.7pp）→ 不可宣称；
污染维度两批都是 −2（极差 0）→ 可宣称。同一实验里指标结论可以不同。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.lifecycle_noise_gate import judge


def _batch(name: str, arms: dict, baseline: str = "off") -> dict:
    return {"name": name, "data": {"armsDetail": arms, "baseline": baseline}}


def test_direction_flip_is_not_claimable():
    batches = [
        _batch("b1", {"off": {"recallRate": 0.9, "contamination": 2, "tokens": 100},
                      "soft": {"recallRate": 0.95, "contamination": 0, "tokens": 102}}),
        _batch("b2", {"off": {"recallRate": 1.0, "contamination": 2, "tokens": 100},
                      "soft": {"recallRate": 0.9, "contamination": 0, "tokens": 98}}),
    ]
    result = judge(batches)
    recall = result["perMetric"]["soft:recallRate"]
    assert recall["directionConsistent"] is False
    assert recall["claimable"] is False
    assert recall["verdict"] == "not-claimable:direction-flips"


def test_zero_noise_consistent_delta_is_claimable():
    batches = [
        _batch("b1", {"off": {"recallRate": 0.96, "contamination": 2, "tokens": 100},
                      "soft": {"recallRate": 0.97, "contamination": 0, "tokens": 101}}),
        _batch("b2", {"off": {"recallRate": 0.99, "contamination": 2, "tokens": 100},
                      "soft": {"recallRate": 1.0, "contamination": 0, "tokens": 101}}),
    ]
    result = judge(batches)
    cont = result["perMetric"]["soft:contamination"]
    assert cont["perBatchDiff"] == [-2, -2]
    assert cont["noise"] == 0
    assert cont["claimable"] is True


def test_delta_within_noise_not_claimable():
    """差异方向一致但小于配置噪声 → 仍需拒绝宣称（FROZEN_PROTOCOL varianceGate）。"""
    batches = [
        _batch("b1", {"off": {"recallRate": 0.90, "contamination": 0, "tokens": 100},
                      "filter": {"recallRate": 0.92, "contamination": 0, "tokens": 90}}),
        _batch("b2", {"off": {"recallRate": 0.96, "contamination": 0, "tokens": 100},
                      "filter": {"recallRate": 0.98, "contamination": 0, "tokens": 90}}),
    ]
    result = judge(batches)
    recall = result["perMetric"]["filter:recallRate"]
    # 差异 +0.02 一致，但 off 自身极差 0.06 → 噪声大于差异
    assert recall["directionConsistent"] is True
    assert recall["exceedsNoise"] is False
    assert recall["claimable"] is False
    assert recall["verdict"] == "not-claimable:delta-within-config-noise"


def test_single_batch_refuses_judgement():
    result = judge([_batch("only", {"off": {"recallRate": 1.0, "contamination": 0, "tokens": 1}})])
    assert result["ok"] is False
    assert result["reason"] == "need-at-least-2-batches"


def test_baseline_itself_not_compared():
    batches = [
        _batch("b1", {"off": {"recallRate": 0.9, "contamination": 1, "tokens": 10},
                      "soft": {"recallRate": 0.9, "contamination": 0, "tokens": 10}}),
        _batch("b2", {"off": {"recallRate": 0.9, "contamination": 1, "tokens": 10},
                      "soft": {"recallRate": 0.9, "contamination": 0, "tokens": 10}}),
    ]
    result = judge(batches)
    assert not any(key.startswith("off:") for key in result["perMetric"]), (
        "基线自身不参与比较")


def test_real_batches_are_judgeable():
    """仓库里真实两批数据必须能被判读（防格式漂移）。"""
    paths = [REPO / "eval/runs/memory-lifecycle-ab-v3.json",
             REPO / "eval/runs/memory-lifecycle-ab-v3b.json"]
    if not all(p.is_file() for p in paths):
        pytest.skip("真实批次文件不存在")
    batches = [{"name": p.name, "data": json.loads(p.read_text(encoding="utf-8"))}
               for p in paths]
    result = judge(batches)
    assert result["ok"] is True
    assert result["claimableCount"] >= 1, "真实数据里至少污染维度应可宣称"
    verdicts = {key: item["verdict"] for key, item in result["perMetric"].items()}
    assert verdicts["soft:contamination"] == "claimable"
