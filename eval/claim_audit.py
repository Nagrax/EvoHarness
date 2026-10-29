#!/usr/bin/env python3
"""声明审计：对外文档不得出现超出证据强度的结论（D 级禁止、B/C 级必须标注）。

为什么需要机器守卫：口径写在文档里靠自觉一定会漂。本工具把
① 评测器产出的 verdict（not-claimable / claimable）
② 文档里的正向结论表述
做交叉检查，不一致即失败。

检查规则：
  R1 README 基准表每一行必须有等级列，取值 ∈ {A, B, C, D, —}（含 (…) 说明）
  R2 RESULTS.md 必须存在"证据强度分级"节，且列出所有当前 not-claimable 指标
  R3 文档中不得把 not-claimable 指标写成"提升/改善/优于"等正向结论
  R4 每个等级标注必须是合法值

用法：python eval/claim_audit.py [--json]
退出码：0 通过；2 有违规。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
RESULTS = REPO / "eval" / "RESULTS.md"
NOISE = REPO / "eval" / "runs" / "lifecycle-noise-gate.json"

VALID_GRADES = {"A", "B", "C", "D", "—"}

# D 级指标对应的"正向结论"词：出现即违规
POSITIVE_WORDS = ("提升", "改善", "优于", "增强", "变好", "收益")


def load_not_claimable() -> dict[str, str]:
    """从噪声门产物读当前 not-claimable 指标 → verdict。"""
    if not NOISE.is_file():
        return {}
    data = json.loads(NOISE.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for key, item in (data.get("perMetric") or {}).items():
        if not item.get("claimable"):
            out[key] = str(item.get("verdict") or "not-claimable")
    return out


def check_readme_grades() -> list[str]:
    problems: list[str] = []
    if not README.is_file():
        return ["readme-missing"]
    text = README.read_text(encoding="utf-8")
    lines = text.splitlines()
    table_rows = [l for l in lines if l.startswith("| **") and l.count("|") >= 4]
    if not table_rows:
        return ["readme-benchmark-table-not-found"]
    for row in table_rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        grade_cell = cells[2]
        # 先抓"看起来像等级"的单元格（单个大写字母/破折号，可带加粗），再验合法性，
        # 这样非法等级（如 Z）能单独报 invalid-grade，而不是都笼统报 missing。
        m = re.match(r"^\**([A-Z—])\**", grade_cell)
        if not m:
            problems.append(f"readme-row-missing-grade:{cells[0][:40]}")
            continue
        if m.group(1) not in VALID_GRADES:
            problems.append(f"readme-invalid-grade:{m.group(1)}")
    return problems


def check_results_section() -> list[str]:
    problems: list[str] = []
    if not RESULTS.is_file():
        return ["results-missing"]
    text = RESULTS.read_text(encoding="utf-8")
    if "证据强度分级" not in text:
        problems.append("results-missing-grade-section")
    not_claimable = load_not_claimable()
    for key in not_claimable:
        # 指标名形如 soft:recallRate —— 文档里必须能看到该臂与维度的 D 级标注
        arm, _, metric = key.partition(":")
        if metric == "recallRate" and "召回" not in text:
            problems.append(f"results-missing-recall-grade:{key}")
    if not_claimable and "D" not in text:
        problems.append("results-no-d-grade-marked")
    return problems


def check_no_positive_claim_for_unclaimable() -> list[str]:
    """R3：文档里不得把 not-claimable 指标写成正向结论。"""
    problems: list[str] = []
    not_claimable = load_not_claimable()
    if not not_claimable:
        return problems
    # 召回维度的 not-claimable（方向反号）绝不能被写成"提升召回"
    recall_unclaimable = [k for k in not_claimable if k.endswith(":recallRate")]
    if recall_unclaimable:
        for name, path in (("README.md", README), ("eval/RESULTS.md", RESULTS)):
            if not path.is_file():
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "召回" not in line and "recall" not in line.lower():
                    continue
                if any(w in line for w in POSITIVE_WORDS):
                    # 允许显式否定的表述
                    if any(neg in line for neg in ("不可宣称", "not-claimable", "D 级",
                                                   "反号", "噪声内", "不改变")):
                        continue
                    problems.append(f"{name}:{lineno}:positive-claim-on-unclaimable-recall")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    problems = (check_readme_grades() + check_results_section()
                + check_no_positive_claim_for_unclaimable())
    payload = {
        "ok": not problems,
        "problems": problems,
        "notClaimable": load_not_claimable(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not problems else 2


if __name__ == "__main__":
    raise SystemExit(main())
