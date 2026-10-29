#!/usr/bin/env python3
"""打印 backlog 中若干条目的源码上下文，便于判断是真缺陷还是合理防御。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    state = json.loads((REPO / "eval" / "loop" / "BACKLOG.json").read_text(encoding="utf-8"))
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    rule = sys.argv[2] if len(sys.argv) > 2 else ""
    shown = 0
    for item in state["items"]:
        detail = item["detail"]
        if rule and detail.get("rule") != rule:
            continue
        path = REPO / detail["file"]
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        line_no = detail["line"]
        start = max(0, line_no - 4)
        ctx = " | ".join(l.strip() for l in lines[start:line_no + 2])
        print(f"{item['id']} {detail['rule']} {detail['file']}:{line_no}")
        print(f"    {ctx[:220]}")
        shown += 1
        if shown >= limit:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
