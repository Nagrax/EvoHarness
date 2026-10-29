#!/usr/bin/env python3
"""把 BACKLOG 迁移到「文件+规则」聚合 key 口径。

背景：初版用 `file:line` 做 key，修一处代码会让后续行号整体漂移，同一信号每轮都被
当成新发现，backlog 只增不减（实测 r3 added=60 而信号总量未降）。

迁移规则：
- 旧 key `module:<rule>:<file>:<line>` → 新 key `module:<rule>:<file>`；
- 同新 key 的多条合并为一条，occurrences 收集全部位置；
- 任意一条已 done → 合并后保持 done（不能因为合并把已完成降级）；
- 其余状态取"最需要处理"的状态：in_progress > failed > partial > pending > blocked。

用法：python eval/migrate_backlog.py [--dry-run]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKLOG = REPO / "eval" / "loop" / "BACKLOG.json"

STATUS_PRIORITY = ["in_progress", "failed", "partial", "pending", "blocked", "done"]


def new_key(key: str) -> str:
    """`module:<rule>:<file>:<line>` → `module:<rule>:<file>`。

    注意 file 是相对路径（agents/agent.py），不含冒号；但为稳妥仍只剥掉
    最后一个纯数字段，且要求剥掉后还剩 ≥3 段。
    """
    parts = key.split(":")
    if parts and parts[0] == "module" and len(parts) >= 4 and parts[-1].isdigit():
        return ":".join(parts[:-1])
    return key


def merge_status(items: list[dict]) -> tuple[str, list[dict]]:
    """合并多条目状态，返回 (聚合状态, 仍未闭合的子项列表)。

    语义：done 表示"已判定/已修复"。合并后如果**还有**未闭合子项，
    就不能把整组标 done（否则会把待修项一起埋掉）；只有当组内全部 done 时才是 done。
    未闭合项保留在 openOccurrences 里，供后续精确定位与修复。
    """
    open_items = [
        {"fromId": item["id"], "status": item["status"],
         "file": (item.get("detail") or {}).get("file"),
         "line": (item.get("detail") or {}).get("line"),
         "note": item.get("note", "")}
        for item in items if item["status"] != "done"
    ]
    if not open_items:
        return "done", []
    for candidate in STATUS_PRIORITY:
        if candidate == "done":
            continue
        if any(i["status"] == candidate for i in items if i["status"] != "done"):
            return candidate, open_items
    return "pending", open_items


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    dry = "--dry-run" in sys.argv
    if not BACKLOG.is_file():
        print(json.dumps({"ok": False, "reason": "backlog-missing"}, ensure_ascii=False))
        return 1
    state = json.loads(BACKLOG.read_text(encoding="utf-8"))
    groups: dict[str, list[dict]] = {}
    for item in state["items"]:
        groups.setdefault(new_key(item["key"]), []).append(item)

    merged_items: list[dict] = []
    changed = 0
    for key, items in groups.items():
        if len(items) == 1 and items[0]["key"] == key:
            merged_items.append(items[0])
            continue
        changed += 1
        merged_status, open_items = merge_status(items)
        occurrences = []
        for item in items:
            detail = item.get("detail") or {}
            occurrences.append({
                "file": detail.get("file"),
                "line": detail.get("line"),
                "exceptLine": detail.get("exceptLine"),
                "fromId": item["id"],
                "status": item["status"],
            })
        first = sorted(items, key=lambda i: i["firstSeenRound"] or "")[0]
        last = sorted(items, key=lambda i: i["lastSeenRound"] or "")[-1]
        notes = [i.get("note", "") for i in items if i.get("note")]
        merged_items.append({
            "id": first["id"],
            "key": key,
            "source": first["source"],
            "severity": min((i["severity"] for i in items),
                            key=lambda s: {"high": 0, "medium": 1, "low": 2, "info": 3}.get(s, 9)),
            "title": first["title"].split(" @ ")[0] + " @ " + str((first.get("detail") or {}).get("file")),
            "detail": first.get("detail"),
            "occurrences": occurrences,
            "openOccurrences": open_items,
            "status": merged_status,
            "firstSeenRound": first.get("firstSeenRound"),
            "lastSeenRound": last.get("lastSeenRound"),
            "seenCount": sum(int(i.get("seenCount") or 1) for i in items),
            "attempts": sum(int(i.get("attempts") or 0) for i in items),
            "mergedFrom": [i["id"] for i in items],
            **({"note": "合并迁移：" + "；".join(notes)} if notes else {}),
        })

    # 重新编号，保持 id 稳定（沿用原首个 id，不新造）
    seen_ids = set()
    for item in merged_items:
        base = item["id"]
        if base in seen_ids:
            n = 1
            while f"{base}-m{n}" in seen_ids:
                n += 1
            item["id"] = f"{base}-m{n}"
        seen_ids.add(item["id"])

    state["items"] = merged_items
    state["events"].append({
        "at": state.get("updatedAt"),
        "kind": "backlog-migration",
        "type": "key-aggregation",
        "before": sum(len(v) for v in groups.values()),
        "after": len(merged_items),
        "groupsChanged": changed,
    })
    report = {
        "ok": True,
        "before": sum(len(v) for v in groups.values()),
        "after": len(merged_items),
        "groupsChanged": changed,
        "dryRun": dry,
    }
    if not dry:
        BACKLOG.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
