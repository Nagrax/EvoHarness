#!/usr/bin/env python3
"""构造"长文档"任务：强制让上下文超过折叠阈值，用于 W15 策略对照。

为什么需要：GAIA 单题 18 轮的上下文利用率只有约 5%（10K/轮 vs 108K 有效窗口），
即使把阈值压到 0.30 也不触发折叠 → 三策略行为必然相同 → 实验 uninformative。

本脚本造一个 **60K tokens 的文档**，任务要求"读完整文档后回答末尾问题"：
- agent 必须 read_file 读入 60K → 上下文利用率 ≈ 55% → 必然触发 0.30 折叠
- 正确答案藏在文档末尾，确保"不读完就答不对"（否则可以跳读，折叠不会发生）

产物：eval/runs/context/longdoc/tasks.jsonl（符合 worker 的 item 结构）
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
OUT = REPO / "eval" / "runs" / "context" / "longdoc"

# 目标约 60K tokens ≈ 240K 字符（中文/英文混合按 ~4 字符/token 粗估）
TARGET_CHARS = 240_000

FILLER_POOL = [
    "系统按阶段推进，每阶段有明确的准入与退出条件，任何跳阶段都会让后续证据链断裂。",
    "评测数据必须与训练数据隔离，否则提升无法归因；隔离靠 family 级划分保证。",
    "当出现不可复现的差异时，优先怀疑采样噪声，而不是先改实现。",
    "配置项的默认值要保守：默认关闭自动晋升，避免误伤生产技能。",
    "日志里必须能区分「没发生」与「失败了」——这两种情况对排查的意义相反。",
    "任何自动修复都要有回滚路径，且回滚本身要留审计记录。",
    "指标口径一旦写进对外文档就不能悄悄改，改了要标注并重算历史。",
    "跨批次复核是拒绝「单批看起来有效」的唯一手段。",
]


def build_document() -> tuple[str, str, str]:
    """返回 (文档正文, 末尾问题, 正确答案)。"""
    parts: list[str] = ["# 长文档压力测试材料\n"]
    index = 0
    char_count = 0
    while char_count < TARGET_CHARS:
        paragraph = FILLER_POOL[index % len(FILLER_POOL)]
        block = f"\n## 第 {index + 1} 节\n\n{paragraph}\n"
        parts.append(block)
        char_count += len(block)
        index += 1

    # 末尾埋一个只有读到这里才知道的答案
    secret = f"ZK-{index:05d}"
    parts.append(
        f"\n## 附录：校验令牌\n\n"
        f"本文档共有 {index} 节。校验令牌为 **{secret}**。\n"
        f"若被问及令牌，只回答该令牌本身。\n"
    )
    question = (
        "请阅读 docs/longdoc.md 的**全部内容**（尤其附录），"
        "然后只输出其中的校验令牌（形如 ZK-XXXXX）。"
    )
    return "".join(parts), question, secret


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    document, question, secret = build_document()

    # worker 在隔离 cwd 里跑，文档随任务一起下发（agent 用 read_file 读）
    doc_path = OUT / "longdoc.md"
    doc_path.write_text(document, encoding="utf-8")

    item = {
        "task_id": "longdoc-fold-trigger",
        "Question": f"{question}\n\n文档路径（绝对路径）：{doc_path}",
        "Final answer": secret,
        "Level": "synthetic",
    }
    (OUT / "tasks.jsonl").write_text(
        json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "docChars": len(document),
        "estimatedTokens": len(document) // 4,
        "sections": (len(document.split("## 第 ")) - 1),
        "task": str(OUT / "tasks.jsonl"),
        "answer": secret,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
