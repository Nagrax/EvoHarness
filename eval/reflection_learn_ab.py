#!/usr/bin/env python3
"""反思学习 A/B：**教训是否让 agent 答对更多题**（可归因设计）。

设计（对齐 Reflexion 精神，但去掉不可归因的噪声）：
- 臂 A（无教训）：6 个标准任务，直接跑 → 基线 correct 率
- 臂 B（有教训）：同样 6 个任务，但 prompt 前缀注入一条**从失败中提炼的教训**

教训的来源（不是编的）：
先用"诱导任务"制造可归因的失败——提示要求 `ANSWER=<num>`，而判分只认
`FINAL: <num>` → 必然失败；然后用 `agents/reflection.py` 的 `derive_lessons`
从这批失败里提炼教训文本，作为臂 B 的注入内容。

**为什么用 prompt 注入而不是走 memory**：本轮要验的是"教训本身有没有用"
（干净归因）。先确认有效，再谈"通过记忆召回是否也能生效"——
否则两个变量（教训有效性 + 召回可靠性）混在一起，结论又一次无法归因。

用法：
  python eval/reflection_learn_ab.py --arm baseline
  python eval/reflection_learn_ab.py --arm lesson --lesson-file <path>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
TASKS = REPO / "eval" / "runs" / "reflection-tasks" / "tasks.jsonl"
MODEL = os.environ.get("EVOHARNESS_EVAL_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")


def load_tasks(path: Path | None = None) -> list[dict]:
    src = path or TASKS
    return [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]


def run_one(item: dict, workdir: Path, tag: str, lesson: str = "",
            max_turns: int = 6) -> dict:
    item_path = workdir / f"item-{tag}.json"
    out_path = workdir / f"out-{tag}.json"
    payload = dict(item)
    if lesson:
        payload["Question"] = f"{lesson}\n\n{payload['Question']}"
    item_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    cwd = workdir / f"cwd-{tag}"
    cwd.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUTF8": "1", "MODEL": MODEL,
           "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.run(
        [sys.executable, "-m", "eval.evolution_experiment", "worker",
         "--item-json", str(item_path), "--dataset", "gaia",
         "--max-turns", str(max_turns), "--timeout", "300", "--out", str(out_path)],
        cwd=str(cwd), env=env, capture_output=True, timeout=600, check=False)
    record = {}
    if out_path.is_file():
        try:
            record = json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError:
            record = {}
    tokens = record.get("tokens") or {}
    return {
        "taskId": item["task_id"],
        "correct": bool(record.get("correct")),
        "prediction": str(record.get("prediction") or "")[:80],
        "tokens": int(tokens.get("input", 0) or 0) + int(tokens.get("output", 0) or 0),
        "error": str(record.get("error") or "")[:120],
        "exitCode": proc.returncode,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True, choices=["baseline", "lesson"])
    parser.add_argument("--lesson-file", default="")
    parser.add_argument("--lesson", default="")
    parser.add_argument("--tasks", default="")
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lesson = args.lesson
    if args.arm == "lesson" and not lesson and args.lesson_file:
        lesson = Path(args.lesson_file).read_text(encoding="utf-8").strip()
    if args.arm == "lesson" and not lesson:
        print(json.dumps({"ok": False, "reason": "lesson-arm-requires-lesson"},
                         ensure_ascii=False))
        return 2

    tasks = load_tasks(Path(args.tasks) if args.tasks else None)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for item in tasks:
            rows.append(run_one(item, workdir, f"{args.arm}-{item['task_id']}",
                                  lesson, args.max_turns))

    correct = sum(1 for r in rows if r["correct"])
    report = {
        "kind": "reflection-learn-ab",
        "arm": args.arm,
        "model": MODEL,
        "tasks": len(rows),
        "correct": correct,
        "correctRate": round(correct / len(rows) * 100, 1) if rows else 0.0,
        "tokens": sum(r["tokens"] for r in rows),
        "lessonApplied": bool(lesson),
        "rows": rows,
        "claimability": {
            "verdict": "not-claimable:single-arm",
            "note": "单臂结果；必须与另一臂同参数一起交给 noise gate 判读",
        },
    }
    out = Path(args.out) if args.out else (
        REPO / "eval" / "runs" / f"reflection-learn-{args.arm}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in
                      ("arm", "tasks", "correct", "correctRate", "tokens", "lessonApplied")},
                     ensure_ascii=False, indent=2))
    for row in rows:
        print(f"  {row['taskId']:10s} correct={row['correct']} "
              f"tok={row['tokens']:>7d} pred={row['prediction'][:40]}")
    return 0 if report["correct"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())