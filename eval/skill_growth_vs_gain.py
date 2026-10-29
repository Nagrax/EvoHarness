#!/usr/bin/env python3
"""检验核心论断：**进化增益是否随技能累积而增长**？

论断来源（W14 两批结论）：「技能从 0 涨到 8，命中没有随之增长」——
目前只是定性描述。本脚本用现有 checkpoint 数据做定量检验：

1. 从每轮 checkpoint 提取 (技能数, 该轮 holdout 命中率)
2. 按技能数分箱，看命中率是否随技能数上升
3. 算 Spearman 秩相关（不假设线性，小样本稳健）
4. 同时给"前 1/3 轮 vs 后 1/3 轮"的命中对比

**零 token 成本**——只读已有产物。若能定量支持，这条就成了可宣称的发现；
若不支持，就必须撤回那句定性描述。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
RUNS = REPO / "eval" / "runs" / "evolution"


def load_points() -> list[dict]:
    """从所有 checkpoint 提取 (batch, arm, round, skills, hits, total)。"""
    points = []
    for path in sorted(RUNS.glob("checkpoint-*-r*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        flags = data.get("correct_flags") or []
        if not flags:
            continue
        arm = str(data.get("arm") or "")
        if arm == "no_evolution":
            continue
        # 批次标签：新版文件名 checkpoint-<batch>-<arm>-r<n>.json
        parts = path.stem.split("-")
        batch = parts[1] if len(parts) >= 4 else "legacy"
        points.append({
            "file": path.name,
            "batch": batch,
            "arm": arm,
            "round": int(data.get("round") or 0),
            "skills": int(data.get("skill_count") or 0),
            "hits": sum(1 for f in flags if f),
            "total": len(flags),
        })
    return points


def spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman 秩相关（无 scipy 依赖）。"""
    n = len(xs)
    if n < 3:
        return float("nan")

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


def main() -> int:
    points = load_points()
    if not points:
        print(json.dumps({"ok": False, "reason": "no-checkpoints"}, ensure_ascii=False))
        return 2

    print(f"points: {len(points)} (arms={sorted({p['arm'] for p in points})}, "
          f"batches={sorted({p['batch'] for p in points})})")

    # ① 逐轮明细
    print(f"\n{'batch':12s}{'arm':20s}{'rnd':>4s}{'skills':>7s}{'hits':>6s}{'rate':>7s}")
    for p in sorted(points, key=lambda x: (x["batch"], x["arm"], x["round"])):
        rate = p["hits"] / p["total"] * 100 if p["total"] else 0
        print(f"{p['batch']:12s}{p['arm']:20s}{p['round']:>4d}{p['skills']:>7d}"
              f"{p['hits']:>6d}{rate:>6.1f}%")

    # ② 技能数 vs 命中率 的秩相关
    xs = [float(p["skills"]) for p in points]
    ys = [p["hits"] / p["total"] for p in points if p["total"]]
    rho = spearman(xs, ys)

    # ③ 前 1/3 vs 后 1/3（按轮次）
    ordered = sorted(points, key=lambda x: (x["arm"], x["round"]))
    third = max(1, len(ordered) // 3)
    early = ordered[:third]
    late = ordered[-third:]

    def rate(group: list[dict]) -> float:
        hits = sum(p["hits"] for p in group)
        total = sum(p["total"] for p in group)
        return hits / total * 100 if total else 0.0

    early_rate, late_rate = rate(early), rate(late)

    # ④ 按技能数分箱
    bins: dict[int, dict] = {}
    for p in points:
        b = bins.setdefault(p["skills"], {"hits": 0, "total": 0, "rounds": 0})
        b["hits"] += p["hits"]
        b["total"] += p["total"]
        b["rounds"] += 1

    print(f"\n=== 技能数 vs 命中率 ===")
    print(f"{'skills':>7s}{'rounds':>8s}{'hits/total':>14s}{'rate':>8s}")
    for skills in sorted(bins):
        b = bins[skills]
        r = b["hits"] / b["total"] * 100 if b["total"] else 0
        print(f"{skills:>7d}{b['rounds']:>8d}{b['hits']:>8d}/{b['total']:<5d}{r:>7.1f}%")

    report = {
        "ok": True,
        "points": len(points),
        "spearmanSkillsVsHitRate": round(rho, 3) if rho == rho else None,
        "earlyThirdRate": round(early_rate, 1),
        "lateThirdRate": round(late_rate, 1),
        "deltaLateVsEarly": round(late_rate - early_rate, 1),
        "skillsRange": [min(xs), max(xs)],
        "verdict": (
            "supports-saturation" if (rho == rho and rho <= 0.2 and late_rate <= early_rate + 5)
            else "does-not-support-saturation"),
        "note": "零 token 成本；仅重读既有 checkpoint",
    }
    out = REPO / "eval" / "runs" / "skill-growth-vs-gain.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n" + json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())