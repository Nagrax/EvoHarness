"""Harness² 前提回归：可优化轴必须是**真实可配置**，不能是纸面参数。

背景（用户提出的核心质疑）：
现有闭环只迭代 skill；而 RSI 论文真正要回答的是"值不值得花 token 去优化 harness"。
要做这件事，harness 的配置面必须真的可改、可回滚——否则实验是在测空气。

本仓库原先有两个硬编码常量（`MAX_RESULT_CHARS=50000`、`AUTO_COMPACT_THRESHOLD=0.70`），
`harness_squared.py` 却把它们列为可配置轴。本文件固定"它们真的可配置"这一事实。

**实现约束（真踩过的坑）**：验证必须用子进程，不能用 importlib.reload。
reload 会重建模块里的类对象，使 `isinstance(x, ToolResult)` 变成 False，
从而跨测试污染 test_tool_result_status.py 的断言（曾导致 3 条测试假失败）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def read_const(module: str, attr: str, env: dict | None = None) -> object:
    """在子进程里读模块常量，完全隔离，不污染当前进程的模块状态。"""
    code = (
        "import json,sys\n"
        f"sys.path.insert(0,{str(REPO)!r})\n"
        f"import {module} as m\n"
        f"print(json.dumps({{'value': getattr(m, {attr!r})}}))\n"
    )
    full_env = {k: v for k, v in os.environ.items()
                if k not in {"EVOHARNESS_MAX_RESULT_CHARS", "EVOHARNESS_AUTO_COMPACT_THRESHOLD"}}
    full_env.update(env or {})
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=180,
                         env=full_env, cwd=str(REPO), check=False)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[-600:]
    line = out.stdout.decode("utf-8").strip().splitlines()[-1]
    return json.loads(line)["value"]


# ── tool_policy：截断上限 ──────────────────────────────


def test_max_result_chars_default():
    assert read_const("agents.tools", "MAX_RESULT_CHARS", {}) == 50000


@pytest.mark.parametrize("value,expected", [("20000", 20000), ("120000", 120000)])
def test_max_result_chars_env_override(value, expected):
    assert read_const("agents.tools", "MAX_RESULT_CHARS",
                      {"EVOHARNESS_MAX_RESULT_CHARS": value}) == expected


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "  ", "1e3"])
def test_max_result_chars_invalid_falls_back(bad):
    assert read_const("agents.tools", "MAX_RESULT_CHARS",
                      {"EVOHARNESS_MAX_RESULT_CHARS": bad}) == 50000, f"非法值 {bad!r} 必须回退默认"


# ── context_policy：折叠触发线 ─────────────────────────


def test_auto_compact_threshold_default():
    assert float(read_const("agents.agent", "AUTO_COMPACT_THRESHOLD", {})) == pytest.approx(0.70)


@pytest.mark.parametrize("value", ["0.85", "0.55"])
def test_auto_compact_threshold_env_override(value):
    observed = float(read_const("agents.agent", "AUTO_COMPACT_THRESHOLD",
                               {"EVOHARNESS_AUTO_COMPACT_THRESHOLD": value}))
    assert observed == pytest.approx(float(value))


@pytest.mark.parametrize("bad", ["abc", "0", "9.9", "-0.2", "nan"])
def test_auto_compact_threshold_invalid_falls_back(bad):
    observed = float(read_const("agents.agent", "AUTO_COMPACT_THRESHOLD",
                               {"EVOHARNESS_AUTO_COMPACT_THRESHOLD": bad}))
    assert observed == pytest.approx(0.70), (
        f"非法值 {bad!r} 必须回退默认，否则越界阈值会让折叠永不触发或每轮触发")


# ── 回滚一致性：去掉 env 必须回到原值 ──────────────────


def test_axes_revert_returns_to_default():
    overridden_tool = read_const("agents.tools", "MAX_RESULT_CHARS",
                                 {"EVOHARNESS_MAX_RESULT_CHARS": "20000"})
    overridden_ctx = float(read_const("agents.agent", "AUTO_COMPACT_THRESHOLD",
                                      {"EVOHARNESS_AUTO_COMPACT_THRESHOLD": "0.55"}))
    assert overridden_tool == 20000
    assert overridden_ctx == pytest.approx(0.55)

    assert read_const("agents.tools", "MAX_RESULT_CHARS", {}) == 50000
    assert float(read_const("agents.agent", "AUTO_COMPACT_THRESHOLD", {})) == pytest.approx(0.70)


# ── 轴定义与真实代码一致（防止文档漂移） ────────────────


def test_harness_axes_declare_real_envs():
    sys.path.insert(0, str(REPO / "eval"))
    import harness_squared

    axes = harness_squared.AXES
    assert {"context_policy", "tool_policy", "memory_policy"} <= set(axes)
    for spec in axes.values():
        assert spec["envs"], "每个轴必须声明真实 env"
        assert spec["variants"], "每个轴必须有可执行变体"
        assert any(v["name"] == "baseline" for v in spec["variants"]), "必须有 baseline 变体"
    # 声明的 env 必须真的被代码读取（上面各条已逐项验证 tool/context 两个轴）
    assert "EVOHARNESS_MAX_RESULT_CHARS" in axes["tool_policy"]["envs"]
    assert "EVOHARNESS_AUTO_COMPACT_THRESHOLD" in axes["context_policy"]["envs"]
