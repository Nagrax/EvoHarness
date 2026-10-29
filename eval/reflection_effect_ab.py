#!/usr/bin/env python3
"""反思收益对照（第二环）：有教训 vs 无教训，同题重跑。

设计（对齐 Reflexion 原型）：取上一轮**真实失败过**的题，重跑两遍——
  arm=no-lesson：HOME 里没有教训
  arm=with-lesson：HOME 里预置该失败类型的教训
判分：通过率差异。样本小（5 题），因此本脚本只产出原始数据，
可宣称性交给 eval/lifecycle_noise_gate.py 跨批次判读——不自行宣称。

用法：python eval/reflection_effect_ab.py [--limit 5] [--json]
退出码：0 完成；2 数据不足。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
PRED = REPO / "eval" / "runs" / "gaia-w18subset" / "predictions.jsonl"
PRED_FULL = REPO / "eval" / "runs" / "gaia" / "predictions.jsonl"
MODEL = os.environ.get("EVOHARNESS_EVAL_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")


def load_failed_tasks(limit: int) -> list[dict]:
    """取真实失败过的题（no-final-marker 优先，其次 wrong-answer）。"""
    from eval.run_benchmark import classify_record

    rows = [json.loads(l) for l in PRED.read_text(encoding="utf-8").splitlines()]
    failed = []
    for row in rows:
        row["dataset"] = "gaia"
        if row.get("timed_out"):
            continue
        if not row.get("has_final_marker"):
            failed.append(("no-final-marker", row))
            continue
        try:
            if classify_record(row, "gaia") != "correct":
                failed.append(("wrong-answer", row))
        except Exception:  # noqa: BLE001
            continue
    # no-final-marker 优先（正是反思教训针对的失败类型）
    failed.sort(key=lambda pair: 0 if pair[0] == "no-final-marker" else 1)
    out = []
    for kind, row in failed[:limit]:
        # worker 读的是 GAIA 原始字段（Question / Final answer），而 predictions.jsonl
        # 是运行产物（question / gold）。两种键名都提供，避免 worker 报 "'Question'"。
        item = dict(row)
        item["Question"] = row.get("question") or row.get("Question") or ""
        item["Final answer"] = row.get("gold") or row.get("Final answer") or ""
        item["Level"] = row.get("level") or row.get("Level") or ""
        item.setdefault("task_id", row.get("task_id"))
        out.append({"kind": kind, "row": item})
    return out


def load_stable_tasks(limit: int,
                      report: str = "failure-stability-2.json") -> list[dict]:
    """只取**稳定复现**的失败题（反思机制的正确作用对象）。

    依据：扩样本实验显示 80% GAIA 失败是稳定的；混合样本会把偶发失败
    掺进来，导致"教训无对象可作用"（上一轮 n=3 的负结果就是这个原因）。
    """
    import json as _json

    stability_path = REPO / "eval" / "runs" / report
    if not stability_path.is_file():
        return []
    stability = _json.loads(stability_path.read_text(encoding="utf-8"))
    stable_ids = [t["taskId"] for t in (stability.get("perTask") or [])
                  if t.get("status") == "stable"]
    # stable 题来自全量 GAIA（failure-stability-2 的 source=full），
    # 因此必须在全量 predictions 里按 task_id 找回原题（踩过一次：去子集里找不到）
    rows = [_json.loads(l) for l in PRED_FULL.read_text(encoding="utf-8").splitlines()]
    by_id = {r["task_id"]: r for r in rows}
    out = []
    for task_id in stable_ids[:limit]:
        row = by_id.get(task_id)
        if not row:
            continue
        item = dict(row)
        item["Question"] = row.get("question") or ""
        item["Final answer"] = row.get("gold") or ""
        item["Level"] = row.get("level") or ""
        item["_stable"] = True
        out.append({"kind": "stable-failure", "row": item})
    return out


def seed_lesson(home: Path, arm: str) -> str:
    """把教训写进该 arm 的 HOME 记忆目录；返回文件名（no-lesson 返回空）。"""
    if arm != "with-lesson":
        return ""
    from agents.reflection import FailureEvent, derive_lessons, lesson_to_memory

    events = [FailureEvent(f"seed-{i}", "no-final-marker", "gaia") for i in range(3)]
    payload = lesson_to_memory(derive_lessons(events)[0])
    mem_dir = home / ".evoharness" / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / payload["filename"]).write_text(payload["content"], encoding="utf-8")
    return payload["filename"]


def run_arm(arm: str, tasks: list[dict], workdir: Path) -> dict:
    home = workdir / arm / "home"
    cwd = workdir / arm / "cwd"
    home.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    lesson = seed_lesson(home, arm)

    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "PYTHONUTF8": "1", "MODEL": MODEL, "EVOHARNESS_MODEL": MODEL,
           # cwd 是隔离目录（worker 要在隔离 cwd 里干活），所以模块路径必须显式给，
           # 否则 `-m eval.evolution_experiment` 找不到包（真实踩坑）。
           "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    results = []
    for index, task in enumerate(tasks):
        item_path = workdir / f"item-{arm}-{index}.json"
        out_path = workdir / f"out-{arm}-{index}.json"
        item_path.write_text(json.dumps(task["row"], ensure_ascii=False), encoding="utf-8")
        cmd = [sys.executable, "-m", "eval.evolution_experiment", "worker",
               "--item-json", str(item_path), "--dataset", "gaia",
               "--max-turns", "18", "--timeout", "420", "--out", str(out_path)]
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                              timeout=900, check=False)
        record = {}
        if out_path.is_file():
            try:
                record = json.loads(out_path.read_text(encoding="utf-8"))
            except ValueError:
                record = {}
        tokens = record.get("tokens") or {}
        results.append({
            "taskId": task["row"]["task_id"], "kind": task["kind"],
            "exitCode": proc.returncode,
            "hasFinalMarker": bool(record.get("has_final_marker")),
            "tokens": (tokens.get("input", 0) or 0) + (tokens.get("output", 0) or 0),
            "stderrTail": proc.stderr.decode("utf-8", "replace")[-200:] if proc.returncode else "",
        })
    passed = sum(1 for r in results if r["hasFinalMarker"])
    return {"arm": arm, "lessonSeeded": lesson, "tasks": len(results),
            "withFinalMarker": passed,
            "finalMarkerRate": round(passed / len(results), 3) if results else 0.0,
            "tokens": sum(r["tokens"] for r in results), "rows": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--source", default="stability",
                        choices=("stability", "w18subset"))
    parser.add_argument("--stability-report",
                        default="failure-stability-2.json")
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.source == "stability":
        tasks = load_stable_tasks(args.limit, args.stability_report)
    else:
        tasks = load_failed_tasks(args.limit)
    if not tasks:
        print(json.dumps({"ok": False, "reason": "no-failed-tasks"}, ensure_ascii=False))
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        no_lesson = run_arm("no-lesson", tasks, workdir)
        with_lesson = run_arm("with-lesson", tasks, workdir)

    delta_pts = round((with_lesson["finalMarkerRate"]
                       - no_lesson["finalMarkerRate"]) * 100, 1)
    report = {
        "kind": "reflection-effect-ab",
        "model": MODEL,
        "source": args.source,
        "tasks": [{"taskId": t["row"]["task_id"], "kind": t["kind"]} for t in tasks],
        "noLesson": {k: no_lesson[k] for k in
                     ("tasks", "withFinalMarker", "finalMarkerRate", "tokens")},
        "withLesson": {k: with_lesson[k] for k in
                       ("tasks", "withFinalMarker", "finalMarkerRate", "tokens")},
        "deltaFinalMarkerPts": delta_pts,
        "claimability": {
            "n": no_lesson["tasks"],
            "verdict": "not-claimable:undersampled" if no_lesson["tasks"] < 30 else "needs-noise-check",
            "note": ("单批次小样本；须与另一批同参数结果一起交给 "
                     "eval/lifecycle_noise_gate.py 判读，不得由本脚本自行宣称收益"),
        },
        "detail": {"noLesson": no_lesson["rows"], "withLesson": with_lesson["rows"]},
    }
    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "reflection-effect-ab.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report if args.json else {
        "model": MODEL, "n": report["claimability"]["n"],
        "noLesson": report["noLesson"], "withLesson": report["withLesson"],
        "deltaPts": delta_pts, "verdict": report["claimability"]["verdict"],
        "out": str(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
