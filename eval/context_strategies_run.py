#!/usr/bin/env python3
"""W15 上下文策略对照：S1 无折叠 / S2 默认整段折叠 / S3 尾部保留 4 轮。

设计约束（如实说明）：
- **S4 阶段交接**需要 plan 文件驱动的 staged 会话，本 runner 未覆盖 → 报告中显式标注
  `s4: not-run`，不得用"S1-S3 已跑"暗示四策略全部验证。
- 同题单、同模型、同预算；策略差异只通过**环境变量**注入（`_apply_strategy`）。
- 每策略独立 HOME（互相污染会让折叠状态串味）。

判读纪律：单批次不下结论；产物交给 `eval/lifecycle_noise_gate.py` 做跨批次判读。

用法：
  python eval/context_strategies_run.py --limit 4 --out eval/runs/context/ab1.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
PRED_FULL = REPO / "eval" / "runs" / "gaia" / "predictions.jsonl"
MODEL = os.environ.get("EVOHARNESS_EVAL_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")

# 策略 → 环境变量（与 eval/context_strategies.py 的定义保持一致）
# 折叠阈值：默认 0.30。实测 GAIA 单题利用率仅约 5%，0.30 仍不触发 →
# 要验证"折叠发生时三策略是否不同"，需用 --threshold 压到更低（如 0.03）。
# 注意：低阈值是**机制验证**，不是生产参数对照，报告须写明。
DEFAULT_THRESHOLD = "0.30"
FORCE_FOLD = {"EVOHARNESS_AUTO_COMPACT_THRESHOLD": DEFAULT_THRESHOLD}


def build_strategies(threshold: str = DEFAULT_THRESHOLD) -> dict[str, dict]:
    force = {"EVOHARNESS_AUTO_COMPACT_THRESHOLD": threshold}
    return {
        "s1_no_fold": {"EVOHARNESS_DISABLE_AUTO_FOLD": "1"},
        "s2_current_fold": dict(force),
        "s3_tail_keep": {**force, "EVOHARNESS_FOLD_KEEP_RECENT_TURNS": "4"},
    }


STRATEGIES: dict[str, dict] = build_strategies()
NOT_RUN = {"s4_stage_handover": "需要 plan 文件驱动的 staged 会话，本 runner 未覆盖"}


def load_tasks(limit: int) -> list[dict]:
    rows = [json.loads(l) for l in PRED_FULL.read_text(encoding="utf-8").splitlines()]
    out = []
    for row in rows[:limit]:
        item = dict(row)
        item["Question"] = row.get("question") or ""
        item["Final answer"] = row.get("gold") or ""
        item["Level"] = row.get("level") or ""
        out.append(item)
    return out


def run_one(item: dict, strategy: str, env_extra: dict, workdir: Path, tag: str) -> dict:
    home = workdir / strategy / "home"
    cwd = workdir / strategy / "cwd"
    home.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    item_path = workdir / f"item-{tag}.json"
    out_path = workdir / f"out-{tag}.json"
    item_path.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")

    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "PYTHONUTF8": "1", "MODEL": MODEL,
           "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", ""),
           **env_extra}
    proc = subprocess.run(
        [sys.executable, "-m", "eval.evolution_experiment", "worker",
         "--item-json", str(item_path), "--dataset", "gaia",
         "--max-turns", "18", "--timeout", "420", "--out", str(out_path)],
        cwd=str(cwd), env=env, capture_output=True, timeout=900, check=False)
    record = {}
    if out_path.is_file():
        try:
            record = json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError:
            record = {}
    tokens = record.get("tokens") or {}
    return {
        "exitCode": proc.returncode,
        "folds": int(record.get("folds") or 0),
        "correct": bool(record.get("correct")),
        "hasFinalMarker": bool(record.get("has_final_marker")),
        "tokens": (tokens.get("input", 0) or 0) + (tokens.get("output", 0) or 0),
        "error": str(record.get("error") or "")[:160],
        "stderrTail": proc.stderr.decode("utf-8", "replace")[-160:] if proc.returncode else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--out", default="")
    parser.add_argument("--strategies", default=",".join(STRATEGIES))
    parser.add_argument("--threshold", default=DEFAULT_THRESHOLD,
                        help="强制折叠阈值；<0.05 属机制验证而非生产对照")
    args = parser.parse_args()

    tasks = load_tasks(args.limit)
    if not tasks:
        print(json.dumps({"ok": False, "reason": "no-tasks"}, ensure_ascii=False))
        return 2

    strategies_map = build_strategies(args.threshold)
    wanted = [s.strip() for s in args.strategies.split(",") if s.strip()]
    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for strategy in wanted:
            if strategy not in strategies_map:
                continue
            env_extra = strategies_map[strategy]
            rows = []
            for index, item in enumerate(tasks):
                rows.append(run_one(item, strategy, env_extra, workdir,
                                    f"{strategy}-{index}"))
            correct = sum(1 for r in rows if r["correct"])
            marker = sum(1 for r in rows if r["hasFinalMarker"])
            results[strategy] = {
                "env": env_extra,
                "tasks": len(rows),
                "correct": correct,
                "passRate": round(correct / len(rows) * 100, 1) if rows else 0.0,
                "finalMarkerRate": round(marker / len(rows) * 100, 1) if rows else 0.0,
                "tokens": sum(r["tokens"] for r in rows),
                "foldTotal": sum(r.get("folds", 0) for r in rows),
                "errors": [r["error"] for r in rows if r["error"]],
                "infraFailures": sum(1 for r in rows if r["exitCode"] != 0),
            }

    report = {
        "kind": "context-strategy-ab",
        "foldThreshold": args.threshold,
        "purpose": ("mechanism-check（低阈值确保折叠发生，非生产参数对照）"
                    if float(args.threshold) < 0.05 else "parameter-comparison"),
        "model": MODEL,
        "tasks": len(tasks),
        "strategies": results,
        "notRun": NOT_RUN,
        "claimability": {
            # 干预未生效（所有策略都没折叠）时，这批数据测的是噪声，不是策略差异
            "verdict": ("uninformative:intervention-not-triggered"
                        if not any(s.get("foldTotal") for s in results.values())
                        else "not-claimable:single-batch"),
            "note": ("单批次不下结论；需另一批同参数结果交给 "
                     "eval/lifecycle_noise_gate.py 判读。"
                     "另：S4 未跑，不得表述为'四策略全部验证'"),
        },
    }
    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "context" / "ab1.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "tasks": len(tasks), "strategies": list(results),
                      "notRun": list(NOT_RUN), "out": str(out)}, ensure_ascii=False, indent=2))
    for name, summary in results.items():
        print(f"  {name:18s} pass={summary['passRate']:5.1f}% "
              f"marker={summary['finalMarkerRate']:5.1f}% "
              f"tok={summary['tokens']:>8d} infra={summary['infraFailures']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
