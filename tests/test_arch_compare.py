"""架构对比（试验场）回归：设计维度必须映射到真实开关。

用户诉求是把 EvoHarness 当试验场，比较"重封装 / 轻量 / 插件式"三种 harness 设计。
要比较，差异就必须是**真实可配置的行为**；否则只是纸面分类。

本文件固定两件事：
1. 三种设计声明的 env 在代码里**确有读取点**；
2. 记忆预取开关真的关掉了预取（不是声明了却不生效）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "eval"))

import eval.arch_compare as arch


def test_all_declared_envs_are_read_by_code():
    support = arch.probe_env_support()
    missing = [env for env, ok in support.items() if not ok]
    assert not missing, f"以下 env 在代码里没有读取点（纸面参数）：{missing}"


def test_three_designs_have_distinct_profiles():
    keys = set(arch.DESIGNS)
    # 三种"设计风格" + 三个"单变量拆解"变体（用于归因，不打包开关）
    required = {"heavy-wrapped", "lightweight", "plugin",
                "only-prefetch-off", "only-early-fold", "only-tight-tools"}
    assert required <= keys, f"缺少设计维度：{required - keys}"
    profiles = {k: tuple(sorted(v["env"].items())) for k, v in arch.DESIGNS.items()}
    assert len({p for p in profiles.values()}) == len(profiles), "每个设计必须在开关组合上真的不同"


def test_single_variable_variants_change_exactly_one_env():
    """单变量拆解必须只改一个开关，否则无法归因。"""
    for key in ("only-prefetch-off", "only-early-fold", "only-tight-tools"):
        spec = arch.DESIGNS[key]
        assert len(spec["env"]) == 1, f"{key} 必须只含一个开关，实际={spec['env']}"


def test_plan_reports_no_blockers():
    plan_path = REPO / "eval" / "runs" / "arch" / "plan.json"
    arch.cmd_plan(str(plan_path))
    import json

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["blocked"] is None, f"仍有未实现开关：{plan['blocked']}"
    assert all(plan["envsVerified"].values())


# ── 预取开关必须真的生效 ──────────────────────────────


def test_memory_prefetch_switch_defaults_on(monkeypatch):
    monkeypatch.delenv("EVOHARNESS_DISABLE_MEMORY_PREFETCH", raising=False)
    import subprocess

    code = (
        "import json,sys\n"
        f"sys.path.insert(0,{str(REPO)!r})\n"
        "import agents.agent as a\n"
        "print(json.dumps({'disabled': a._memory_prefetch_disabled()}))\n"
    )
    import os

    env = {k: v for k, v in os.environ.items() if k != "EVOHARNESS_DISABLE_MEMORY_PREFETCH"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=120,
                         env=env, cwd=str(REPO), check=False)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[-400:]
    import json

    assert json.loads(out.stdout.decode("utf-8").strip().splitlines()[-1])["disabled"] is False


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("0", False),
                                            ("false", False), ("", False)])
def test_memory_prefetch_switch_parsing(value, expected):
    import json
    import os
    import subprocess

    code = (
        "import json,sys\n"
        f"sys.path.insert(0,{str(REPO)!r})\n"
        "import agents.agent as a\n"
        "print(json.dumps({'disabled': a._memory_prefetch_disabled()}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "EVOHARNESS_DISABLE_MEMORY_PREFETCH"}
    env["EVOHARNESS_DISABLE_MEMORY_PREFETCH"] = value
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=120,
                         env=env, cwd=str(REPO), check=False)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[-400:]
    observed = json.loads(out.stdout.decode("utf-8").strip().splitlines()[-1])["disabled"]
    assert observed is expected


def test_prefetch_switch_is_wired_into_both_protocol_paths():
    """开关必须同时接进 Anthropic 与 OpenAI 两条路径，否则只关一半。"""
    source = (REPO / "agents" / "agent.py").read_text(encoding="utf-8")
    assert source.count("_memory_prefetch_disabled()") >= 2, (
        "两个协议路径都应检查预取开关；只接一处会导致对照实验半边生效")
