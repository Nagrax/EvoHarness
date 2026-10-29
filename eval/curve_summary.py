#!/usr/bin/env python3
"""打印进化实验曲线摘要（三臂 × 轮次）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "eval" / "runs" / "evolution" / "curve.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    print("arms:", data.get("arms"))
    print("evolution_fired_by_arm:", json.dumps(data.get("evolution_fired_by_arm"), ensure_ascii=False))
    print("new_skills_by_arm:", json.dumps(data.get("new_skills_by_arm"), ensure_ascii=False))
    print("holdout_readonly_ok_by_arm:", json.dumps(data.get("holdout_readonly_ok_by_arm"), ensure_ascii=False))
    print("total_tokens_M:", data.get("total_tokens_M"), "side_query_tokens_M:", data.get("side_query_tokens_M"))
    print("split_counts:", json.dumps((data.get("split_manifest") or {}).get("counts"), ensure_ascii=False))
    print("points:")
    for point in data.get("points") or []:
        failures = point.get("failures") or {}
        print(f"  r{point['round']} {point['arm']:20s} pass={point['pass_at_1']:5.1f} "
              f"n={point['n']} tokM={point['tokens_M']} "
              f"fail={json.dumps(failures, ensure_ascii=False)} "
              f"evolved={point.get('evolution_fired')} skills={point.get('skill_count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
