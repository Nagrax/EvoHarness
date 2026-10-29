#!/usr/bin/env python3
"""Harness² 对照结果分析：把配置实验的 Δcorrect / Δtokens 折算成 ROI 判断。

判据复用 eval/roi.py 的红线：
  · Δcorrect < 0 且省 token → 明确为负（省 token 不是收益）
  · CI 跨 0 或 n 太小 → not-claimable
用法：python eval/harness2_analyze.py [--axis tool_policy]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "eval" / "runs" / "harness2"
MIN_N = 30


def analyze(axis: str) -> dict:
    path = OUT / f"run-{axis}.json"
    if not path.is_file():
        return {"ok": False, "reason": "run-missing", "axis": axis}
    report = json.loads(path.read_text(encoding="utf-8"))
    results = report.get("results") or []
    n = int((report.get("scale") or {}).get("holdout") or 0)

    baseline = next((r for r in results if r.get("variant") == "baseline"), None)
    if baseline is None:
        return {"ok": False, "reason": "baseline-missing", "axis": axis}
    base_point = baseline.get("lastPoint") or {}
    base_pass = float(base_point.get("pass_at_1") or 0.0)
    base_tok = float(baseline.get("totalTokensM") or 0.0)

    comparisons = []
    for record in results:
        if record.get("variant") == "baseline":
            continue
        point = record.get("lastPoint") or {}
        variant_pass = float(point.get("pass_at_1") or 0.0)
        variant_tok = float(record.get("totalTokensM") or 0.0)
        delta_pass = variant_pass - base_pass
        delta_tok = variant_tok - base_tok
        token_saving_pct = (-delta_tok / base_tok * 100) if base_tok else None

        if delta_pass < 0:
            # 掉质量时，token 是否省下来只是次要信息：省了也是负收益。
            if delta_tok < 0:
                verdict = "negative:quality-loss-outweighs-savings"
            elif delta_tok > 0:
                verdict = "negative:quality-loss-and-more-cost"
            else:
                verdict = "negative:quality-loss-at-same-cost"
        elif base_pass == 0.0 and variant_pass == 0.0:
            # 基线本就 0 分：这类题对当前 harness 是"做不出来"，两种配置都没解决问题。
            # 此时"同质量更便宜"没有意义——省下的只是失败的成本，不是效率提升。
            verdict = "uninformative:both-zero-baseline"
        elif delta_pass > 0 and delta_tok <= 0:
            verdict = "positive:better-and-cheaper"
        elif delta_pass > 0 and delta_tok > 0:
            verdict = "tradeoff:paid-more-got-more"
        elif delta_pass == 0 and delta_tok < 0:
            verdict = "positive:same-quality-cheaper"
        else:
            verdict = "neutral"

        if n < MIN_N and verdict.startswith("positive"):
            verdict += ":but-not-claimable-n<" + str(MIN_N)

        comparisons.append({
            "variant": record["variant"],
            "env": record.get("env") or {},
            "passAt1": variant_pass,
            "tokensM": variant_tok,
            "deltaPassAt1": round(delta_pass, 2),
            "deltaTokensM": round(delta_tok, 3),
            "tokenSavingPct": (round(token_saving_pct, 1) if token_saving_pct is not None else None),
            "roiPerMillionTokens": (round(delta_pass / delta_tok, 2) if delta_tok else None),
            "verdict": verdict,
        })

    return {
        "ok": True,
        "axis": axis,
        "nPerVariant": n,
        "baseline": {"passAt1": base_pass, "tokensM": base_tok},
        "comparisons": comparisons,
        "claimRule": f"n={n} < {MIN_N}：所有正向结论一律不可宣称；负向结论（掉质量）可如实报告",
        "whatThisProves": [
            "harness 配置是**真实可改**的（env 生效，见 tests/test_harness_axes_configurable.py）",
            "对照实验能给出 Δcorrect 与 Δtokens，从而判断收益是否对冲成本",
            "省 token 但掉正确率必须判负——这是本项目对 RSI 权衡问题的直接回答方式",
        ],
        "whatThisDoesNotProve": [
            "任何变体在统计意义上更好/更差（n 太小）",
            "最优点在哪（需要 W14 规模的曲线与更多变体）",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--axis", default="tool_policy")
    args = parser.parse_args()
    result = analyze(args.axis)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    out = OUT / f"analysis-{args.axis}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
