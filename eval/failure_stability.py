#!/usr/bin/env python3
"""失败稳定性重测：同一批失败题同配置重复跑，看失败是否复现。

为什么必须先做这个：上一轮发现 W18 里 12 题全部 no-final-marker 的题，重跑竟然
3/3 都给出标记——**失败本身可能是随机的**。针对不稳定的失败沉淀"教训"，
本质是在拟合噪声。只有稳定复现的失败模式才值得反思与修复。

判定（对每道题，2026-10-28 升级为失败率连续值）：
  failRate = 无最终标记的遍数 / 总遍数
  stable      : failRate >= 0.8（容忍 1 次抖动）→ 能力缺陷，值得沉淀教训
  flaky       : 0 < failRate < 0.8            → 随机波动，不得据此改策略
  disappeared : failRate == 0.0               → 原失败未复现，原观测作废
  confidence  : repeats >= 3 才是 high——实测 2 遍判出的 stable 约 25% 在后续批次不复现

用法：python eval/failure_stability.py [--limit 5] [--repeats 3] [--out PATH]
退出码：0 完成；2 无数据。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
MODEL = os.environ.get("EVOHARNESS_EVAL_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")
PRED = REPO / "eval" / "runs" / "gaia-w18subset" / "predictions.jsonl"
PRED_FULL = REPO / "eval" / "runs" / "gaia" / "predictions.jsonl"

# 判定阈值（2026-10-28 升级，依据实测）：
# 旧版用"复现率 == 1.0 / == 0.0"的二分类，2 遍重复时把 2/2 判 stable；
# 但实测 8 道"2/2 stable"的题再跑一批后有 25% 给出标记 → 2 遍不够。
# 现在：① 用失败率连续值；② stable 容忍 1 次抖动（>=0.8）；
#       ③ 显式标注置信度，<3 遍一律 low。
STABLE_MIN_FAIL_RATE = 0.8
MIN_REPEATS_FOR_CONFIDENCE = 3


def classify_failure(fail_rate: float) -> str:
    """按失败率分类：stable（够高的持续失败）/ flaky（中间带）/ disappeared（未复现）。"""
    if fail_rate <= 0.0:
        return "disappeared"
    if fail_rate >= STABLE_MIN_FAIL_RATE:
        return "stable"
    return "flaky"


def load_candidates(limit: int, source: str = "full") -> list[dict]:
    """取失败题（转成 worker 要的原始字段）。

    source: w18subset（题少，5-7 道）| full（全量 165 题里的失败题，够 10+ 道）
    """
    from eval.run_benchmark import classify_record

    path = PRED_FULL if source == "full" else PRED
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    picked = []
    for row in rows:
        row["dataset"] = "gaia"
        if row.get("timed_out"):
            continue
        if not row.get("has_final_marker"):
            picked.append(("no-final-marker", row))
            continue
        try:
            if classify_record(row, "gaia") != "correct":
                picked.append(("wrong-answer", row))
        except Exception:  # noqa: BLE001
            continue
    # no-final-marker 优先（反思教训针对的失败类型）
    picked.sort(key=lambda pair: 0 if pair[0] == "no-final-marker" else 1)
    out = []
    for kind, row in picked[:limit]:
        item = dict(row)
        item["Question"] = row.get("question") or ""
        item["Final answer"] = row.get("gold") or ""
        item["Level"] = row.get("level") or ""
        item["_failureKind"] = kind
        out.append(item)
    return out


def run_once(item: dict, workdir: Path, tag: str) -> dict:
    home = workdir / tag / "home"
    cwd = workdir / tag / "cwd"
    home.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    item_path = workdir / f"item-{tag}.json"
    out_path = workdir / f"out-{tag}.json"
    item_path.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONUTF8": "1",
           "MODEL": MODEL, "EVOHARNESS_MODEL": MODEL,
           "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
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
        "hasFinalMarker": bool(record.get("has_final_marker")),
        "tokens": (tokens.get("input", 0) or 0) + (tokens.get("output", 0) or 0),
        "error": str(record.get("error") or "")[:160],
        "stderrTail": proc.stderr.decode("utf-8", "replace")[-200:] if proc.returncode else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--source", default="full",
                        choices=("full", "w18subset"))
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    candidates = load_candidates(args.limit, args.source)
    if not candidates:
        print(json.dumps({"ok": False, "reason": "no-candidates"}, ensure_ascii=False))
        return 2

    per_task = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for index, item in enumerate(candidates):
            runs = []
            for rep in range(args.repeats):
                runs.append(run_once(item, workdir, f"t{index}-r{rep}"))
            no_marker = sum(1 for r in runs if not r["hasFinalMarker"])
            runs_n = len(runs) or 1
            fail_rate = no_marker / runs_n
            status = classify_failure(fail_rate)
            per_task.append({
                "taskId": item.get("task_id"),
                "questionHead": str(item.get("Question"))[:60],
                "repeats": len(runs),
                "noMarkerCount": no_marker,
                "failRate": round(fail_rate, 3),
                "status": status,
                "confidence": ("high" if len(runs) >= MIN_REPEATS_FOR_CONFIDENCE else "low"),
                "tokens": sum(r["tokens"] for r in runs),
                "errors": [r["error"] for r in runs if r["error"]],
                "infraFailures": sum(1 for r in runs if r["exitCode"] != 0),
            })

    counts = {"stable": 0, "flaky": 0, "disappeared": 0}
    for task in per_task:
        counts[task["status"]] += 1
    rates = sorted(t["failRate"] for t in per_task)
    report = {
        "kind": "failure-stability",
        "model": MODEL,
        "source": args.source,
        "repeats": args.repeats,
        "tasks": len(per_task),
        "statusCounts": counts,
        "failRateDistribution": {
            "min": rates[0] if rates else None,
            "median": rates[len(rates) // 2] if rates else None,
            "max": rates[-1] if rates else None,
        },
        "confidence": ("high" if args.repeats >= MIN_REPEATS_FOR_CONFIDENCE else "low"),
        "thresholds": {"stableMinFailRate": STABLE_MIN_FAIL_RATE,
                       "minRepeatsForConfidence": MIN_REPEATS_FOR_CONFIDENCE},
        "tokens": sum(t["tokens"] for t in per_task),
        "conclusion": ("只有 stable 类失败值得沉淀教训；flaky 不得据此改策略；"
                       "disappeared 说明原观测作废。"
                       f"repeats<{MIN_REPEATS_FOR_CONFIDENCE} 时判定置信度为 low——"
                       "实测 2 遍判出的 stable 有约 25% 在后续批次不复现"),
        "perTask": per_task,
    }
    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "failure-stability.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({k: report[k] for k in
                          ("model", "source", "repeats", "tasks", "statusCounts",
                           "failRateDistribution", "confidence", "tokens", "conclusion")},
                         ensure_ascii=False, indent=2))
        for task in per_task:
            print(f"  {str(task['taskId'])[:8]} {task['status']:12s} "
                  f"noMarker={task['noMarkerCount']}/{task['repeats']} "
                  f"tokens={task['tokens']} infra={task['infraFailures']}")
        print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
