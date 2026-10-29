#!/usr/bin/env python3
"""运行间方差估计：同一配置重复跑，看结果有多不稳定。

真实问题（本轮实测）：`only-prefetch-off` 同一配置、同一题池、同一规模，
两次运行一次 pass=50.0、一次 pass=0.0。若不做方差估计，就会把
"这一次跑出来的差异"当成"配置差异"——这是对照实验最经典的陷阱。

本模块做三件事：
1. 读同一设计的多份 run 记录（按 runId/时间戳归档），算 pass@1 的极差与波动；
2. 给出"最小可检测差异"的粗略估计（噪声多大，差异要多大才有意义）；
3. 明确结论：方差 > 声称的差异时，必须判 not-claimable。

用法：
  python eval/variance_check.py --design only-prefetch-off
  python eval/variance_check.py --json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ARCH = REPO / "eval" / "runs" / "arch"
HIST = ARCH / "history"


def collect_history() -> list[dict]:
    """收集所有归档的 arch run（每次运行 append 一条到 history/）。"""
    rows: list[dict] = []
    if not HIST.is_dir():
        return rows
    for path in sorted(HIST.glob("run-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        scale = data.get("scale") or {}
        for record in data.get("results") or []:
            point = record.get("lastPoint") or {}
            if not point:
                continue
            rows.append({
                "file": path.name,
                "design": record.get("design"),
                "passAt1": float(point.get("pass_at_1") or 0.0),
                "tokensM": record.get("totalTokensM"),
                "rounds": scale.get("rounds"),
                "tasks": scale.get("tasks"),
                "holdout": scale.get("holdout"),
                "taskIdsFile": scale.get("taskIdsFile"),
                "env": record.get("env") or {},
            })
    return rows


def analyze(rows: list[dict], design: str | None) -> dict:
    filtered = [r for r in rows if design is None or r["design"] == design]
    if not filtered:
        return {"ok": False, "reason": "no-history", "hint": "先跑 eval/arch_run.py（会归档到 history/）"}

    by_design: dict[str, list[dict]] = {}
    for row in filtered:
        by_design.setdefault(str(row["design"]), []).append(row)

    summary = []
    for key, items in by_design.items():
        passes = [i["passAt1"] for i in items]
        tokens = [float(i["tokensM"]) for i in items if i.get("tokensM") is not None]
        spread = max(passes) - min(passes)
        # 同配置同规模才可比：这里只在规模一致的分组内算波动
        scales = {(i["rounds"], i["tasks"], i["holdout"], i["taskIdsFile"]) for i in items}
        summary.append({
            "design": key,
            "runs": len(items),
            "passAt1Values": passes,
            "passAt1Range": round(spread, 1),
            "passAt1Stdev": round(statistics.pstdev(passes), 1) if len(passes) > 1 else 0.0,
            "tokensMValues": tokens,
            "distinctScales": len(scales),
            "comparableWithinSameScale": len(scales) == 1,
        })
    summary.sort(key=lambda s: -s["passAt1Range"])

    unstable = [s for s in summary if s["passAt1Range"] > 0 and s["comparableWithinSameScale"]]
    worst = max((s["passAt1Range"] for s in unstable), default=0.0)

    return {
        "ok": True,
        "designs": summary,
        "unstableSameScale": [{"design": s["design"], "range": s["passAt1Range"],
                               "values": s["passAt1Values"]} for s in unstable],
        "maxObservedRangePts": worst,
        "implication": (
            f"同一配置在同规模下的实测极差达 {worst:.1f}pp。"
            "任何小于该极差的配置差异都不可宣称，必须先扩样本（n>=30）或重复多轮取均值。"
        ),
        "rule": [
            "对照实验的 claimed delta 必须显著大于同配置重复运行的极差",
            "n<30 时一律 not-claimable（eval/roi.py 已有判据）",
            "报告里必须给出同配置重复次数与极差，而不是只报单次结果",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = collect_history()
    result = analyze(rows, args.design or None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    out = ARCH / "variance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
