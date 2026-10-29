#!/usr/bin/env python3
"""难度梯度任务生成器：找到模型的**能力边界**（50% 通过率附近）。

为什么做这个（2026-10-29 的判定）：
- GAIA stable-failure 题：模型**稳定失败**（correct 恒 0）→ 测不出学习效应
- 合成 v1/v2：模型**全对**（100%）→ 天花板效应，同样测不出
- 两者之间需要"**可解但会失败**"的任务带，而这条边界**必须先标定**

本脚本生成 5 档同构任务（难度递增但每档都确定可解），每档 6 题：
- L1 单数字 + 单步          （已知 100%，作为对照）
- L2 多数字干扰 + 单步      （已知 100%）
- L3 多数字 + 两步运算
- L4 双文件 + 两步 + 取模
- L5 三文件 + 中间结果传递 + 取模

产出：tasks-L{n}.jsonl + 每题的数据文件；正确答案由生成器算出。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
OUT = REPO / "eval" / "runs" / "difficulty-ladder"


def _doc_basic(seed: int, extra: dict[str, int] | None = None) -> str:
    lines = ["# 材料", "", f"seed={seed}"]
    for name, value in (extra or {}).items():
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def build_level(level: int, count: int = 6) -> list[dict]:
    tasks = []
    for i in range(1, count + 1):
        base = 137 * i + 41
        f1, f2 = 3 + i, 7 + 2 * i
        off = 1000 + 13 * i
        mod = 991 + i
        answer, doc, question = None, "", ""

        if level == 1:
            answer = base * f1 + off
            doc = _doc_basic(base)
            question = (f"读 {OUT / f'L1-{i:02d}.md'}，取 seed 值 N，"
                        f"计算 N × {f1} + {off}，只输出 `FINAL: <结果>`。")
        elif level == 2:
            answer = (base * f1 + off) % mod
            doc = _doc_basic(base, {"noise1": i * 17, "noise2": i * 23})
            question = (f"读 {OUT / f'L2-{i:02d}.md'}，只取 seed 值 N（其余是干扰），"
                        f"计算 (N × {f1} + {off}) mod {mod}，"
                        f"只输出 `FINAL: <结果>`（非负整数，无逗号）。")
        elif level == 3:
            mid = (base * f1 + off) % mod
            answer = (mid * f2 + base) % mod
            doc = _doc_basic(base, {"noise1": i * 19})
            question = (f"读 {OUT / f'L3-{i:02d}.md'}，取 seed 值 N，"
                        f"第 1 步：M = (N × {f1} + {off}) mod {mod}；"
                        f"第 2 步：R = (M × {f2} + N) mod {mod}；"
                        f"只输出 `FINAL: <R>`。")
        elif level == 4:
            mid = (base * f1 + off) % mod
            second = 53 * i + 7
            answer = (mid * f2 + second) % mod
            doc = _doc_basic(base)
            question = (f"读两个文件：{OUT / f'L4-{i:02d}-a.md'}（取 seed 值 N）与 "
                        f"{OUT / f'L4-{i:02d}-b.md'}（取 key 值 K）；"
                        f"第 1 步 M = (N × {f1} + {off}) mod {mod}；"
                        f"第 2 步 R = (M × {f2} + K) mod {mod}；"
                        f"只输出 `FINAL: <R>`。")
        else:  # level 5
            mid = (base * f1 + off) % mod
            second = 61 * i + 11
            third = 29 * i + 3
            answer = ((mid * f2 + second) % mod * third) % mod
            doc = _doc_basic(base)
            question = (f"读三个文件：{OUT / f'L5-{i:02d}-a.md'}（N）、"
                        f"{OUT / f'L5-{i:02d}-b.md'}（K）、"
                        f"{OUT / f'L5-{i:02d}-c.md'}（J）；"
                        f"第 1 步 M = (N × {f1} + {off}) mod {mod}；"
                        f"第 2 步 T = (M × {f2} + K) mod {mod}；"
                        f"第 3 步 R = (T × J) mod {mod}；"
                        f"只输出 `FINAL: <R>`。")
        tasks.append({
            "task_id": f"L{level}-{i:02d}",
            "level": level,
            "answer": answer,
            "doc": doc,
            "extra": {"f1": f1, "f2": f2, "off": off, "mod": mod},
            "Question": question,
            "Final answer": f"FINAL: {answer}",
        })
    return tasks


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {}
    for level in range(1, 6):
        tasks = build_level(level)
        for task in tasks:
            tid = task["task_id"]
            (OUT / f"{tid}.md").write_text(task["doc"], encoding="utf-8")
            if level == 4:
                (OUT / f"{tid}-a.md").write_text(task["doc"], encoding="utf-8")
                (OUT / f"{tid}-b.md").write_text(
                    f"# 材料 B\n\nkey={53 * int(tid.split('-')[1]) + 7}\n", encoding="utf-8")
            if level == 5:
                idx = int(tid.split("-")[1])
                (OUT / f"{tid}-a.md").write_text(task["doc"], encoding="utf-8")
                (OUT / f"{tid}-b.md").write_text(f"key={61 * idx + 11}\n", encoding="utf-8")
                (OUT / f"{tid}-c.md").write_text(f"key={29 * idx + 3}\n", encoding="utf-8")
        with (OUT / f"tasks-L{level}.jsonl").open("w", encoding="utf-8") as fh:
            for task in tasks:
                fh.write(json.dumps(task, ensure_ascii=False) + "\n")
        summary[f"L{level}"] = {"tasks": len(tasks),
                                "answers": {t["task_id"]: t["Final answer"] for t in tasks}}

    (OUT / "manifest.json").write_text(
        json.dumps({"levels": summary, "dir": str(OUT)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps({"ok": True, "levels": list(summary),
                      "tasksPerLevel": 6, "dir": str(OUT)}, ensure_ascii=False, indent=2))
    for lvl, info in summary.items():
        print(f"  {lvl}: " + ", ".join(list(info["answers"].values())[:3]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())