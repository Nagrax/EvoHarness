#!/usr/bin/env python3
"""生命周期 A/B 的跨批次噪声门：分指标判定"可宣称 / 不可宣称"。

为什么需要它：单批次里 soft 臂看起来全面胜出（召回 1.000、零污染）；
跨批次复核后**召回维度的差异方向反号**（+3.3pp / −6.7pp），
而**污染维度两批完全一致**（off=2, soft=0）。同一个实验里，
有的结论能宣称、有的不能——必须逐指标判，不能整包宣称。

判定规则（对齐 FROZEN_PROTOCOL.statistics.varianceGate）：
  某指标可宣称 ⇔ ① 方向跨批次一致 ② |均值差| > 该指标的跨批次极差

用法：
  python eval/lifecycle_noise_gate.py eval/runs/memory-lifecycle-ab-v3.json eval/runs/memory-lifecycle-ab-v3b.json
退出码：0 判读完成；2 批次不足（<2 个无法做噪声检验）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_batches(paths: list[Path]) -> list[dict]:
    out = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({"name": path.name, "data": data})
    return out


def _normalize_batch(data: dict) -> dict:
    """把不同实验报告的格式归一成 {armsDetail, baseline} 再判读。

    支持两种真实格式：
      1) memory-lifecycle-ab: {armsDetail: {...}, baseline: "off"}
      2) reflection-effect-ab: {noLesson: {...}, withLesson: {...}}（基线固定 noLesson）
    归一化是必要的抽象——否则每加一种实验就要复制一份噪声门逻辑。
    """
    if isinstance(data.get("armsDetail"), dict) and data.get("baseline"):
        return {"armsDetail": data["armsDetail"], "baseline": data["baseline"]}
    if isinstance(data.get("noLesson"), dict) and isinstance(data.get("withLesson"), dict):
        arms = {"no-lesson": dict(data["noLesson"]), "with-lesson": dict(data["withLesson"])}
        # 字段名对齐：反思报告用 finalMarkerRate，噪声门按 recallRate 比较"达标率"
        for arm in arms.values():
            if "finalMarkerRate" in arm and "recallRate" not in arm:
                arm["recallRate"] = arm["finalMarkerRate"]
        return {"armsDetail": arms, "baseline": "no-lesson"}
    return {"armsDetail": {}, "baseline": ""}


def judge(batches: list[dict]) -> dict:
    if len(batches) < 2:
        return {"ok": False, "reason": "need-at-least-2-batches"}

    normalized = [_normalize_batch(b["data"]) for b in batches]
    if not normalized[0]["armsDetail"]:
        return {"ok": False, "reason": "unsupported-report-format",
                "batches": [b["name"] for b in batches]}

    arms = list(normalized[0]["armsDetail"].keys())
    baseline = normalized[0]["baseline"]
    per_metric: dict[str, dict] = {}

    for arm in arms:
        if arm == baseline:
            continue
        for metric in ("recallRate", "contamination", "tokens"):
            values = [n["armsDetail"].get(arm, {}).get(metric) for n in normalized]
            base_values = [n["armsDetail"].get(baseline, {}).get(metric) for n in normalized]
            if any(v is None for v in values) or any(v is None for v in base_values):
                continue
            diffs = [v - b for v, b in zip(values, base_values)]
            spread = max(values) - min(values)
            base_spread = max(base_values) - min(base_values)
            noise = max(spread, base_spread)
            mean_diff = sum(diffs) / len(diffs)
            direction_consistent = all(d > 0 for d in diffs) or all(d < 0 for d in diffs)
            exceeds_noise = abs(mean_diff) > noise
            claimable = direction_consistent and exceeds_noise and abs(mean_diff) > 0
            per_metric[f"{arm}:{metric}"] = {
                "arm": arm,
                "metric": metric,
                "perBatchArm": values,
                "perBatchBaseline": base_values,
                "perBatchDiff": [round(d, 4) for d in diffs],
                "meanDiff": round(mean_diff, 4),
                "noise": round(noise, 4),
                "directionConsistent": direction_consistent,
                "exceedsNoise": exceeds_noise,
                "claimable": claimable,
                "verdict": ("claimable" if claimable else
                            ("not-claimable:direction-flips" if not direction_consistent
                             else "not-claimable:delta-within-config-noise")),
            }
    return {
        "ok": True,
        "batches": [b["name"] for b in batches],
        "baseline": baseline,
        "arms": arms,
        "perMetric": per_metric,
        "claimableCount": sum(1 for v in per_metric.values() if v["claimable"]),
        "totalComparisons": len(per_metric),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("batches", nargs="+")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    paths = [Path(p) if Path(p).is_absolute() else REPO / p for p in args.batches]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(json.dumps({"ok": False, "missing": missing}, ensure_ascii=False))
        return 2
    result = judge(load_batches(paths))

    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "lifecycle-noise-gate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"batches: {result['batches']}  baseline: {result['baseline']}")
        print(f"claimable: {result['claimableCount']}/{result['totalComparisons']}\n")
        for key, item in result["perMetric"].items():
            flag = "✓" if item["claimable"] else "✗"
            print(f"{flag} {key:24s} diff={item['perBatchDiff']} "
                  f"mean={item['meanDiff']:+.3f} noise={item['noise']:.3f} "
                  f"→ {item['verdict']}")
        print(f"\n-> {out}")
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
