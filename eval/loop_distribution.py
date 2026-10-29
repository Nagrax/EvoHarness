#!/usr/bin/env python3
"""打印 backlog 聚合项的 done/open 分布，核对迁移是否埋掉了待修项。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    state = json.loads((REPO / "eval" / "loop" / "BACKLOG.json").read_text(encoding="utf-8"))
    total_open = 0
    total_done = 0
    for item in state["items"]:
        occ = item.get("occurrences") or []
        done = [o for o in occ if o.get("status") == "done"]
        opened = item.get("openOccurrences") or []
        total_open += len(opened)
        total_done += len(done)
        print(f"{item['id']} {item['status']:9s} occ={len(occ):3d} done={len(done):3d} "
              f"open={len(opened):3d} {item['key'].split(':')[-1]}")
    print(f"TOTAL done={total_done} open={total_open}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
