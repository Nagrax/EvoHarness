#!/usr/bin/env python3
"""从稳定性产物中提取 **flaky 题**，作为高信息密度实验的题池。

为什么用 flaky 题（2026-10-29 判定）：
- stable-fail 题（reproRate=1.0）：正确率恒 0 → 任何学习/进化效应都测不出
- disappeared 题（reproRate=0.0）：恒对 → 天花板效应，同样测不出
- **flaky 题（reproRate≈0.5）：有时对有时错** → 单次观测信息量最大，
  少量的题目就能积累出统计功效

用法：
  python eval/extract_flaky_pool.py                    # 汇总所有稳定性产物
  python eval/extract_flaky_pool.py --out pool.json    # 导出题池
  python eval/extract_flaky_pool.py --make-tasks pool.jsonl   # 生成实验用 jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
RUNS = REPO / "eval" / "runs"


def collect() -> dict:
    """合并所有 failure-stability-*.json 的 perTask。"""
    pooled: dict[str, dict] = {}
    sources = []
    for path in sorted(RUNS.glob("failure-stability-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sources.append(path.name)
        for task in data.get("perTask") or []:
            tid = str(task.get("taskId") or "")
            if not tid:
                continue
            entry = pooled.setdefault(tid, {
                "taskId": tid,
                "questionHead": task.get("questionHead") or "",
                "observations": [],
            })
            entry["observations"].append({
                "repeats": task.get("repeats"),
                "noMarkerCount": task.get("noMarkerCount"),
                "reproRate": task.get("reproRate"),
                "status": task.get("status"),
                "source": path.name,
            })

    for entry in pooled.values():
        rates = [o["reproRate"] for o in entry["observations"]
                 if isinstance(o.get("reproRate"), (int, float))]
        if not rates:
            entry["classification"] = "unknown"
        elif all(r <= 0.0 for r in rates):
            entry["classification"] = "always-pass"
        elif all(r >= 1.0 for r in rates):
            entry["classification"] = "always-fail"
        else:
            entry["classification"] = "flaky"
        entry["meanReproRate"] = round(sum(rates) / len(rates), 3) if rates else None

    by_class: dict[str, list[str]] = {}
    for entry in pooled.values():
        by_class.setdefault(entry["classification"], []).append(entry["taskId"])

    return {
        "ok": True,
        "sources": sources,
        "pooled": len(pooled),
        "byClass": {k: len(v) for k, v in by_class.items()},
        "flakyIds": sorted(by_class.get("flaky") or []),
        "alwaysFailIds": sorted(by_class.get("always-fail") or []),
        "alwaysPassIds": sorted(by_class.get("always-pass") or []),
        "tasks": pooled,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="eval/runs/flaky-pool.json")
    parser.add_argument("--make-tasks", default="", help="生成实验 jsonl 的路径")
    args = parser.parse_args()

    data = collect()
    out = REPO / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({k: data[k] for k in ("pooled", "byClass", "sources")},
                     ensure_ascii=False, indent=2))
    print(f"flaky ids ({len(data['flakyIds'])}): {data['flakyIds'][:6]}")

    if args.make_tasks and data["flakyIds"]:
        # 生成实验用 jsonl：从 GAIA 数据集取题干与答案
        dataset = REPO / "eval" / "data" / "gaia-validation.jsonl"
        if not dataset.is_file():
            candidates = list((REPO / "eval" / "data").glob("*gaia*.jsonl"))
            dataset = candidates[0] if candidates else dataset
        if not dataset.is_file():
            print(json.dumps({"ok": False, "reason": "gaia-dataset-not-found"},
                             ensure_ascii=False))
            return 2
        wanted = set(data["flakyIds"])
        rows = []
        for line in dataset.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            tid = str(item.get("task_id") or item.get("id") or "")
            if tid in wanted:
                rows.append(item)
        dest = REPO / args.make_tasks
        dest.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                        encoding="utf-8")
        print(f"wrote {len(rows)} tasks -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())