#!/usr/bin/env python3
"""Harness 架构对比评测：把"重封装 / 轻量 / 插件式"变成可测的配置维度。

用户的核心诉求：EvoHarness 应作为**试验场**，在真实任务上比较不同 harness 设计，
并回答"收益能否覆盖 token 成本"。

本模块不去争论哪种设计更好，而是把三种设计里**可配置的行为差异**抽出来，
纳入同一套成本-收益核算（复用 eval/roi.py 判据）。

三种设计在"可配置行为"上的真实差异（都能在代码里点到）：
  重封装（Claude Code 式）：默认多层预取（记忆召回 + skill 检索 + 意图提示），
                            单轮上下文更全，token 更高。
  轻量（pi-agent 式）：默认关闭自动预取与工具提示，只按需最小上下文，token 更低。
  插件式（DSH 式）：按能力开关：预取/提示/tool hints 各自独立，可单独开。

用法：
  python eval/arch_compare.py axes       # 看三种设计映射到哪些真实开关
  python eval/arch_compare.py plan       # 生成对比计划
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 每种"设计风格" = 一组**真实存在于请求路径**的 env 开关。
# 教训：初版把 EVOHARNESS_DISABLE_TOOL_HINTS 当成设计差异，但查代码发现
# `get_active_tool_definitions(self.tools)` 默认 `with_hints=False`，
# 工具安全注解**根本没有注入模型请求**。把它算作"重封装 vs 轻量"的差异是错的，
# 已从设计维度里移除，并改用一个确实影响每轮请求的开关组合。
DESIGNS: dict[str, dict] = {
    "heavy-wrapped": {
        "title": "重封装（默认多层预取：记忆召回 + 早期折叠）",
        "env": {
            "EVOHARNESS_DISABLE_MEMORY_PREFETCH": "0",
            "EVOHARNESS_AUTO_COMPACT_THRESHOLD": "0.70",
        },
        "rationale": "每轮多一次旁路召回，上下文更全但更贵；折叠更晚、保留更多原文",
        "codeRefs": ["agents/agent.py:_chat_openai/_chat_anthropic 预取调用点",
                     "agents/agent.py:AUTO_COMPACT_THRESHOLD"],
    },
    "lightweight": {
        "title": "轻量（按需最小上下文：关预取 + 更早折叠）",
        "env": {
            "EVOHARNESS_DISABLE_MEMORY_PREFETCH": "1",
            "EVOHARNESS_AUTO_COMPACT_THRESHOLD": "0.55",
        },
        "rationale": "省掉旁路调用、更早折叠；token 更低，但可能丢召回与状态",
        "codeRefs": ["同上两个开关"],
    },
    "plugin": {
        "title": "插件式（按能力取舍：保留召回，收紧工具结果）",
        "env": {
            "EVOHARNESS_DISABLE_MEMORY_PREFETCH": "0",
            "EVOHARNESS_MAX_RESULT_CHARS": "20000",
        },
        "rationale": "保留收益侧（记忆召回），只压成本侧（工具输出）；验证'只砍贵的、不砍有用的'是否更优",
        "codeRefs": ["agents/agent.py 预取开关", "agents/tools.py:MAX_RESULT_CHARS"],
    },
    # ── 单变量拆解：打包开关的组合结果无法归因，必须逐个测 ──
    "only-prefetch-off": {
        "title": "单变量：只关记忆预取",
        "env": {"EVOHARNESS_DISABLE_MEMORY_PREFETCH": "1"},
        "rationale": "定位'省 token'到底来自关预取还是更早折叠",
        "codeRefs": ["agents/agent.py:_memory_prefetch_disabled"],
    },
    "only-early-fold": {
        "title": "单变量：只把折叠线改到 0.55",
        "env": {"EVOHARNESS_AUTO_COMPACT_THRESHOLD": "0.55"},
        "rationale": "定位折叠时机对质量与成本的影响",
        "codeRefs": ["agents/agent.py:AUTO_COMPACT_THRESHOLD"],
    },
    "only-tight-tools": {
        "title": "单变量：只把工具截断改到 20k",
        "env": {"EVOHARNESS_MAX_RESULT_CHARS": "20000"},
        "rationale": "定位工具输出截断的独立影响（此前与折叠/预取打包，无法归因）",
        "codeRefs": ["agents/tools.py:MAX_RESULT_CHARS"],
    },
}


def probe_env_support() -> dict:
    """检查这些开关是否真的被代码读取（防止又出现"纸面参数"）。

    扫描范围要覆盖**真正读取 env 的模块**：agent.py（预取/折叠阈值）、
    tools.py（截断上限）、memory.py、prompt.py。初版漏了 tools.py，
    导致 EVOHARNESS_MAX_RESULT_CHARS 被误判为纸面参数。
    """
    sources = []
    for name in ("agents/agent.py", "agents/prompt.py", "agents/memory.py",
                 "agents/tool_hints.py", "agents/tools.py", "agents/session_memory.py"):
        path = REPO / name
        if path.is_file():
            sources.append(path.read_text(encoding="utf-8", errors="replace"))
    blob = "\n".join(sources)
    out = {}
    for design in DESIGNS.values():
        for env_name in design["env"]:
            out[env_name] = env_name in blob
    return out


def cmd_axes(as_json: bool) -> int:
    support = probe_env_support()
    rows = []
    for key, design in DESIGNS.items():
        rows.append({**design, "key": key,
                     "envsActuallyRead": {env: support.get(env, False) for env in design["env"]}})
    payload = {
        "designs": rows,
        "allEnvsRead": all(support.values()),
        "missingEnvsReadPoints": [env for env, ok in support.items() if not ok],
        "rule": [
            "只有 env 真的被代码读取，才允许把它当作设计差异来评测（否则是纸面参数）",
            "三种设计的差异必须在同一批题、同一判据下比较，且用 eval/roi.py 的 verdict 判定",
            "n<30 一律 not-claimable；不得用省 token 冒充收益",
        ],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"\n[{row['key']}] {row['title']}")
            print(f"  env={json.dumps(row['env'], ensure_ascii=False)}")
            print(f"  依据：{row['rationale']}")
            print(f"  代码位置：{row['codeRefs']}")
            print(f"  env 真被读取：{json.dumps(row['envsActuallyRead'], ensure_ascii=False)}")
        if payload["missingEnvsReadPoints"]:
            print(f"\n!! 以下 env 在代码里找不到读取点（纸面参数）：{payload['missingEnvsReadPoints']}")
    return 0


def cmd_plan(out_path: str) -> int:
    support = probe_env_support()
    plan = {
        "kind": "arch-compare-plan",
        "question": "不同 harness 设计风格（重封装/轻量/插件式）在真实任务上的收益能否覆盖 token 成本？",
        "designs": {k: {"env": v["env"], "title": v["title"]} for k, v in DESIGNS.items()},
        "envsVerified": support,
        "blocked": ([e for e, ok in support.items() if not ok] or None),
        "protocol": [
            "先跑 eval/holdout_calibrate.py 确认题池有区分度",
            "每种设计独立 workspace + manifest（路径绑定 design 名）",
            "同一批题、同模型、同判据；用 eval/roi.py pair 出 Δcorrect 与 CI",
            "报告三层：always-fail / always-pass / discriminating，不得只看总数",
        ],
        "note": "blocked 非空时不得宣称该设计维度可评测——先把开关做成真实可配置",
    }
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "designs": len(DESIGNS),
                      "blocked": plan["blocked"]}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Harness 架构对比（试验场）")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("axes"); p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda a: cmd_axes(a.json))
    p = sub.add_parser("plan"); p.add_argument("--out", default="eval/runs/arch/plan.json")
    p.set_defaults(func=lambda a: cmd_plan(a.out))
    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
