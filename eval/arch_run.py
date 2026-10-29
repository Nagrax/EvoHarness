#!/usr/bin/env python3
"""架构对比实跑：三种 harness 设计在同一批题上的成本-收益对照。

只改 env（不改代码），每种设计独立 workspace + manifest；
结果喂给 eval/roi.py 的判据（n<30 一律 not-claimable）。

用法：
  python eval/arch_run.py --rounds 2 --tasks 1 --holdout 2 \
      --task-ids-file eval/runs/always-pass-ids.json --designs heavy-wrapped,plugin
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "eval" / "runs" / "arch"


def run_design(key: str, design: dict, *, rounds: int, tasks: int, holdout: int,
               task_ids_file: str, repeats: int = 1) -> list[dict]:
    """跑一个设计 repeats 次，返回全部结果（方差分析需要同配置多份样本）。

    背景：heavy-wrapped 基线同配置重复两次，pass@1 极差 50pp（eval/runs/arch/variance.json）。
    单次运行的"配置差异"完全可能落在噪声里。要得到可信结论，必须：
      · 同配置重复 repeats 次；
      · 报告均值与极差，而不是单次值；
      · n*repeats 仍 <30 时一律 not-claimable。
    """
    env_base = dict(os.environ)
    env_base.update(design.get("env") or {})
    env_base["PYTHONUTF8"] = "1"
    scale = f"r{rounds}t{tasks}h{holdout}"
    pool = "-" + Path(task_ids_file).stem if task_ids_file else ""
    records: list[dict] = []
    for repeat in range(repeats):
        repeat_tag = f"{scale}-{key}{pool}-rep{repeat + 1}" if repeats > 1 else f"{scale}-{key}{pool}"
        workspace = str(OUT / "ws" / repeat_tag)
        manifest = str(OUT / "split" / f"{repeat_tag}.json")
        Path(manifest).parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable, "-m", "eval.evolution_experiment", "run",
            "--dataset", "gaia",
            "--rounds", str(rounds), "--tasks-per-round", str(tasks), "--holdout", str(holdout),
            "--arms", "no_evolution",
            "--max-turns", "6", "--timeout", "300",
            "--split-manifest", manifest, "--workspace", workspace,
        ]
        if task_ids_file:
            cmd.extend(["--task-ids-file", task_ids_file])
        print(f"[run] {key} repeat={repeat + 1}/{repeats} env={design.get('env')}", flush=True)
        started = time.time()
        proc = subprocess.run(cmd, cwd=str(REPO), env=env_base, capture_output=True,
                              timeout=5400, check=False)
        record = {
            "design": key,
            "repeat": repeat + 1,
            "env": design.get("env") or {},
            "exitCode": proc.returncode,
            "durationSeconds": round(time.time() - started, 1),
            "stderrTail": proc.stderr.decode("utf-8", "replace")[-800:],
        }
        # 只在成功时读 curve，避免把上一轮结果记成这次的成绩
        if proc.returncode == 0:
            curve_path = REPO / "eval" / "runs" / "evolution" / "curve.json"
            if curve_path.is_file():
                curve = json.loads(curve_path.read_text(encoding="utf-8"))
                points = [p for p in (curve.get("points") or []) if p.get("arm") == "no_evolution"]
                record["lastPoint"] = points[-1] if points else None
                record["totalTokensM"] = curve.get("total_tokens_M")
                record["sideQueryTokensM"] = curve.get("side_query_tokens_M")
            else:
                record["lastPoint"] = None
        else:
            record["lastPoint"] = None
            record["totalTokensM"] = None
            record["runFailed"] = True
        records.append(record)
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--tasks", type=int, default=1)
    parser.add_argument("--holdout", type=int, default=2)
    parser.add_argument("--task-ids-file", default="")
    parser.add_argument("--designs", default="")   # 逗号分隔；空=全部
    parser.add_argument("--repeats", type=int, default=3,
                        help="每个设计重复次数；>=2 才能估方差，结论按均值±极差报告")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO / "eval"))
    import arch_compare

    wanted = [d.strip() for d in args.designs.split(",") if d.strip()]
    designs = {k: v for k, v in arch_compare.DESIGNS.items() if not wanted or k in wanted}

    support = arch_compare.probe_env_support()
    blocked = [e for e, ok in support.items() if not ok]
    if blocked:
        print(json.dumps({"ok": False, "reason": "paper-parameters", "blocked": blocked},
                         ensure_ascii=False, indent=2))
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    per_design: dict[str, list[dict]] = {}
    for key, design in designs.items():
        per_design[key] = run_design(key, design, rounds=args.rounds, tasks=args.tasks,
                                     holdout=args.holdout, task_ids_file=args.task_ids_file,
                                     repeats=max(1, args.repeats))
    results = [r for records in per_design.values() for r in records]

    # 每设计聚合：均值 ± 极差（方差可见，单次值不再直接当结论）
    import statistics

    def summarize(records: list[dict]) -> dict | None:
        points = [r.get("lastPoint") or {} for r in records if r.get("lastPoint")]
        passes = [float(p.get("pass_at_1") or 0) for p in points]
        tokens = [float(r.get("totalTokensM") or 0) for r in records if r.get("totalTokensM")]
        if not passes:
            return None
        return {
            "runs": len(records),
            "runsScored": len(passes),
            "passMean": round(statistics.fmean(passes), 1),
            "passMin": min(passes),
            "passMax": max(passes),
            "passRange": round(max(passes) - min(passes), 1),
            "tokensMeanM": round(statistics.fmean(tokens), 3),
            "sideQueryTokensM": next((r.get("sideQueryTokensM") for r in records
                                      if r.get("sideQueryTokensM") is not None), None),
        }

    summaries = {key: summarize(records) for key, records in per_design.items()}
    baseline = summaries.get("heavy-wrapped")
    comparisons = []
    if baseline and baseline.get("passMean") is not None:
        base_pass = float(baseline["passMean"])
        base_tok = float(baseline.get("tokensMeanM") or 0)
        # 噪声门必须同时覆盖两类噪声（真实踩坑）：
        #   批内极差：本批同设计各 repeat 之间的波动；
        #   跨批次极差：历史归档里同设计同规模的波动（heavy-wrapped 实测 [0,0,0,50]，
        #   本批内极差为 0 但跨批次 50pp——只看批内会把跨批次噪声漏掉）。
        in_batch_noise = max((float(s.get("passRange") or 0) for s in summaries.values()
                              if s and s.get("passRange") is not None), default=0.0)
        cross_batch_noise = 0.0
        variance_path = REPO / "eval" / "runs" / "arch" / "variance.json"
        if variance_path.is_file():
            try:
                variance = json.loads(variance_path.read_text(encoding="utf-8"))
                cross_batch_noise = float(variance.get("maxObservedRangePts") or 0)
            except ValueError:
                pass
        noise = max(in_batch_noise, cross_batch_noise)
        n_total = args.holdout * max(1, args.repeats)
        for key, summary in summaries.items():
            if key == "heavy-wrapped" or not summary or summary.get("passMean") is None:
                continue
            d_pass = float(summary["passMean"]) - base_pass
            d_tok = float(summary.get("tokensMeanM") or 0) - base_tok
            if d_pass < 0:
                verdict = ("negative:quality-loss-outweighs-savings" if d_tok < 0
                           else "negative:quality-loss-and-more-cost")
            elif baseline["passMean"] == 0 and float(summary["passMean"]) == 0:
                verdict = "uninformative:both-zero-baseline"
            elif d_pass > 0 and d_tok <= 0:
                verdict = "positive:better-and-cheaper"
            elif d_pass > 0 and d_tok > 0:
                verdict = "tradeoff:paid-more-got-more"
            elif d_pass == 0 and d_tok < 0:
                verdict = "positive:same-quality-cheaper"
            else:
                verdict = "neutral"
            # 双重门：样本不足、或差异小于同配置噪声极差 → 一律不可宣称
            if n_total < 30 and verdict.startswith(("positive", "tradeoff")):
                verdict += ":but-not-claimable-n<30"
            if verdict.startswith(("positive", "tradeoff")) and abs(d_pass) <= noise:
                verdict += ":delta-within-config-noise"
            comparisons.append({
                "design": key,
                "passMean": summary["passMean"], "passRange": summary["passRange"],
                "tokensMeanM": summary.get("tokensMeanM"),
                "deltaPassMean": round(d_pass, 2), "deltaTokensMeanM": round(d_tok, 3),
                "verdict": verdict,
            })

    report = {
        "kind": "arch-compare-run",
        "scale": {"rounds": args.rounds, "tasks": args.tasks, "holdout": args.holdout,
                  "taskIdsFile": args.task_ids_file or None, "repeats": args.repeats,
                  "effectiveNPerDesign": args.holdout * max(1, args.repeats)},
        "designs": list(designs),
        "results": results,
        "summaries": summaries,
        "comparisonsVsHeavyWrapped": comparisons,
        "noiseGate": {"inBatchNoisePP": in_batch_noise,
                      "crossBatchNoisePP": cross_batch_noise,
                      "effectiveNoisePP": noise,
                      "rule": ("差异绝对值 <= max(批内极差, 跨批次极差) 时不可宣称；"
                               "跨批次噪声来自 history 归档（eval/variance_check.py）")},
        "claimRule": ("n*repeats<30 时正向结论一律不可宣称；"
                      "差异 <= 同配置噪声极差时同样不可宣称"),
        "note": "只改 env 不改代码；每设计重复 repeats 次、独立 workspace+manifest",
    }
    out = OUT / "run-arch.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hist = OUT / "history"
    hist.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    (hist / f"run-{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(out), "runs": len(results),
                      "archived": str(hist / f"run-{stamp}.json")}, ensure_ascii=False, indent=2))
    for row in comparisons:
        print(f"  {row['design']:20s} passMean={row['passMean']} range={row['passRange']} "
              f"tokMeanM={row['tokensMeanM']} dPass={row['deltaPassMean']:+} {row['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
