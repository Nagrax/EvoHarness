#!/usr/bin/env python3
"""Harness² 实验：让 harness 优化 harness 自身（而非只优化 skill）。

问题定位（用户提出的核心质疑）：
  现在的闭环只迭代 skill。skill 进化的收益能否对冲它多消耗的 token？未知。
  而 RSI 论文真正要回答的是"是否值得花这些 token 去优化 harness"。

本模块把"优化对象"从 skill 扩展到 **harness 的可配置面**，并强制每次改动都带
成本-收益核算。三个可优化轴（都能在真实代码里点到）：

  H1 context_policy —— 折叠触发阈值 / 尾部保留轮数（agents/agent.py、session_memory.py）
  H2 tool_policy    —— 工具结果截断上限 / 并发安全集合（agents/tools.py）
  H3 memory_policy  —— 召回条数上限 / 头部读取字节 / 触发门槛（agents/memory.py）

每个"harness 变体"是一个 env 覆盖集合，跑同一批任务，比较：
  · Δcorrect（配对）
  · Δtokens（含 side_query）
  · ROI = Δcorrect / ΔtokensM

关键纪律：
1. 变体必须是**真实可配置**的（env 变量或代码常量），不能是纸面参数；
2. 每个变体必须能 revert，且 revert 后行为与基线逐字节一致；
3. n 小的时候 ROI 判 not-claimable（复用 roi.py 的判据）；
4. 只允许改"可回滚的行为开关"，不允许改评测口径、denominator、judge。

用法：
  python eval/harness_squared.py axes           # 列出可优化轴与当前值
  python eval/harness_squared.py diff <axis>    # 展示该轴的变体与影响面
  python eval/harness_squared.py plan --out X   # 生成可执行实验计划
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# ── 可优化轴定义 ───────────────────────────────────────────────
# 每条都必须能指向真实代码位置与环境变量；env=None 表示目前只有代码常量。
AXES: dict[str, dict] = {
    "context_policy": {
        "title": "上下文策略（折叠触发 / 尾部保留）",
        "why": "长任务里折叠既省 token 又有损；阈值与保留轮数直接决定成本-质量权衡",
        "envs": ["EVOHARNESS_AUTO_COMPACT_THRESHOLD"],
        "code": ["agents/agent.py:_check_and_compact", "agents/session_memory.py"],
        "variants": [
            {"name": "baseline", "env": {}, "desc": "当前默认"},
            {"name": "late-fold", "env": {"EVOHARNESS_AUTO_COMPACT_THRESHOLD": "0.85"},
             "desc": "更晚折叠：保留更多原文，token 更高、状态更全"},
            {"name": "early-fold", "env": {"EVOHARNESS_AUTO_COMPACT_THRESHOLD": "0.55"},
             "desc": "更早折叠：token 更低，但可能丢状态"},
        ],
        "metric": "Δcorrect vs Δtokens",
    },
    "tool_policy": {
        "title": "工具结果策略（截断上限）",
        "why": "工具输出是长任务 token 主消耗；截断上限决定成本与信息完整度",
        "envs": ["EVOHARNESS_MAX_RESULT_CHARS"],
        "code": ["agents/tools.py:MAX_RESULT_CHARS", "agents/tools.py:_truncate_result"],
        "variants": [
            {"name": "baseline", "env": {}, "desc": "当前 50000 字符"},
            {"name": "tight", "env": {"EVOHARNESS_MAX_RESULT_CHARS": "20000"},
             "desc": "更激进截断：省 token，可能丢失关键输出"},
            {"name": "loose", "env": {"EVOHARNESS_MAX_RESULT_CHARS": "120000"},
             "desc": "更宽松：信息全，token 高"},
        ],
        "metric": "Δcorrect vs Δtokens",
    },
    "memory_policy": {
        "title": "记忆召回策略（条数 / 头部字节 / 触发门槛）",
        "why": "本项目真实踩过召回静默丢；条数与读取量直接决定收益与成本",
        "envs": ["EVOHARNESS_MAX_MEMORY_RECALL", "EVOHARNESS_MEMORY_HEADER_BYTES"],
        "code": ["agents/memory.py:MAX_MEMORY_RECALL", "agents/memory.py:HEADER_READ_BYTES",
                 "agents/memory.py:should_prefetch_memory"],
        "variants": [
            {"name": "baseline", "env": {}, "desc": "上限 5 条、头部 64KB"},
            {"name": "recall15", "env": {"EVOHARNESS_MAX_MEMORY_RECALL": "15"},
             "desc": "召回 15 条：召回更全、上下文更贵"},
            {"name": "recall3", "env": {"EVOHARNESS_MAX_MEMORY_RECALL": "3"},
             "desc": "召回 3 条：更省，可能丢召回"},
        ],
        "metric": "Δcorrect vs Δtokens",
    },
}


def read_current_values(axis: str) -> dict:
    """读当前生效值：优先 env，其次导入模块取常量。"""
    values = {}
    for name in AXES[axis]["envs"]:
        values[name] = os.environ.get(name, "(unset)")
    # 尝试导入真实常量，避免"纸面参数"
    try:
        sys.path.insert(0, str(REPO))
        if axis == "memory_policy":
            import agents.memory as memory

            values["MAX_MEMORY_RECALL(effective)"] = memory.MAX_MEMORY_RECALL
            values["HEADER_READ_BYTES(effective)"] = memory.HEADER_READ_BYTES
        if axis == "tool_policy":
            import agents.tools as tools

            values["MAX_RESULT_CHARS(effective)"] = tools.MAX_RESULT_CHARS
    except Exception as error:  # noqa: BLE001
        values["import-error"] = f"{type(error).__name__}: {error}"
    return values


def cmd_axes(as_json: bool) -> int:
    out = {}
    for axis, spec in AXES.items():
        out[axis] = {**spec, "current": read_current_values(axis)}
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    for axis, spec in out.items():
        print(f"\n[{axis}] {spec['title']}")
        print(f"  为什么值得优化：{spec['why']}")
        print(f"  可配置 env：{spec['envs']}")
        print(f"  真实代码位置：{spec['code']}")
        print(f"  当前值：{json.dumps(spec['current'], ensure_ascii=False)}")
        for variant in spec["variants"]:
            print(f"    - {variant['name']:10s} env={variant['env']}  {variant['desc']}")
    return 0


def cmd_plan(out_path: str) -> int:
    """生成可执行实验计划：每轴每变体 = 一次独立 run，记录 ROI 所需字段。"""
    plan = {
        "kind": "harness-squared-plan",
        "question": "优化 harness 本身带来的收益，能否对冲它多消耗的 token？",
        "axes": {},
        "protocol": [
            "同一批任务（同 split manifest）、同模型、同预算下跑基线与变体",
            "每变体独立 run，记录逐题 correct 与 tokens",
            "用 eval/roi.py pair 做配对 Δcorrect + bootstrap CI",
            "ROI = Δcorrect / ΔtokensM；n<30 或 CI 跨 0 判 not-claimable",
            "变体必须可 revert，revert 后与基线逐字节一致",
        ],
        "forbidden": [
            "改评测口径、denominator、judge 来制造提升",
            "把 side_query 开销摊进主线后宣称'进化免费'",
            "在小样本上宣称 profitable",
        ],
    }
    for axis, spec in AXES.items():
        plan["axes"][axis] = {
            "variants": spec["variants"],
            "runs": [
                {
                    "runId": f"{axis}-{variant['name']}",
                    "env": variant["env"],
                    "expectedCost": "1 batch x N tasks",
                }
                for variant in spec["variants"]
            ],
        }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(out),
                      "axes": len(plan["axes"]),
                      "totalRuns": sum(len(v["runs"]) for v in plan["axes"].values())},
                     ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Harness² 实验：优化 harness 自身")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("axes"); p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda a: cmd_axes(a.json))
    p = sub.add_parser("plan"); p.add_argument("--out", default="eval/runs/harness2/plan.json")
    p.set_defaults(func=lambda a: cmd_plan(a.out))
    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
