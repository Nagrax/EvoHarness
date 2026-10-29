#!/usr/bin/env python3
"""Skill 遥测 CLI —— 给 `/api/eval/skills` 补一个非 HTTP 入口。

背景（2026-10-29 全栈对应分析）：`/api/eval/skills` 是**唯一一个既无前端入口
也无 CLI 的端点**，用户只能 curl。它与 `/api/skills` 高度重叠（后者是超集），
保留它是因为"公开演示环境无产物时可回退 mock 数据"。

本 CLI **直接读产物文件**（不经 HTTP），所以：
- 本地排查/AI Agent 自检不需要起服务
- 输出的字段与端点保持一致（同一数据源），不会漂移

用法：
  python eval/eval_skills_cli.py            # 表格
  python eval/eval_skills_cli.py --json     # 机器可读
  python eval/eval_skills_cli.py --status healthy
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
EVOLUTION_DIR = REPO / ".evoharness" / "skill-evolution"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def collect() -> dict:
    report = _read_json(EVOLUTION_DIR / "online_eval_report.json", {}) or {}
    usage_stats = _read_json(EVOLUTION_DIR / "skill_usage_stats.json", {}) or {}
    rows = []
    for item in report.get("skills") or []:
        name = str(item.get("skill") or "")
        if not name:
            continue
        usage = usage_stats.get(name, {}) if isinstance(usage_stats, dict) else {}
        rows.append({
            "skill": name,
            "status": str(item.get("status") or ""),
            "retrieved": int(item.get("retrieved", 0) or 0),
            "relevant": int(item.get("relevant", 0) or 0),
            "used": int(item.get("used", 0) or 0),
            "used_rate": float(item.get("used_rate", 0.0) or 0.0),
            "relevance_rate": float(item.get("relevance_rate", 0.0) or 0.0),
            "last_action": str(item.get("last_action") or ""),
            "last_time": str(item.get("last_time") or ""),
            "source": str(usage.get("source") or ""),
        })
    return {
        "ok": True,
        "hasData": bool(rows),
        "source": str(EVOLUTION_DIR / "online_eval_report.json"),
        "count": len(rows),
        "skills": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Skill 遥测（读 online_eval_report 产物）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--status", default="", help="按状态过滤，如 healthy/watch/incubating")
    args = parser.parse_args()

    data = collect()
    rows = data["skills"]
    if args.status:
        rows = [r for r in rows if r["status"] == args.status]
        data = {**data, "skills": rows, "count": len(rows), "filteredBy": args.status}

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print(f"没有遥测数据（{data['source']} 不存在或为空）")
        return 0
    print(f"{'skill':32s}{'status':12s}{'retr':>6s}{'use%':>7s}{'rel%':>7s}  last")
    for row in rows:
        print(f"{row['skill'][:31]:32s}{row['status'][:11]:12s}"
              f"{row['retrieved']:>6d}{row['used_rate'] * 100:>6.0f}%"
              f"{row['relevance_rate'] * 100:>6.0f}%  {row['last_action'][:20]}")
    print(f"\n共 {len(rows)} 个 skill（数据源：{data['source']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())