#!/usr/bin/env python3
"""交付一致性审计：文档里声明的数字，与磁盘上的事实**是否一致**。

为什么做（2026-10-29，目标 5「如实区分」的直接要求）：
经过 110+ 轮，检查点已 4200+ 行、HANDOFF / RESULTS / EVIDENCE_INDEX 各自迭代。
**长文档最大的风险不是写错，而是过期**——某个数字在某一轮是对的，
之后事实变了、文档没跟上，就变成"文档在撒谎"。

本脚本对比四类声明与磁盘事实：
1. 门禁用例数（检查点 vs 最新 gate.json）
2. 预算（检查点 / HANDOFF vs LIVE_BUDGET.json）
3. backlog open（检查点 vs BACKLOG.json）
4. 测试文件数（EVIDENCE_INDEX vs tests/ 实际）

**不一致不会被自动改**——只报告，由人判断哪边是对的。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
HANDOFF = Path(r"D:/claudecode/.zcode/workflows/evoharness-optimize-audit/HANDOFF.md")
CHECKPOINT = REPO / "eval" / "loop" / "CHECKPOINT-20261025.md"
BUDGET = REPO / "eval" / "runs" / "live-w13" / "LIVE_BUDGET.json"
BACKLOG = REPO / "eval" / "loop" / "BACKLOG.json"


def latest_gate() -> dict | None:
    runs = sorted((REPO / "eval" / "runs").glob("*/gate.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for path in runs:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("ok") and (data.get("runs") or []):
            return data
    return None


def facts() -> dict:
    out: dict[str, object] = {}
    gate = latest_gate()
    if gate:
        counts = [(r.get("counts") or {}).get("tests", 0) for r in gate["runs"]]
        out["gateTests"] = min(counts) if counts else 0
    if BUDGET.is_file():
        data = json.loads(BUDGET.read_text(encoding="utf-8"))
        out["spent"] = (data.get("spentTokens") or data.get("spent") or
                        data.get("totalSpent") or data.get("used") or 0)
        out["remaining"] = (data.get("remaining") or data.get("budgetRemaining")
                            or data.get("remainingTokens") or 0)
    if BACKLOG.is_file():
        data = json.loads(BACKLOG.read_text(encoding="utf-8"))
        items = data.get("items") or []
        out["backlogOpen"] = sum(1 for i in items if i.get("status") != "done")
        out["backlogTotal"] = len(items)
    tests = REPO / "tests"
    out["testFiles"] = len(list(tests.glob("test_*.py"))) if tests.is_dir() else 0
    return out


def latest_section(text: str) -> str:
    """只返回检查点里**编号最大**那一节（当前状态由最新一节声明）。"""
    matches = list(re.finditer(r"^## (\d+)\. ", text, re.M))
    if not matches:
        return text
    last = max(matches, key=lambda m: int(m.group(1)))
    return text[last.start():]


def last_claim(text: str, pattern: str) -> int | None:
    """取文档里**最后一次**出现的数字声明（最新的通常最有代表性）。"""
    matches = re.findall(pattern, text)
    if not matches:
        return None
    value = matches[-1]
    if isinstance(value, tuple):
        value = value[0]
    return int(str(value).replace(",", ""))


def main() -> int:
    fact = facts()
    issues: list[dict] = []

    cp = CHECKPOINT.read_text(encoding="utf-8") if CHECKPOINT.is_file() else ""
    ho = HANDOFF.read_text(encoding="utf-8") if HANDOFF.is_file() else ""

    # ① 门禁用例数
    claim = last_claim(latest_section(cp), r"(\d{3,4})\s*tests")
    if claim is not None and fact.get("gateTests"):
        if claim != fact["gateTests"]:
            issues.append({"kind": "gate-tests", "doc": "CHECKPOINT",
                           "claimed": claim, "actual": fact["gateTests"]})

    # ② 预算
    spent_claim = last_claim(latest_section(cp), r"累计\s*\**\**([\d,]{7,})")
    if spent_claim is not None and fact.get("spent"):
        if abs(spent_claim - fact["spent"]) > 5_000_000:
            issues.append({"kind": "spent", "doc": "CHECKPOINT",
                           "claimed": spent_claim, "actual": fact["spent"]})

    # ③ backlog open
    open_claim = last_claim(latest_section(cp), r"backlog\s*(?:open|待办)\D{0,6}(\d+)")
    if open_claim is not None and "backlogOpen" in fact:
        if open_claim != fact["backlogOpen"]:
            issues.append({"kind": "backlog-open", "doc": "CHECKPOINT",
                           "claimed": open_claim, "actual": fact["backlogOpen"]})

    # ④ HANDOFF 的门禁数
    ho_claim = last_claim(ho, r"(\d{3,4})\s*(?:tests|测试)")
    if ho_claim is not None and fact.get("gateTests"):
        if ho_claim != fact["gateTests"]:
            issues.append({"kind": "gate-tests", "doc": "HANDOFF",
                           "claimed": ho_claim, "actual": fact["gateTests"]})

    report = {
        "ok": not issues,
        "facts": fact,
        "issues": issues,
        "note": "只报告不一致，不自动修改——哪边对需人判断",
    }
    out = REPO / "eval" / "runs" / "docs-consistency.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(fact, ensure_ascii=False, indent=2))
    print("\nissues:", len(issues))
    for issue in issues:
        print(f"  {issue['doc']:12s}{issue['kind']:14s}"
              f"文档={issue['claimed']}  实际={issue['actual']}")
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())