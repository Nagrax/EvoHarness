#!/usr/bin/env python3
"""为"反思是否有效"构造**可解且会失败**的合成任务。

为什么必须换题（2026-10-29 判读）：
现有 `reflection_effect_ab.py` 用的是 GAIA 的 **stable-failure** 题——这些题
模型**稳定答不对**，所以反思再有效，`correct` 也永远是 0，只能退化成
`finalMarkerRate`（"有没有输出最终标记"）这种代理指标。**用不可能答对的题
测"学习能力"，结论注定是零效应**，这不是反思无效，是实验设计错了。

新设计（对齐 Reflexion 的做法）：
- 任务**可解**：读一个文件里的数字，做简单运算，按格式输出 —— 正确答案唯一
- 但**容易失败**：必须真的读文件（不读就猜不对）、必须按 `FINAL: <数字>` 格式
- 判据是 `correct`，不是"有没有 marker"

这样"反思带来行为改善"可以通过 **correct 率上升**直接验证。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
OUT = REPO / "eval" / "runs" / "reflection-tasks"


def build(count: int = 6) -> list[dict]:
    """生成 count 个同构任务（同 failure kind、不同数值），答案唯一可判。"""
    tasks = []
    for i in range(1, count + 1):
        seed = 13 * i + 7
        factor = 3 + i
        offset = 100 * i
        answer = seed * factor + offset
        tasks.append({
            "task_id": f"refl-{i:02d}",
            "seed": seed,
            "factor": factor,
            "offset": offset,
            "answer": answer,
            "Question": (
                f"1) 用 read_file 读取 {OUT / f'refl-{i:02d}.txt'} 的内容；"
                f"2) 其中只有一个整数 N，取它；"
                f"3) 计算 N × {factor} + {offset}；"
                f"4) 只输出一行，格式必须是 `FINAL: <计算结果>`。"
                "不要解释、不要省略 FINAL 前缀。"
            ),
            "Final answer": f"FINAL: {answer}",
        })
    return tasks


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tasks = build()
    # 每题写一个只含数字的文件（必须真读才知道）
    for task in tasks:
        (OUT / f"{task['task_id']}.txt").write_text(str(task["seed"]), encoding="utf-8")

    # 两臂共用的题目清单
    manifest = [{"taskId": t["task_id"], "kind": "format-and-read"} for t in tasks]
    (OUT / "tasks.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n",
        encoding="utf-8")
    (OUT / "manifest.json").write_text(
        json.dumps({"tasks": manifest, "kind": "refl-synthetic",
                    "failureKind": "format-and-read"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    print(json.dumps({"ok": True, "count": len(tasks),
                      "dir": str(OUT),
                      "answers": {t["task_id"]: t["Final answer"] for t in tasks}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())