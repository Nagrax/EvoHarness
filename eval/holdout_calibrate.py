#!/usr/bin/env python3
"""holdout 难度校准：避免"全 0 分"让对照实验失去信号。

真实问题（本轮实测踩到）：
  tool_policy 轴在 r1t1h1 / r2t2h2 / r3t3h3 三种规模下，多次出现 baseline 与变体**都是 0 分**。
  0 对 0 不是"效果相同"，而是"这批题对当前 harness 全做不出来"——对照实验没有信号。

本模块的作用：
  1. 从既有 predictions 里统计每题的通过情况，找出"太难"（全配置都失败）与"太易"（全配置都通过）的题；
  2. 给出可用于对照实验的题池建议（有区分度的题）；
  3. 在计划层面显式记录难度分层，避免再次用无信号题池做结论。

用法：
  python eval/holdout_calibrate.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "eval" / "runs"


def load_correct(tag: str) -> dict[str, bool]:
    """按官方判据（run_benchmark.classify_record）逐题判定，不用猜字段。

    真踩过的坑：predictions.jsonl 里**没有** `correct` 字段（只有 gold/prediction/
    tokens 等），初版直接读 `row["correct"]` 会把 165 题全部当成失败，
    得出"GAIA 全 0 分"这种与 summary 完全矛盾的结论。这里改用仓库真实判分函数。
    """
    path = RUNS / tag / "predictions.jsonl"
    if not path.is_file():
        return {}
    sys.path.insert(0, str(REPO))
    try:
        from eval.run_benchmark import classify_record
    except Exception:  # noqa: BLE001
        return {}
    dataset = "hle" if "hle" in tag else "gaia"
    out: dict[str, bool] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        key = str(row.get("task_id") or row.get("id") or "")
        if not key:
            continue
        out[key] = classify_record(row, dataset) == "correct"
    return out


def calibrate(tags: list[str]) -> dict:
    per_tag = {tag: load_correct(tag) for tag in tags}
    per_tag = {k: v for k, v in per_tag.items() if v}
    if len(per_tag) < 2:
        return {"ok": False, "reason": "need-at-least-two-tags-with-predictions",
                "have": list(per_tag)}

    # 只看共同题
    common = set.intersection(*(set(v) for v in per_tag.values()))
    if not common:
        return {"ok": False, "reason": "no-common-tasks", "tags": list(per_tag)}

    always_fail: list[str] = []
    always_pass: list[str] = []
    discriminating: list[str] = []
    for key in sorted(common):
        outcomes = [per_tag[tag][key] for tag in per_tag]
        if all(not x for x in outcomes):
            always_fail.append(key)
        elif all(x for x in outcomes):
            always_pass.append(key)
        else:
            discriminating.append(key)

    total = len(common)
    return {
        "ok": True,
        "tags": list(per_tag),
        "commonTasks": total,
        "alwaysFail": len(always_fail),
        "alwaysPass": len(always_pass),
        "discriminating": len(discriminating),
        "discriminatingRate": round(len(discriminating) / total * 100, 1) if total else 0.0,
        "buckets": {
            "alwaysFailIds": always_fail[:50],
            "alwaysPassIds": always_pass[:50],
            "discriminatingIds": discriminating[:50],
        },
        "verdict": (
            "no-signal:all-configs-fail" if not always_pass and not discriminating
            else "no-signal:all-configs-pass" if not always_fail and not discriminating
            else "usable-with-stratification"
        ),
        "recommendation": [
            "对照实验的报错题应分层统计：always-fail（无区分度）、always-pass（无区分度）、discriminating（有区分度）",
            "只在 discriminating 子集上比较配置优劣；全 0/全 1 只能说明难度不匹配，不能说明配置优劣",
            "若 alwaysFail 比例过高，应先换题或降低难度，而不是扩大样本烧 token",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tags", default="gaia,gaia-fold03")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    result = calibrate(tags)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    out = RUNS / "holdout-calibration.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
