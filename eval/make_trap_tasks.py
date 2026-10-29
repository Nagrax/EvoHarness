#!/usr/bin/env python3
"""陷阱任务（v3）：预算足够，但失败来自**策略选择**——这才是反思能改善的失败。

前四次反思实验失败的共同原因（2026-10-29 汇总）：
1. GAIA stable-failure：正确率恒 0（太难，不可解）
2. 合成 v1/v2：正确率 100%（太易，无失败可学）
3. 难度梯度 L3-L5：三档都 83.3%，失败是撞轮次预算（物理限制）
4. L5 + max-turns=3：两臂 delta 0，因为任务需要 ≥5 轮而只给 3 轮（物理不可能）

**正确平台的三条件**（本轮按此设计）：
- ① 预算足够完成（给 8 轮，任务只需 3-4 轮）
- ② 失败来自**策略不当**（不是"做不到"）
- ③ 教训能指向**真实可选的行为**

**陷阱设计**：任务里有两条互相矛盾的指示，**必须读完两个文件才能发现哪条有效**。
- `a.md`：`seed=<N>` + 一行**过期的误导说明**"直接用 seed 计算即可"
- `b.md`：`supersedes=a` + `key=<K>` + "a 的说明已作废，请用 key"
→ 只读 a 就计算 → **算错**（策略失败：跳读/轻信过期说明）
→ 读完两个文件并遵守 supersedes → **正确**

失败率由模型"是否会跳读"决定，**与轮次预算无关**（8 轮绰绰有余）。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
OUT = REPO / "eval" / "runs" / "trap-tasks"


def build(count: int = 6) -> list[dict]:
    tasks = []
    for i in range(1, count + 1):
        seed = 211 * i + 53          # 干扰值
        key = 97 * i + 31            # 正确值
        factor, offset = 7 + i, 500 + 17 * i
        mod = 887 + i

        trap_answer = (seed * factor + offset) % mod     # 只读 a 会得到这个（错）
        answer = (key * factor + offset) % mod           # 读完两文件才正确

        doc_a = (
            f"# 材料 A（第 {i} 组）\n\n"
            f"seed={seed}\n\n"
            "## 使用说明\n\n"
            "直接用本文件的 seed 值参与计算即可，无需查看其他文件。\n"
        )
        doc_b = (
            f"# 材料 B（第 {i} 组）\n\n"
            f"supersedes=a\n"
            f"key={key}\n\n"
            "## 更正说明\n\n"
            "材料 A 中的使用说明已作废。**必须用本文件的 key 值**替代 seed。\n"
        )
        tasks.append({
            "task_id": f"T{i:02d}",
            "level": "trap",
            "seed": seed,
            "key": key,
            "trap_answer": trap_answer,
            "answer": answer,
            "factor": factor,
            "offset": offset,
            "mod": mod,
            "Question": (
                f"读取 {OUT / f'T{i:02d}-a.md'} 与 {OUT / f'T{i:02d}-b.md'} 两个文件，"
                "按它们的说明确定要用的数值（注意：两个文件之间可能有更正关系）；"
                f"然后计算 (值 × {factor} + {offset}) mod {mod}，取非负整数，"
                "只输出一行 `FINAL: <结果>`。"
            ),
            "Final answer": f"FINAL: {answer}",
        })
    return tasks


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tasks = build()
    for task in tasks:
        tid = task["task_id"]
        (OUT / f"{tid}-a.md").write_text(
            f"# 材料 A（第 {tid} 组）\n\nseed={task['seed']}\n\n"
            "## 使用说明\n\n直接用本文件的 seed 值参与计算即可，无需查看其他文件。\n",
            encoding="utf-8")
        (OUT / f"{tid}-b.md").write_text(
            f"# 材料 B（第 {tid} 组）\n\nsupersedes=a\nkey={task['key']}\n\n"
            "## 更正说明\n\n材料 A 中的使用说明已作废。**必须用本文件的 key 值**替代 seed。\n",
            encoding="utf-8")
    (OUT / "tasks.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": True, "count": len(tasks), "dir": str(OUT),
        "correctIfReadBoth": {t["task_id"]: f"FINAL: {t['answer']}" for t in tasks},
        "wrongIfOnlyReadA": {t["task_id"]: f"FINAL: {t['trap_answer']}" for t in tasks},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())