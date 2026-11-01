#!/usr/bin/env python3
"""按文件批量判读静默吞错：对某个聚合项里的每个位置套用判据，输出可执行结论。

判据与 eval/loop/README.md 一致：
  real-defect        吞掉用户可见状态（保存/加载/写入/记录/追加/同步/恢复）
  reasonable-defense 清理收尾（close/kill/unlink/rmtree/cleanup/cancel/discard）
  needs-review       其余

用法：
  python eval/triage_file.py <L-id>            # 打印该聚合项下每个位置的判读
  python eval/triage_file.py <L-id> --json
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

STATE_WORDS = ("save", "persist", "write_text", "write(", "flush", "commit",
               "publish", "promote", "append(", "extend(", "update(", "restore", "sync")
CLEANUP_WORDS = ("close", "kill", "terminate", "unlink", "rmtree", "shutdown",
                 "cleanup", "cancel", "discard", "remove", "drop", "release", "dispose")
# 降级型：失败只影响"优化/索引/缓存"，真相仍可从源头恢复（如内存索引、mtime 记录、
# 有界扫描里的单文件跳过）。这类不该算缺陷，也不该算清理型。
DEGRADE_WORDS = ("getmtime", "normcase", "normpath", "mem_path", "index", "cache",
                 "memory_index", "read_file_state", "walk(", "read_text(errors")


def block_source(path: Path, line: int, span: int = 14) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    start = max(0, line - 1)
    return "\n".join(lines[start:start + span])


def classify(path: Path, line: int) -> tuple[str, str]:
    text = block_source(path, line).lower()
    state_hits = sorted({w for w in STATE_WORDS if w in text})
    cleanup_hits = sorted({w for w in CLEANUP_WORDS if w in text})
    degrade_hits = sorted({w for w in DEGRADE_WORDS if w in text})
    # 优先级：清理 > 降级 > 状态。降级型失败可从源头恢复，不该按缺陷对待。
    if cleanup_hits and not state_hits:
        return "reasonable-defense", "cleanup:" + ",".join(cleanup_hits)
    if degrade_hits and not state_hits:
        return "degradation-only", "degrade:" + ",".join(degrade_hits)
    if state_hits and not cleanup_hits:
        return "real-defect", "state:" + ",".join(state_hits)
    if state_hits and cleanup_hits:
        return "real-defect", "state-priority:" + ",".join(state_hits) + "|cleanup:" + ",".join(cleanup_hits)
    if state_hits and degrade_hits:
        return "real-defect", "state-priority:" + ",".join(state_hits) + "|degrade:" + ",".join(degrade_hits)
    return "needs-review", "no-signal"


def _usage() -> str:
    return "用法: python eval/triage_file.py <L-id> [--json]"


def main() -> int:
    if any(a in {"-h", "--help"} for a in sys.argv[1:]):
        print(_usage())
        return 0
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print("usage: python eval/triage_file.py <L-id> [--json]")
        return 2
    item_id = sys.argv[1]
    as_json = "--json" in sys.argv
    state = json.loads((REPO / "eval" / "loop" / "BACKLOG.json").read_text(encoding="utf-8"))
    item = next((i for i in state["items"] if i["id"] == item_id), None)
    if item is None:
        print(json.dumps({"ok": False, "reason": "unknown-id"}, ensure_ascii=False))
        return 1

    rows = []
    for occurrence in item.get("openOccurrences") or []:
        rel = occurrence.get("file") or ""
        line = int(occurrence.get("line") or 0)
        category, reason = classify(REPO / rel, line)
        snippet = block_source(REPO / rel, line, span=6).replace("\n", " | ")[:180]
        rows.append({
            "fromId": occurrence.get("fromId"),
            "file": rel, "line": line,
            "category": category, "reason": reason, "snippet": snippet,
        })
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    report = {"ok": True, "itemId": item_id, "key": item["key"], "counts": counts, "rows": rows}
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{item_id} {item['key']}  open={len(rows)}  {json.dumps(counts, ensure_ascii=False)}")
        for row in rows:
            print(f"  [{row['category']:18s}] {row['file']}:{row['line']}  {row['reason'][:60]}")
            print(f"      {row['snippet'][:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
