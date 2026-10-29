#!/usr/bin/env python3
"""Harness² 全轴汇总：把三个轴的对照结果并成一张表，并给出可宣称/不可宣称的边界。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
OUT = REPO / "eval" / "runs" / "harness2"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rows = []
    for analysis in sorted(OUT.glob("analysis-*.json")):
        data = json.loads(analysis.read_text(encoding="utf-8"))
        if not data.get("ok"):
            continue
        base = data.get("baseline") or {}
        for row in data.get("comparisons") or []:
            rows.append({
                "axis": data.get("axis"),
                "variant": row.get("variant"),
                "baselinePass": base.get("passAt1"),
                "variantPass": row.get("passAt1"),
                "baselineTokensM": base.get("tokensM"),
                "variantTokensM": row.get("tokensM"),
                "deltaPass": row.get("deltaPassAt1"),
                "deltaTokensM": row.get("deltaTokensM"),
                "tokenSavingPct": row.get("tokenSavingPct"),
                "verdict": row.get("verdict"),
                "n": data.get("nPerVariant"),
            })
    summary = {
        "kind": "harness2-summary",
        "nPerVariant": (rows[0]["n"] if rows else None),
        "rows": rows,
        "counts": {
            "total": len(rows),
            "negative": sum(1 for r in rows if str(r["verdict"]).startswith("negative")),
            "uninformative": sum(1 for r in rows if str(r["verdict"]).startswith("uninformative")),
            "claimable": sum(1 for r in rows if str(r["verdict"]).startswith(("positive", "tradeoff"))
                             and "not-claimable" not in str(r["verdict"])),
        },
        "whatThisProves": [
            "harness 配置面是真实可改的（env 生效，非法值回退，有 16 条回归固定）",
            "对照管线能产出 Δcorrect 与 Δtokens，从而判断收益是否对冲成本",
            "判别器能识别负收益（tight 变体：省 13.3% token 但掉 100pp → 判负）",
            "当两种配置都做不出该题时判 uninformative，不把'省了失败成本'包装成效率提升",
        ],
        "whatThisDoesNotProve": [
            "任何轴的优劣排序——每题 n=1，远低于可宣称门槛 n>=30",
            "'最优点'在哪——需要 W14 规模与更多变体",
            "论文数值复现——未读 Harness² 原文，不引用任何论文数字",
        ],
        "nextScaleUp": "每轴每变体 ≥30 题、≥3 轮，用 eval/roi.py pair 做 bootstrap CI",
    }
    out = OUT / "summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["counts"], ensure_ascii=False))
    for row in rows:
        print(f"  {row['axis']:16s} {row['variant']:12s} pass {row['baselinePass']}->{row['variantPass']} "
              f"tokM {row['baselineTokensM']}->{row['variantTokensM']} "
              f"saved={row['tokenSavingPct']}%  {row['verdict']}")
    print(f"\nsummary -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
