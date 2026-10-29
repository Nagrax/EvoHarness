#!/usr/bin/env python3
"""闭环无人值守一轮：collect → triage → 摘要，可挂定时任务。

用户目标 3 要求"可持续自动续跑，不等用户回复"。之前 `loop.py` 只有分离的子命令
（collect / triage / next），没有一个"跑完整一轮并落摘要"的入口——
定时任务要串三四个命令，中间任何一步失败都不会被发现。

本脚本 = **一个可调度的原子步骤**：
1. `collect`（12 个信号源）
2. `triage --apply`（合理防御自动闭合）
3. 汇总：本轮新增/闭合/仍待办 + 各信号源状态
4. 落盘 `eval/runs/loop-autorun-<round>.json`（可追溯）
5. 退出码反映状态：0=正常；3=有新的 high 待办（供告警）；2=某信号源失败

用法（定时任务）：
  python eval/loop_autorun.py --once
  python eval/loop_autorun.py --once --skip-triage
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
RUNS = REPO / "eval" / "runs"


def _run(args: list[str], timeout: int) -> dict:
    started = time.time()
    proc = subprocess.run([sys.executable, *args], cwd=str(REPO),
                          capture_output=True, timeout=timeout, check=False)
    return {
        "cmd": " ".join(args),
        "exitCode": proc.returncode,
        "seconds": round(time.time() - started, 1),
        "stdoutTail": proc.stdout.decode("utf-8", "replace")[-600:],
        "stderrTail": proc.stderr.decode("utf-8", "replace")[-400:],
    }


def summarize_backlog() -> dict:
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return {"ok": False, "reason": "backlog-missing"}
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") or []
    open_items = [i for i in items if i.get("status") != "done"]
    by_sev: dict[str, int] = {}
    for item in open_items:
        sev = str(item.get("severity") or "?")
        by_sev[sev] = by_sev.get(sev, 0) + 1
    return {
        "ok": True,
        "total": len(items),
        "open": len(open_items),
        "openBySeverity": by_sev,
        "rounds": len(data.get("rounds") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="跑一轮（默认行为）")
    parser.add_argument("--skip-triage", action="store_true")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    steps = []

    # ① 审计器自检（先确认"检查器还工作"，再看它说什么）
    selfcheck = _run(["eval/auditor_selfcheck.py"], 300)
    steps.append({"name": "auditorSelfcheck", **selfcheck})

    collect = _run(["eval/loop.py", "collect"], args.timeout)
    steps.append({"name": "collect", **collect})

    triage: dict | None = None
    if not args.skip_triage:
        triage = _run(["eval/loop.py", "triage", "--apply"], 300)
        steps.append({"name": "triage", **triage})

    backlog = summarize_backlog()
    failed = [s for s in steps if s["exitCode"] != 0]

    report = {
        "ok": not failed,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps": steps,
        "backlog": backlog,
        "failedSteps": [s["name"] for s in failed],
    }
    round_tag = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = RUNS / f"loop-autorun-{round_tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "ok": report["ok"],
        "collectExit": collect["exitCode"],
        "triageExit": (triage or {}).get("exitCode"),
        "backlog": backlog,
        "failedSteps": report["failedSteps"],
        "report": str(out),
    }, ensure_ascii=False, indent=2))

    if failed:
        return 2
    high_open = int((backlog.get("openBySeverity") or {}).get("high", 0))
    return 3 if high_open > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())