#!/usr/bin/env python3
"""打印 backlog 中若干条目的源码上下文，便于判断是真缺陷还是合理防御。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")


def _usage() -> str:
    return ("用法: python eval/loop_context.py [limit] [rule]\n"
            "  limit  最多显示多少条（默认 20）\n"
            "  rule   只显示该 rule 的条目（如 silent-except）")


def main() -> int:
    if any(a in {"-h", "--help"} for a in sys.argv[1:]):
        print(_usage())
        return 0
    # stdout 可能是 StringIO / 包装器（被测试或 CI 捕获时），没有 reconfigure。
    # 编码设置失败不应让整个排查工具失效——退化为按现状输出即可。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    state = json.loads((REPO / "eval" / "loop" / "BACKLOG.json").read_text(encoding="utf-8"))
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    rule = sys.argv[2] if len(sys.argv) > 2 else ""
    shown = 0
    for item in state["items"]:
        detail = item.get("detail") or {}
        raw_rule = detail.get("rule")
        # rule 可能是 str、None 或 list（历史条目未统一），统一成可比较的字符串
        if isinstance(raw_rule, (list, tuple)):
            rule_text = " | ".join(str(x) for x in raw_rule)
        elif isinstance(raw_rule, str):
            rule_text = raw_rule
        else:
            rule_text = ""
        if rule and raw_rule != rule and rule not in rule_text:
            continue
        file_rel = detail.get("file")
        if not isinstance(file_rel, str) or not file_rel:
            continue
        path = REPO / file_rel
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        line_no = detail.get("line")
        # line 可能是 None（聚合条目）或超出文件长度
        if not isinstance(line_no, int) or line_no < 0:
            continue
        line_no = min(line_no, len(lines))
        start = max(0, line_no - 4)
        ctx = " | ".join(l.strip() for l in lines[start:line_no + 2])
        print(f"{item['id']} {rule_text} {file_rel}:{line_no}")
        print(f"    {ctx[:220]}")
        shown += 1
        if shown >= limit:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
