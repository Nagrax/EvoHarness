#!/usr/bin/env python3
"""功效分析：用 flaky 题做进化对照，**需要多少观测**才能检出效应？

为什么先算（2026-10-29，预算解除后）：
用户解除了 300M 限制，可以大胆烧 token。但**盲目扩样是最贵的错误**——
之前 W14 用 stable-fail 题（正确率恒 0），烧了 40+ 轮也只得到 3-4 次命中，
统计上永远不显著。

本脚本用蒙特卡洛模拟回答：在 flaky 题池上，
- 给定每题单次运行的失败率 p_base
- 给定进化的相对改善 r（如失败率从 0.5 降到 0.4）
- 给定每臂观测数 n
→ **McNemar 检出显著（p<0.05）的概率（power）是多少**

产出：一张"观测数 → power"表，直接指导实验规模。
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def simulate_power(p_base: float, p_treat: float, n: int,
                   trials: int = 2000, seed: int = 7) -> float:
    """配对模拟：同一题，基线失败率 p_base、处理组失败率 p_treat。

    只有"两臂结果不同"的题进 McNemar 的 b/c 格。
    """
    rng = random.Random(seed)
    hits = 0
    for _ in range(trials):
        b = c = 0
        for _ in range(n):
            base_fail = rng.random() < p_base
            treat_fail = rng.random() < p_treat
            # b = 处理组成功而基线失败（改善）；c = 反向（恶化）
            if base_fail and not treat_fail:
                b += 1
            elif treat_fail and not base_fail:
                c += 1
        if mcnemar_exact(b, c) < 0.05:
            hits += 1
    return hits / trials


def main() -> int:
    scenarios = [
        ("flaky 0.50 -> 0.40（相对 -20%）", 0.50, 0.40),
        ("flaky 0.50 -> 0.35（相对 -30%）", 0.50, 0.35),
        ("flaky 0.30 -> 0.24（相对 -20%）", 0.30, 0.24),
        ("stable-fail 1.00 -> 0.90（W14 现状）", 1.00, 0.90),
    ]
    sizes = [20, 40, 60, 100, 150, 200]

    print(f"{'scenario':38s}" + "".join(f"{n:>8d}" for n in sizes))
    table = {}
    for name, p_base, p_treat in scenarios:
        row = []
        for n in sizes:
            power = simulate_power(p_base, p_treat, n, trials=1500)
            row.append(round(power, 3))
        table[name] = dict(zip([str(s) for s in sizes], row))
        print(f"{name:38s}" + "".join(f"{p:>8.2f}" for p in row))

    # 找到"首个达到 0.8 power"的观测数
    conclusions = {}
    for name, p_base, p_treat in scenarios:
        needed = None
        for n in (10, 20, 30, 40, 60, 80, 100, 150, 200, 300, 400, 600):
            if simulate_power(p_base, p_treat, n, trials=800) >= 0.8:
                needed = n
                break
        conclusions[name] = needed

    report = {
        "ok": True,
        "note": ("蒙特卡洛 power 分析；n = 每臂观测数；"
                 "b/c 只统计两臂结果不同的观测"),
        "powerTable": table,
        "n_for_80pct_power": conclusions,
        "interpretation": {
            "flaky-20pct-gain": (
                "若要检出 20% 相对改善，flaky 题需要 ~"
                f"{conclusions.get('flaky 0.50 -> 0.40（相对 -20%）')} 个观测/臂；"
                "按 1M token/次，两臂约 "
                f"{(conclusions.get('flaky 0.50 -> 0.40（相对 -20%）') or 0) * 2} M tokens"),
            "w14-status": (
                "W14 用 stable-fail 题（1.00→0.90），"
                f"80% power 需要 ~{conclusions.get('stable-fail 1.00 -> 0.90（W14 现状）')} "
                "个观测/臂——这就是它一直不显著的原因"),
        },
    }
    out = REPO / "eval" / "runs" / "power-analysis.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n80% power 所需观测/臂：")
    for name, n in conclusions.items():
        print(f"  {name:38s} n = {n}")
    print("\n" + json.dumps(report["interpretation"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())