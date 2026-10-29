#!/usr/bin/env python3
"""批次读取工具：按批次标识**严格**取结果，避免混批。

为什么需要它（真实事故）：2026-10-29 W14 前后两批写同名 checkpoint 互相覆盖，
读侧只能靠 mtime 猜，差点用混合数据得出错误结论。写侧已改为带 tag（下批生效），
但**历史批次仍是旧命名**，所以读侧必须同时支持两种口径：

  新命名  checkpoint-<tag>-<arm>-r<n>.json     → 按文件名精确匹配
  旧命名  checkpoint-<arm>-r<n>.json           → 必须给 --since（mtime 过滤）

用法：
  python eval/batch_results.py list
  python eval/batch_results.py show --tag ws-w14c
  python eval/batch_results.py show --since "2026-10-29T01:30"     # 旧命名批次
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "eval" / "runs" / "evolution"


def list_batches() -> dict:
    """列出可识别的批次：新命名的 sidecar + 旧命名的 curve 时间。"""
    sidecars = sorted(RUNS.glob("batch-*.json"))
    batches = []
    for path in sidecars:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        batches.append({"tag": data.get("batchTag") or path.stem[6:],
                        "naming": "tagged",
                        "workspace": data.get("workspace"),
                        "arms": data.get("arms"),
                        "startedAt": data.get("startedAt"),
                        "file": path.name})
    legacy = sorted(RUNS.glob("checkpoint-*-r*.json"))
    legacy_files = [p for p in legacy if p.name.count("-") == 2]  # checkpoint-<arm>-r<n>
    if legacy_files:
        batches.append({"tag": "(legacy-untagged)", "naming": "legacy",
                        "count": len(legacy_files),
                        "hint": "旧命名无法分辨归属，必须用 --since 按 mtime 过滤"})
    return {"batches": batches, "tagged": len(sidecars), "legacyFiles": len(legacy_files)}


def load_batch(tag: str | None = None, since: str | None = None,
               until: str | None = None) -> dict:
    """按 tag（新命名）或 since（旧命名，mtime 过滤）读取一批 checkpoint。"""
    if not tag and not since:
        return {"ok": False, "reason": "need-tag-or-since"}

    if tag:
        files = sorted(RUNS.glob(f"checkpoint-{tag}-*-r*.json"))
        if not files:
            return {"ok": False, "reason": f"no-checkpoints-for-tag:{tag}"}
    else:
        cutoff = dt.datetime.fromisoformat(since).timestamp()
        upper = dt.datetime.fromisoformat(until).timestamp() if until else None
        files = sorted(
            p for p in RUNS.glob("checkpoint-*-r*.json")
            if p.name.count("-") == 2
            and p.stat().st_mtime >= cutoff
            and (upper is None or p.stat().st_mtime < upper))
        if not files:
            return {"ok": False, "reason": f"no-checkpoints-since:{since}"}

    by_arm: dict[str, list[dict]] = {}
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        arm = str(data.get("arm") or "")
        flags = data.get("correct_flags") or []
        by_arm.setdefault(arm, []).append({
            "round": data.get("round"),
            "hits": sum(1 for f in flags if f),
            "total": len(flags),
            "newSkills": data.get("new_skills"),
            "evolutionFired": data.get("evolution_fired"),
            "tokensM": round(float(data.get("stream_tokens_M") or 0)
                             + float(data.get("holdout_tokens_M") or 0), 2),
            "sideQueryM": round(float(data.get("side_query_tokens_M") or 0), 4),
            "file": path.name,
            "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        })

    summary = {}
    for arm, rows in by_arm.items():
        hits = sum(r["hits"] for r in rows)
        total = sum(r["total"] for r in rows)
        summary[arm] = {
            "rounds": len(rows),
            "hits": hits,
            "total": total,
            "passRate": round(hits / total * 100, 1) if total else 0.0,
            "tokensM": round(sum(r["tokensM"] for r in rows), 2),
            "sideQueryM": round(sum(r.get("sideQueryM") or 0 for r in rows), 4),
            # 进化自身开销占该臂总消耗的比例（判读 ROI 的关键分母）
            "evolutionOverheadPct": (
                round(sum(r.get("sideQueryM") or 0 for r in rows)
                      / sum(r["tokensM"] for r in rows) * 100, 2)
                if sum(r["tokensM"] for r in rows) else 0.0),
            "newSkillsFinal": rows[-1]["newSkills"] if rows else None,
        }
    return {"ok": True, "tag": tag, "since": since, "until": until, "files": len(files),
            "arms": summary, "detail": by_arm}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("--tag", default="")
    show.add_argument("--since", default="")
    show.add_argument("--until", default="")
    args = parser.parse_args()

    if args.cmd == "list":
        print(json.dumps(list_batches(), ensure_ascii=False, indent=2))
        return 0
    result = load_batch(args.tag or None, args.since or None, args.until or None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
