#!/usr/bin/env python3
"""Harness² 真实对照跑（小样本）：同一批任务，跑基线与变体，量化 Δcorrect 与 Δtokens。

与 evolution_experiment 的区别：
  · 优化对象是 **harness 配置**（env），不是 skill 池；
  · 每个变体是一次独立 run，只改 env，不改代码；
  · 结果直接喂给 eval/roi.py 的判据（n 小 → not-claimable）。

用法（小样本先验证管线）：
  python eval/harness2_run.py --axis tool_policy --rounds 1 --tasks 1 --holdout 1
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
OUT = REPO / "eval" / "runs" / "harness2"


def run_variant(axis: str, variant: dict, *, rounds: int, tasks: int, holdout: int,
                workspace: str, split_manifest: str, task_ids_file: str = "") -> dict:
    env = dict(os.environ)
    env.update(variant.get("env") or {})
    env["PYTHONUTF8"] = "1"
    cmd = [
        sys.executable, "-m", "eval.evolution_experiment", "run",
        "--dataset", "gaia",
        "--rounds", str(rounds),
        "--tasks-per-round", str(tasks),
        "--holdout", str(holdout),
        "--arms", "no_evolution",   # 只测 harness 配置，不掺进化
        "--max-turns", "6",
        "--timeout", "300",
        "--split-manifest", split_manifest,
        "--workspace", workspace,
    ]
    if task_ids_file:
        # 只跑有区分度的题：全 0/全 1 的题池上做配置对照等于白烧 token（见 eval/holdout_calibrate.py）
        cmd.extend(["--task-ids-file", task_ids_file])
    started = time.time()
    proc = subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True, timeout=3600, check=False)
    return {
        "runId": f"{axis}-{variant['name']}",
        "variant": variant["name"],
        "env": variant.get("env") or {},
        "exitCode": proc.returncode,
        "durationSeconds": round(time.time() - started, 1),
        "stdoutTail": proc.stdout.decode("utf-8", "replace")[-1200:],
        "stderrTail": proc.stderr.decode("utf-8", "replace")[-600:],
    }


def read_curve() -> dict:
    path = REPO / "eval" / "runs" / "evolution" / "curve.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--axis", default="tool_policy")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--tasks", type=int, default=1)
    parser.add_argument("--holdout", type=int, default=1)
    parser.add_argument("--variants", default="")   # 逗号分隔；空=全部
    parser.add_argument("--task-ids-file", default="",
                        help="只用给定 task_id 子集（先用 eval/holdout_calibrate.py 筛有区分度的题）")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO / "eval"))
    import harness_squared

    spec = harness_squared.AXES[args.axis]
    wanted = [v.strip() for v in args.variants.split(",") if v.strip()]
    variants = [v for v in spec["variants"] if not wanted or v["name"] in wanted]

    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for variant in variants:
        # 每个变体独立 workspace，且 manifest 路径要绑定**本次规模**：
        # 冻结 manifest 的 append-only 保护会拒绝参数变更（holdout/tasks 不同即拒绝），
        # 复用同一路径会让第二次不同规模的实验直接 SplitManifestDrift 失败。
        tag = f"{args.axis}-{variant['name']}"
        scale_tag = f"r{args.rounds}t{args.tasks}h{args.holdout}"
        # manifest 必须同时绑定**题池子集**：只改规模但复用同一 manifest 时，
        # 若这次用了 --task-ids-file（题集变了），旧 manifest 里"存在但不在数据集"的题
        # 会触发 SplitManifestDrift: existing split task missing from dataset（实测踩到）。
        pool_tag = "-" + Path(args.task_ids_file).stem if args.task_ids_file else ""
        workspace = str(OUT / "ws" / f"{scale_tag}-{tag}{pool_tag}")
        manifest = str(OUT / "split" / f"{scale_tag}-{tag}{pool_tag}.json")
        Path(manifest).parent.mkdir(parents=True, exist_ok=True)
        print(f"[run] {tag} scale={scale_tag} env={variant.get('env')}", flush=True)
        record = run_variant(args.axis, variant, rounds=args.rounds, tasks=args.tasks,
                             holdout=args.holdout, workspace=workspace, split_manifest=manifest,
                             task_ids_file=args.task_ids_file)
        # 失败的 run 不得复用上一次成功的 curve：否则会把旧结果记成这次变体的结果。
        # 只有 exitCode==0 才读 curve；否则如实标为 run-failed。
        if record["exitCode"] == 0:
            curve = read_curve()
            points = [p for p in (curve.get("points") or []) if p.get("arm") == "no_evolution"]
            record["lastPoint"] = points[-1] if points else None
            record["totalTokensM"] = curve.get("total_tokens_M")
        else:
            record["lastPoint"] = None
            record["totalTokensM"] = None
            record["runFailed"] = True
        record["scaleTag"] = scale_tag + pool_tag
        results.append(record)

    report = {
        "kind": "harness2-run",
        "axis": args.axis,
        "scale": {"rounds": args.rounds, "tasks": args.tasks, "holdout": args.holdout},
        "results": results,
        "note": ("这是 harness 配置对照组：只改 env、不改代码；"
                 "每个变体独立 workspace/manifest，避免互相污染"),
        "claimRule": "n 小于 30 或不带 CI 时，任何 Δcorrect 都判 not-claimable（见 eval/roi.py）",
    }
    out = OUT / f"run-{args.axis}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(out), "runs": len(results)}, ensure_ascii=False, indent=2))
    for record in results:
        point = record.get("lastPoint") or {}
        print(f"  {record['runId']:28s} exit={record['exitCode']} "
              f"pass={point.get('pass_at_1')} tokM={record.get('totalTokensM')} "
              f"dur={record['durationSeconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
