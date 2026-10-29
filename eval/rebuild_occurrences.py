#!/usr/bin/env python3
"""用代码指纹重建 backlog 里 module 类 finding 的 occurrence。

为什么需要：occurrence 只存行号，代码改动后行号失效 → finding 既定位不到、
也不会被扫描清空（文件里确实还有该规则命中）→ 永久挂 pending，闭环空转。

做法：
1. 读最新 `eval/runs/module-scan.json`（带 fingerprint）
2. 按 `module:<rule>:<file>` 聚合出**当前真实位置**
3. 对每个 backlog 项：
   - 当前仍有命中 → 用新位置重建 openOccurrences（含 fingerprint 与重定位后的行号）
   - 当前已无命中 → 清空 openOccurrences（该项已修）
4. 旧 occurrences 归档到 `staleOccurrences`（保留历史，不丢证据）

用法：python eval/rebuild_occurrences.py [--apply]
不加 --apply 只打印将要发生的变化（dry-run 是默认，防误改）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

BACKLOG = REPO / "eval" / "loop" / "BACKLOG.json"
SCAN = REPO / "eval" / "runs" / "module-scan.json"
from eval.module_scan import locate_by_fingerprint  # noqa: E402


def group_scan(findings: list[dict]) -> dict[str, list[dict]]:
    """按 module:<rule>:<file> 聚合当前扫描结果。"""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for finding in findings:
        key = f"module:{finding.get('rule')}:{finding.get('file')}"
        grouped[key].append(finding)
    return dict(grouped)


def rebuild(apply: bool) -> dict:
    scan = json.loads(SCAN.read_text(encoding="utf-8"))
    current = group_scan(scan.get("findings") or [])
    backlog = json.loads(BACKLOG.read_text(encoding="utf-8"))

    changed, emptied, untouched = [], [], []
    for item in backlog["items"]:
        key = item.get("key") or ""
        if not key.startswith("module:"):
            continue
        hits = current.get(key) or []
        old = item.get("openOccurrences") or []
        if not hits:
            if old:
                emptied.append({"id": item["id"], "key": key, "was": len(old)})
                item.setdefault("staleOccurrences", []).extend(old)
                item["openOccurrences"] = []
            continue
        # 重建：每条命中带指纹 + 重定位后的当前行号
        rebuilt = []
        for hit in hits:
            fingerprint = hit.get("fingerprint") or ""
            located = locate_by_fingerprint(REPO / str(hit.get("file", "")), fingerprint) \
                if fingerprint else 0
            rebuilt.append({
                "fromId": item["id"],
                "status": "pending",
                "file": hit.get("file"),
                "line": hit.get("line"),
                "exceptLine": hit.get("exceptLine"),
                "fingerprint": fingerprint,
                "locatedLine": located,
                "staleLocation": bool(fingerprint) and located == 0,
                "triage": "needs-review",
                "triageReason": "rebuilt-by-fingerprint",
            })
        # locatedLine/staleLocation 是派生值，必须每次按当前文件重算，
        # 否则代码移动后仍指向旧行（原实现只在指纹变化时写回，是真缺陷）。
        # 用**指纹**作 key：指纹稳定，line 会漂移（用它作 key 会永远匹配不上）
        old_lines = {(o.get("file"), o.get("fingerprint")): o.get("locatedLine")
                     for o in old}
        refreshed = [(n.get("file"), n.get("fingerprint"), n.get("locatedLine"))
                     for n in rebuilt]
        line_moved = any(
            (f, fp) in old_lines and old_lines[(f, fp)] != loc
            for f, fp, loc in refreshed)
        structural = (len(rebuilt) != len(old)
                      or any((o.get("fingerprint") or "") != (n.get("fingerprint") or "")
                             for o, n in zip(old, rebuilt)))
        if structural or line_moved:
            changed.append({"id": item["id"], "key": key,
                            "old": len(old), "new": len(rebuilt),
                            "lineMoved": line_moved})
            item["openOccurrences"] = rebuilt
        else:
            untouched.append(item["id"])

    stale = sum(1 for item in backlog["items"]
                for occ in (item.get("openOccurrences") or [])
                if occ.get("staleLocation"))

    result = {
        "ok": True, "applied": apply,
        "itemsChanged": len(changed), "itemsEmptied": len(emptied),
        "itemsUnchanged": len(untouched), "staleLocations": stale,
        "changed": changed[:15], "emptied": emptied[:15],
    }
    if apply:
        backup = BACKLOG.with_suffix(".json.bak-before-rebuild")
        shutil.copyfile(BACKLOG, backup)
        result["backup"] = str(backup)
        BACKLOG.write_text(json.dumps(backlog, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="真正写回（默认 dry-run，只报告变化）")
    args = parser.parse_args()
    result = rebuild(args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
