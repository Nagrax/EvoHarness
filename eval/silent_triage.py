#!/usr/bin/env python3
"""闭环判读器：把模块扫描的静默吞错信号自动分类为「真缺陷」/「合理防御」/「需人工判读」。

判据（对应 eval/loop/README.md 的第 2 节）：
  真缺陷 —— 被吞掉的是用户可见状态：保存/加载/写入/发布/恢复/同步/缓存回填；
  合理防御 —— 清理与收尾：close/kill/terminate/unlink/rmtree/shutdown、管道关闭、临时文件删除；
  需人工判读 —— 其余。

只用静态上下文（异常处理块前后若干行），不做语义模型判断；分类结果写入
eval/runs/silent-triage.json。

**定位（2026-10-31 核实后修正）**：这是**手动排查工具**，当前**不在闭环自动流程内**。
闭环里用的是 `eval/mark_triaged.py`（把人工/半自动判读过的条目置为已处理）。
两者分工：
- `silent_triage.py`：给定 BACKLOG，**自动初分类**（real-defect / reasonable-defense /
  needs-review），帮助人判断"哪些值得先看"；
- `mark_triaged.py`：把你确认过的结论**写回**条目，让闭环不再重复报同一处。

若将来要接进闭环，应在 `loop.py` 的 `_collect_module` 之后调用，并把
`needs-review` 之外的两类自动落到对应处置——**但目前没有做，不要按"已接入"理解**。

用法：python eval/silent_triage.py [--json]
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 真缺陷关键词：吞掉的是"状态"而不是"清理"
STATE_WORDS = (
    "save", "load", "write", "persist", "restore", "sync", "flush", "commit",
    "publish", "promote", "append", "record", "index", "refresh", "update",
)
# 合理防御关键词：清理型收尾
CLEANUP_WORDS = (
    "close", "kill", "terminate", "unlink", "rmtree", "shutdown", "remove",
    "cleanup", "cancel", "discard", "drop", "release", "dispose", "wait_for",
)


def classify(path: Path, line: int) -> tuple[str, str]:
    """返回 (类别, 依据)。类别 ∈ {real-defect, reasonable-defense, needs-review}。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "needs-review", "unreadable"
    start = max(0, line - 6)
    end = min(len(lines), line + 3)
    window = "\n".join(lines[start:end]).lower()
    state_hits = [w for w in STATE_WORDS if w in window]
    cleanup_hits = [w for w in CLEANUP_WORDS if w in window]
    # 清理型优先：close/kill 等出现在 try 块里，通常就是收尾
    if cleanup_hits and not state_hits:
        return "reasonable-defense", f"cleanup:{','.join(sorted(cleanup_hits))}"
    if state_hits and not cleanup_hits:
        return "real-defect", f"state:{','.join(sorted(state_hits))}"
    if state_hits and cleanup_hits:
        # 两者都有：以"保存/写入"为准（清理失败不影响用户状态，反之会）
        return "real-defect", f"state-priority:{','.join(sorted(state_hits))}|cleanup:{','.join(sorted(cleanup_hits))}"
    return "needs-review", "no-signal"


def main() -> int:
    # 与 loop_context 同源缺陷：stdout 可能是 StringIO / 包装器（测试或 CI 捕获时）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    backlog_path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not backlog_path.is_file():
        print(json.dumps({"ok": False, "reason": "backlog-missing"}, ensure_ascii=False))
        return 1
    state = json.loads(backlog_path.read_text(encoding="utf-8"))
    buckets: dict[str, list[dict]] = {"real-defect": [], "reasonable-defense": [], "needs-review": []}
    for item in state["items"]:
        detail = item.get("detail") or {}
        rule = detail.get("rule", "")
        # rule 可能是 list（历史条目 L0022 把三条判据写成了数组）。
        # 直接 `list in set` 会抛 TypeError: unhashable type —— 整个工具不可用。
        if not isinstance(rule, str) or rule not in {
                "silent-except", "silent-except-return-none"}:
            continue
        path = REPO / detail.get("file", "")
        category, reason = classify(path, int(detail.get("line") or 0))
        buckets[category].append({
            "id": item["id"],
            "file": detail.get("file"),
            "line": detail.get("line"),
            "rule": rule,
            "reason": reason,
            "status": item.get("status"),
        })
    report = {
        "ok": True,
        "counts": {k: len(v) for k, v in buckets.items()},
        **buckets,
    }
    out = REPO / "eval" / "runs" / "silent-triage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "counts": report["counts"], "out": str(out)}, ensure_ascii=False))
    for entry in buckets["real-defect"][:20]:
        print(f"  REAL  {entry['id']} {entry['file']}:{entry['line']} {entry['reason'][:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
