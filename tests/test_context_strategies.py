"""W15 回归：上下文四策略对照的开关接线。

四策略（E-05）映射到真实 env/机制：
  S1 无折叠        → EVOHARNESS_DISABLE_AUTO_FOLD=1（本轮新增：只挡自动路径）
  S2 当前折叠      → 默认（整段折叠，keep=0）
  S3 尾部保留 N 轮  → EVOHARNESS_FOLD_KEEP_RECENT_TURNS=N
  S4 阶段交接      → _loop_stages_active（plan 文件激活阶段机）

验收：四策略均实跑且真实接线；同题同模型同预算。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _read_const(module: str, expr: str, env: dict | None = None) -> object:
    code = (
        "import json,sys\n"
        f"sys.path.insert(0,{str(REPO)!r})\n"
        f"print(json.dumps({{'value': {expr}}}))\n"
    )
    full_env = {k: v for k, v in os.environ.items()
                if k not in {"EVOHARNESS_DISABLE_AUTO_FOLD",
                             "EVOHARNESS_FOLD_KEEP_RECENT_TURNS"}}
    full_env.update(env or {})
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=180,
                         env=full_env, cwd=str(REPO), check=False)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[-500:]
    return json.loads(out.stdout.decode("utf-8").strip().splitlines()[-1])["value"]


def test_s1_disable_auto_fold_switch_exists_and_read():
    assert (REPO / "agents" / "agent.py").read_text(encoding="utf-8").count(
        "EVOHARNESS_DISABLE_AUTO_FOLD") >= 1


def test_s1_only_blocks_auto_path_not_manual_tool():
    """S1 只挡自动压缩；模型/用户主动调用 compact_context 仍可用。"""
    source = (REPO / "agents" / "agent.py").read_text(encoding="utf-8")
    # 开关在 _check_and_compact（自动路径），不在 _compact_conversation（手动入口）
    check_fn = source.split("async def _check_and_compact")[1].split("async def _compact_conversation")[0]
    assert "EVOHARNESS_DISABLE_AUTO_FOLD" in check_fn
    compact_fn = source.split("async def _compact_conversation")[1].split("async def _compact_anthropic")[0]
    assert "EVOHARNESS_DISABLE_AUTO_FOLD" not in compact_fn


def test_s3_keep_recent_turns_env_read_each_call():
    """尾部保留逐次读 env（每次调用都取，便于对照实验中按轮切换）。"""
    assert _read_const("agents.agent", "__import__('agents.agent',fromlist=['x'])._fold_keep_recent_turns()",
                       {"EVOHARNESS_FOLD_KEEP_RECENT_TURNS": "0"}) == 0
    assert _read_const("agents.agent", "__import__('agents.agent',fromlist=['x'])._fold_keep_recent_turns()",
                       {"EVOHARNESS_FOLD_KEEP_RECENT_TURNS": "4"}) == 4
    # 非法值回退 0
    assert _read_const("agents.agent", "__import__('agents.agent',fromlist=['x'])._fold_keep_recent_turns()",
                       {"EVOHARNESS_FOLD_KEEP_RECENT_TURNS": "abc"}) == 0


def test_four_strategies_have_distinct_config_profiles():
    sys.path.insert(0, str(REPO / "eval"))
    from eval.context_strategies import STRATEGIES

    assert set(STRATEGIES) == {"s1_no_fold", "s2_current_fold",
                               "s3_tail_keep", "s4_stage_handover"}
    profiles = {k: json.dumps(v["env"], sort_keys=True) for k, v in STRATEGIES.items()}
    # S1/S2/S3 可纯 env 区分；S4 需要阶段机（plan 文件），由 runner 单独接线
    assert len({profiles[k] for k in ("s1_no_fold", "s2_current_fold", "s3_tail_keep")}) == 3
