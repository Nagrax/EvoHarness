#!/usr/bin/env python3
"""KPI 趋势与回归检测（eval-workflow.v1，设计 §4/§5）。

- 每轮把 collab-eval / feishu-eval 的量化指标追加进 kpi-history.jsonl（带 sha 口径）
- 对比最近两次同源记录：指标劣化（< 上次）→ medium finding；reopened > 0 → high
- 诚实边界：这里只有趋势一致性（B 级），不宣称统计显著性；样本不足时明确说不足

用法：python integration/eval/kpi_trend.py --record --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

INTEGRATION = Path(__file__).resolve().parents[1]
REPO = INTEGRATION.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "eval"))

from integration.config import ensure_dirs  # noqa: E402

HISTORY = INTEGRATION / "data" / "kpi-history.jsonl"
FEISHU_EVAL = REPO / "feishu-daily-agent" / "eval" / "runs" / "feishu-eval.json"
COLLAB_EVAL = INTEGRATION / "data" / "collab-eval.json"
BACKLOG = REPO / "eval" / "loop" / "BACKLOG.json"

# 指标名 → 劣化即报警的方向（1=越高越好）。新指标在此登记才算数。
TRACKED = {
    "collab": {"overallRate": 1, "safetyFailed": -1, "severe": -1},
    "feishu": {"autoPrecision": 1, "autoCoverage": 1, "draftRecall": 1},
}


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def record() -> list[dict]:
    """采集当轮指标并追加历史。返回本次写入的记录。"""
    ensure_dirs()
    entries: list[dict] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    if COLLAB_EVAL.is_file():
        data = json.loads(COLLAB_EVAL.read_text(encoding="utf-8"))
        entries.append({
            "at": now, "source": "collab",
            "metrics": {"overallRate": data.get("overallRate"),
                        "safetyFailed": int(bool(data.get("safetyFailed"))),
                        "severe": int(data.get("severe") or 0)},
            "artifactSha": _sha(COLLAB_EVAL),
        })
    if FEISHU_EVAL.is_file():
        data = json.loads(FEISHU_EVAL.read_text(encoding="utf-8"))
        approved = (data.get("strategies") or {}).get("approved") or {}
        entries.append({
            "at": now, "source": "feishu",
            "metrics": {"autoPrecision": approved.get("autoPrecision"),
                        "autoCoverage": approved.get("autoCoverage"),
                        "draftRecall": approved.get("draftRecall")},
            "artifactSha": _sha(FEISHU_EVAL),
        })

    reopened = 0
    if BACKLOG.is_file():
        state = json.loads(BACKLOG.read_text(encoding="utf-8"))
        for item in state.get("items") or []:
            if item.get("status") == "pending" and int(item.get("seenCount") or 0) > 1 \
                    and item.get("closedReason") in (None, "", "no-open-occurrence") \
                    and item.get("reopenCount"):
                reopened = max(reopened, int(item["reopenCount"]))
    if entries:
        entries[-1]["reopened"] = reopened
        with HISTORY.open("a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entries


def _history_by_source() -> dict[str, list[dict]]:
    if not HISTORY.is_file():
        return {}
    by_source: dict[str, list[dict]] = {}
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        by_source.setdefault(str(entry.get("source")), []).append(entry)
    return by_source


def check() -> dict:
    """对比最近两次同源记录，产出 findings。"""
    findings: list[dict] = []
    by_source = _history_by_source()
    for source, direction in TRACKED.items():
        rows = by_source.get(source) or []
        if len(rows) < 2:
            continue  # 样本不足：明确不宣称，不是"干净"
        prev, last = rows[-2], rows[-1]
        for metric, orient in direction.items():
            p, l = prev.get("metrics", {}).get(metric), last.get("metrics", {}).get(metric)
            if p is None or l is None:
                continue
            degraded = (l < p) if orient == 1 else (l > p)
            if degraded:
                findings.append({
                    "source": "kpiTrend",
                    "key": f"kpi-trend:{source}.{metric}",
                    "severity": "medium",
                    "title": f"KPI 劣化：{source}.{metric} {p} → {l}",
                    "detail": {"prev": p, "last": l, "at": last.get("at"),
                               "note": "趋势一致性信号（B 级），先复测再定位"},
                })
        reopened = int(last.get("reopened") or 0)
        if reopened > 0:
            findings.append({
                "source": "kpiTrend",
                "key": "kpi-trend:reopened",
                "severity": "high",
                "title": f"已闭合项复现 {reopened} 次（回归检测）",
                "detail": {"reopened": reopened, "at": last.get("at")},
            })
    return {"ok": not findings, "checked": list(by_source.keys()), "findings": findings}


def trend_summary() -> dict:
    """闭环报告用：最近指标 + 记录轮数。"""
    by_source = _history_by_source()
    out: dict[str, dict] = {}
    for source, rows in by_source.items():
        last = rows[-1]
        out[source] = {"records": len(rows), "at": last.get("at"),
                       "metrics": last.get("metrics", {})}
    return out


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.record:
        print(json.dumps({"recorded": record()}, ensure_ascii=False, indent=2))
    if args.check or not args.record:
        print(json.dumps(check(), ensure_ascii=False, indent=2))
    result = check()
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
