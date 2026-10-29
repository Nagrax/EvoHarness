#!/usr/bin/env python3
"""反思任务 v2：**更难但确定可解**，目标是基线失败率落在 30%~70%。

v1 失败原因（2026-10-29 实测）：单数字文件 + 一次乘法 → 基线 6/6 全对（天花板效应），
教训无处施展。

v2 加三个难度维度，但每一维都**保证答案唯一**：
1. **多数字干扰**：文件里有 4 个数字（章节号 / 注释数 / 干扰项 / seed），
   只取标注 `seed=` 的那一个 —— 读错行就错
2. **取模运算**：`(seed × factor + offset) % 997` —— 算术出错概率明显上升
3. **格式严格**：必须 `FINAL: <整数>`，逗号/空格/单位都算错

三者组合仍是确定的、可判定的正确结果（不是 stable-failure 那种"根本做不到"）。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
OUT = REPO / "eval" / "runs" / "reflection-tasks-v2"


def build(count: int = 6) -> list[dict]:
    tasks = []
    for i in range(1, count + 1):
        seed = 347 * i + 89
        factor = 5 + i
        offset = 1000 * i + 37
        answer = (seed * factor + offset) % 997

        # 4 个数字：只有 seed= 那行是需要的，其余是干扰
        doc = (
            f"# 任务材料 {i}\n\n"
            f"## 章节编号\n\n本章属于第 {i * 7} 节。\n\n"
            f"## 元数据\n\n"
            f"revision={i * 3}\n"
            f"checksum={i * 11 + 5}\n"
            f"seed={seed}\n"
            f"noise={i * 13 + 2}\n\n"
            f"## 说明\n\n"
            f"只有 `seed=` 后面的整数参与计算；其余数字均为干扰项。\n"
        )
        tasks.append({
            "task_id": f"refl2-{i:02d}",
            "seed": seed, "factor": factor, "offset": offset, "answer": answer,
            "doc": doc,
            "Question": (
                f"1) 用 read_file 读取 {OUT / f'refl2-{i:02d}.md'}；"
                f"2) 只取其中 `seed=` 后面的整数 N（其余数字是干扰，不要用）；"
                f"3) 计算 (N × {factor} + {offset}) mod 997，结果取非负整数；"
                f"4) 只输出一行 `FINAL: <结果>`，不要千分位逗号、不要单位、不要解释。"
            ),
            "Final answer": f"FINAL: {answer}",
        })
    return tasks


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tasks = build()
    for task in tasks:
        (OUT / f"{task['task_id']}.md").write_text(task["doc"], encoding="utf-8")
    (OUT / "tasks.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n",
        encoding="utf-8")
    print(json.dumps({"ok": True, "count": len(tasks), "dir": str(OUT),
                      "answers": {t["task_id"]: t["Final answer"] for t in tasks}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())