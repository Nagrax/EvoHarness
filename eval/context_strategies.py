"""W15 上下文四策略定义：把 E-05 四策略映射到真实 env/机制。

S1 无折叠        → EVOHARNESS_DISABLE_AUTO_FOLD=1（自动压缩禁用；手动工具保留）
S2 当前折叠      → 默认整段折叠（keep=0）
S3 尾部保留 N 轮  → EVOHARNESS_FOLD_KEEP_RECENT_TURNS=4（先单因素校准 N）
S4 阶段交接      → 阶段机激活（plan 文件；runner 以 staged 会话启动）

约束（FROZEN_PROTOCOL.json qualityConstraints）：同题/同模型/同预算配对；
非劣效界 = 配对差值 95% 下界 >= -2pp；不足以证明不退化则保留实验开关。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

STRATEGIES: dict[str, dict] = {
    "s1_no_fold": {
        "title": "无折叠（对照下界：上下文自然增长）",
        "env": {"EVOHARNESS_DISABLE_AUTO_FOLD": "1"},
        "mechanism": "只挡自动压缩；compact_context 工具仍可用",
    },
    "s2_current_fold": {
        "title": "当前折叠（整段折叠，keep=0）",
        "env": {},
        "mechanism": "默认行为：利用率>70% 整段折叠为三层结构",
    },
    "s3_tail_keep": {
        "title": "尾部保留最近 4 轮",
        "env": {"EVOHARNESS_FOLD_KEEP_RECENT_TURNS": "4"},
        "mechanism": "折叠旧段，保留最近 4 个完整轮原文（消融后的第一修复方向）",
    },
    "s4_stage_handover": {
        "title": "阶段交接（plan→execute→verify→deliver）",
        "env": {},
        "mechanism": "阶段机激活：70% 强制收段，LLM 交接，raw 会话被 handover 替换",
        "requiresStaged": True,
    },
}

# 单因素校准：S3 的 N 先在 {2,4,8} 中校准再冻结（验收："先单因素校准N"）
S3_CALIBRATION_GRID = [2, 4, 8]
S3_FROZEN_N = 4


def plan_payload(strategy: str, *, rounds: int, holdout: int) -> dict:
    spec = STRATEGIES[strategy]
    return {
        "strategy": strategy,
        "title": spec["title"],
        "env": spec["env"],
        "requiresStaged": spec.get("requiresStaged", False),
        "scale": {"rounds": rounds, "holdout": holdout},
        "protocol": [
            "同题同模型同预算：同一 split manifest、同一 arm 集",
            "每策略独立 workspace（env/阶段机互不污染）",
            "记录：上下文超限率、成功率差值CI、重复工具数、token/成本、耗时、每次压缩前后引用",
            "非劣效判据：配对差值 95% 下界 >= -2pp（对现任 s2）",
            "不足以证明不退化 → 保留实验开关，不改默认",
        ],
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan"); p.add_argument("--out", default="eval/runs/context/plan.json")
    p.add_argument("--rounds", type=int, default=6)
    p.add_argument("--holdout", type=int, default=6)
    def _plan(a):
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: plan_payload(k, rounds=a.rounds, holdout=a.holdout) for k in STRATEGIES}
        payload["s3CalibrationGrid"] = S3_CALIBRATION_GRID
        payload["s3FrozenN"] = S3_FROZEN_N
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "path": str(out), "strategies": len(STRATEGIES)},
                         ensure_ascii=False, indent=2))
        return 0
    p.set_defaults(func=_plan)
    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
