#!/usr/bin/env python3
"""W12 冻结协议守卫：校验 FROZEN_PROTOCOL.json 的完整性，并做预算前置检查。

职责：
1. 校验冻结协议结构（样本量、统计门、质量门、预算字段齐全且自洽）；
2. 预算前置门：按协议的 reservations 规则检查 LIVE_BUDGET 剩余，
   零预算/余额不足在**请求前**拒绝（exit 4）；
3. 校验协议引用的工具真实存在（eval/roi.py 等）——引用不存在的工具=纸面协议。

用法：
  python eval/frozen_protocol_guard.py check
  python eval/frozen_protocol_guard.py budget --needed 5000000
退出码：0 通过；2 协议无效；4 预算不足。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROTOCOL = REPO / "eval" / "FROZEN_PROTOCOL.json"
LEDGER = REPO / "eval" / "runs" / "live-w13" / "LIVE_BUDGET.json"

REQUIRED_TOOL_REFS = [
    "eval/roi.py",
    "eval/holdout_calibrate.py",
    "eval/variance_check.py",
    "eval/judge_bias_probe.py",
    "eval/skill_combo_replay.py",
    "eval/harness2_analyze.py",
]


def check_protocol() -> tuple[list[str], dict]:
    problems: list[str] = []
    if not PROTOCOL.is_file():
        return ["protocol-missing"], {}
    try:
        data = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    except ValueError as error:
        return [f"invalid-json: {error}"], data

    # 结构完整性
    for key in ("arms", "sampleSizes", "statistics", "budget", "qualityConstraints",
                "scopeReductions", "changeControl"):
        if key not in data:
            problems.append(f"missing-section:{key}")

    arms = data.get("arms") or {}
    if set(arms.get("threeArm") or []) != {"no_evolution", "ungated_evolution", "gated_evolution"}:
        problems.append("arms.threeArm-must-be-canonical-three")

    formal = (data.get("sampleSizes") or {}).get("formal") or {}
    if int(formal.get("holdoutPerArm") or 0) * int(formal.get("repeats") or 0) < 30:
        problems.append("formal.effectiveN-below-30")
    if int(formal.get("rounds") or 0) < 10:
        problems.append("formal.rounds-below-10")

    stats = data.get("statistics") or {}
    for rule in ("pairing", "varianceGate", "claimRule"):
        if rule not in stats:
            problems.append(f"statistics.{rule}-missing")

    budget = data.get("budget") or {}
    if int(budget.get("totalTokens") or 0) <= 0:
        problems.append("budget.totalTokens-invalid")
    if not budget.get("ledger"):
        problems.append("budget.ledger-missing")
    accounting = budget.get("accounting") or []
    if not any("unknown" in str(item) for item in accounting):
        problems.append("budget.accounting-must-cover-unknown-usage")

    quality = data.get("qualityConstraints") or {}
    gates = quality.get("judgeBiasGates") or {}
    for probe, expected in (("position", "unbiased"), ("length", "unbiased"),
                            ("injection", "robust"), ("schema", "robust")):
        if gates.get(probe) != expected:
            problems.append(f"quality.judgeBiasGates.{probe}-must-be-{expected}")
    if not quality.get("noAutoCover"):
        problems.append("quality.noAutoCover-missing")

    # 引用的工具必须真实存在
    for ref in REQUIRED_TOOL_REFS:
        if not (REPO / ref).is_file():
            problems.append(f"tool-ref-missing:{ref}")

    return problems, data


def check_budget(needed: int) -> tuple[int, dict]:
    """预算前置门：返回 (exit_code, detail)。0=可跑；4=不足/缺失。"""
    if not LEDGER.is_file():
        return 4, {"ok": False, "code": "budget-missing", "path": str(LEDGER),
                   "reason": "无 LIVE_BUDGET.json：未登记预算，请求前拒绝"}
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    remaining = int(ledger.get("remainingTokens") or 0)
    detail = {
        "ok": remaining >= needed,
        "code": "ok" if remaining >= needed else "insufficient-balance",
        "neededTokens": needed,
        "remainingTokens": remaining,
        "spentTokens": ledger.get("spentTokens"),
        "totalTokens": ledger.get("totalTokens"),
    }
    if remaining < needed:
        detail["action"] = "blocked（协议规定：余额不足在请求前拒绝，不记账 0 成本）"
    return (0 if remaining >= needed else 4), detail


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check"); p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda a: _cmd_check(a.json))
    p = sub.add_parser("budget"); p.add_argument("--needed", type=int, required=True)
    p.set_defaults(func=lambda a: _cmd_budget(a.needed))
    args = parser.parse_args()
    return int(args.func(args) or 0)


def _cmd_check(as_json: bool) -> int:
    problems, data = check_protocol()
    payload = {"ok": not problems, "problems": problems,
               "protocolVersion": (data or {}).get("version")}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not problems else 2


def _cmd_budget(needed: int) -> int:
    code, detail = check_budget(needed)
    print(json.dumps(detail, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
