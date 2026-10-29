"""arch_run repeats 口径回归：结论必须基于均值±极差，且受噪声门约束。

真实事故（eval/runs/arch/variance.json）：heavy-wrapped 基线同配置重复两次极差 50pp，
单次运行的"配置差异"全部落在噪声里。为防回退，本文件固定：
1. run_design 支持 repeats 并逐次独立 workspace/manifest；
2. main 报告 summaries（均值/极差）与 noiseGate；
3. 差异 <= 同配置极差时 verdict 必须带 :delta-within-config-noise；
4. n*repeats<30 时正向结论必须带 :but-not-claimable-n<30。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import eval.arch_run as arch_run


def test_run_design_accepts_repeats():
    signature = inspect.signature(arch_run.run_design)
    assert "repeats" in signature.parameters, "run_design 必须支持 repeats 参数"
    default = signature.parameters["repeats"].default
    assert default >= 1


def test_run_design_creates_independent_workspace_per_repeat():
    source = inspect.getsource(arch_run.run_design)
    assert "repeat_tag" in source, "每次 repeat 必须独立 workspace/manifest，否则互相污染"
    assert "rep{repeat + 1}" in source


def test_main_reports_mean_and_range():
    source = inspect.getsource(arch_run.main)
    assert "passMean" in source and "passRange" in source, "必须输出均值±极差"
    assert "noiseGate" in source, "必须输出噪声门"
    assert "delta-within-config-noise" in source, "差异落在噪声内必须打标"
    assert "but-not-claimable-n<30" in source, "样本不足必须打标"


def test_noise_gate_uses_max_config_range():
    source = inspect.getsource(arch_run.main)
    assert "max(" in source and "passRange" in source, "噪声门必须取各设计极差的最大值"
    assert "abs(d_pass) <= noise" in source, "比较必须用绝对差异 vs 噪声"


def test_noise_gate_includes_cross_batch_history():
    """噪声门必须含跨批次历史噪声：批内极差为 0 也会漏掉 [0,0,0,50] 型波动（实测踩坑）。"""
    source = inspect.getsource(arch_run.main)
    assert "variance.json" in source, "必须读跨批次方差历史"
    assert "cross_batch_noise" in source, "必须单列跨批次噪声"
    assert "max(in_batch_noise, cross_batch_noise)" in source, "有效噪声取两者最大"


def test_effective_n_multiplies_repeats():
    source = inspect.getsource(arch_run.main)
    assert "args.holdout * max(1, args.repeats)" in source, (
        "有效样本量必须是 holdout×repeats，否则 repeats 不影响判定门槛")
