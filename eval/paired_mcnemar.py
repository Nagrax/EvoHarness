#!/usr/bin/env python3
"""McNemar 配对检验：进化臂 vs 基线，同一题上的对/错配对。

为什么用配对检验而不是 CI（2026-10-29 判定）：
进化实验的命中极稀疏（4/72），二项分布 95% CI 会宽到没有意义。
配对检验只看"**同一题上两臂结果不同**的那些观测"，对稀疏事件灵敏得多。

前提：两臂必须跑**同一批题**。本脚本因此强制校验：
- 若 checkpoint 带 `holdout_task_ids` → 按 task_id 配对（严格）
- 若没有 → **拒绝做配对**，只输出描述性统计（不假装能算 p 值）

输出：2×2 配对表 + 精确 McNemar p 值（双侧）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
RUNS = REPO / "eval" / "runs" / "evolution"


def _load(arm: str, batch: str | None) -> list[dict]:
    """载入某臂所有 checkpoint（可按批次过滤）。"""
    out = []
    for path in sorted(RUNS.glob("checkpoint-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("arm") != arm:
            continue
        parts = path.stem.split("-")
        tag = parts[1] if len(parts) >= 4 else "legacy"
        if batch and tag != batch:
            continue
        flags = data.get("correct_flags") or []
        ids = data.get("holdout_task_ids") or []
        out.append({
            "file": path.name,
            "round": int(data.get("round") or 0),
            "flags": [1 if f else 0 for f in flags],
            "ids": [str(x) for x in ids],
        })
    return out


def pair(arm_rows: list[dict], base_rows: list[dict]) -> tuple[dict, dict]:
    """按 task_id 配对；无 id 时返回空配对并说明原因。"""
    by_arm: dict[str, int] = {}
    by_base: dict[str, int] = {}
    id_available = True
    for row in arm_rows:
        if not row["ids"]:
            id_available = False
            break
        for tid, flag in zip(row["ids"], row["flags"]):
            by_arm[f"{row['round']}:{tid}"] = flag
    for row in base_rows:
        if not row["ids"]:
            id_available = False
            break
        for tid, flag in zip(row["ids"], row["flags"]):
            by_base[f"{row['round']}:{tid}"] = flag

    if not id_available:
        return {}, {"idAvailable": False,
                    "reason": "checkpoint 缺 holdout_task_ids（历史批次无法配对）"}

    table = {"bothCorrect": 0, "armOnly": 0, "baseOnly": 0, "bothWrong": 0}
    shared = sorted(set(by_arm) & set(by_base))
    for key in shared:
        a, b = by_arm[key], by_base[key]
        if a and b:
            table["bothCorrect"] += 1
        elif a and not b:
            table["armOnly"] += 1
        elif b and not a:
            table["baseOnly"] += 1
        else:
            table["bothWrong"] += 1
    return table, {"idAvailable": True, "sharedTasks": len(shared),
                   "armTasks": len(by_arm), "baseTasks": len(by_base)}


def mcnemar_exact(b: int, c: int) -> float:
    """精确 McNemar（双侧）：b/c 是两臂结果不同的那两个格子。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    # 2 * P(X <= k), X ~ Binomial(n, 0.5)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", default="gated_evolution")
    parser.add_argument("--baseline", default="no_evolution")
    parser.add_argument("--batch", default="")
    args = parser.parse_args()

    arm_rows = _load(args.arm, args.batch or None)
    base_rows = _load(args.baseline, args.batch or None)
    if not arm_rows or not base_rows:
        print(json.dumps({"ok": False, "reason": "missing-arm-data",
                          "arm": len(arm_rows), "base": len(base_rows)},
                         ensure_ascii=False))
        return 2

    table, meta = pair(arm_rows, base_rows)
    report: dict = {
        "ok": True,
        "arm": args.arm,
        "baseline": args.baseline,
        "batch": args.batch or "all",
        "armRounds": len(arm_rows),
        "baseRounds": len(base_rows),
        **meta,
    }

    if not meta.get("idAvailable"):
        report["ok"] = False
        report["verdict"] = "not-pairable:missing-task-ids"
        report["note"] = ("历史 checkpoint 无 task_ids → 顺序相同也不等于题目相同，"
                          "拒绝做配对检验（不假装能算 p 值）")
        report["armHits"] = sum(sum(r["flags"]) for r in arm_rows)
        report["baseHits"] = sum(sum(r["flags"]) for r in base_rows)
    else:
        p = mcnemar_exact(table["armOnly"], table["baseOnly"])
        report.update({
            "table": table,
            "mcnemarP": round(p, 4),
            "verdict": ("significant" if p < 0.05 else "not-significant"),
            "note": ("精确 McNemar 双侧；只看两臂结果不同的题（armOnly vs baseOnly）"),
        })

    out = REPO / "eval" / "runs" / "paired-mcnemar.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())