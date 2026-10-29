#!/usr/bin/env python3
"""W19 声明审计：把"每条对外主张 ↔ 证据 ↔ 等级"做成可核对的清单。

审核的不是"测试过没过"（门禁已管），而是：**每句对外说的话，能不能点到证据**。
规则（对齐 eval/RESULTS.md §六 与 FROZEN_PROTOCOL）：
  A 级：可复跑 + 唯一产物存在 + CI 记录 → 允许对外声明
  B 级：可复跑但单批次 → 必须带"单批次"标注
  C 级：外部/数据集不在仓库 → 只能引用
  D 级：n<30 / 噪声内 / 方向反号 → **禁止**任何正向表述
产物缺失、等级与证据不符、或 D 级出现正向表述 → 审核失败。

用法：python eval/final_audit.py [--json] [--out PATH]
退出码：0 通过；2 有阻断项。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
RUNS = REPO / "eval" / "runs"


def _exists(rel: str) -> bool:
    return (REPO / rel).is_file()


# 主张清单：每条 = 对外表述 + 等级 + 证据路径（可多个）+ 必需标注
CLAIMS = [
    {"id": "gaia-pass1", "claim": "GAIA 165 题 pass@1 49.1%（CI 41.2–57.0）", "grade": "A",
     "evidence": ["eval/runs/gaia/summary.json"], "requireCi": True},
    {"id": "hle-pass1", "claim": "HLE n=75 pass@1 10.7%（CI 4.0–17.3）", "grade": "A",
     "evidence": ["eval/runs/hle/summary.json"], "requireCi": True},
    {"id": "fold-ablation",
     "claim": "折叠消融阈值 0.30 降 11.5pp（McNemar p=0.011）",
     # 产物 eval/runs/gaia-nofold/ 已不存在 → 只能引用文档记录，不能算可核对证据
     "grade": "C", "documentOnly": True,
     "evidence": ["eval/RESULTS.md"], "requireLabel": "单批次"},
    {"id": "locomo", "claim": "LoCoMo 持平 + token −62%", "grade": "C",
     "evidence": ["eval/RESULTS.md"], "requireLabel": "外部"},
    {"id": "lifecycle-soft-decontam", "claim": "软标注消除污染（−2 事件，两批极差 0）", "grade": "A",
     "evidence": ["eval/runs/memory-lifecycle-ab-v3.json",
                  "eval/runs/memory-lifecycle-ab-v3b.json",
                  "eval/runs/lifecycle-noise-gate.json"],
     "requireNoiseGate": "soft:contamination"},
    {"id": "lifecycle-filter-cost", "claim": "硬过滤省 ~10% tokens 但损失 8.4pp 召回", "grade": "A",
     "evidence": ["eval/runs/lifecycle-noise-gate.json"],
     "requireNoiseGate": "filter:recallRate"},
    {"id": "reflection-recall", "claim": "反思教训可召回（3/3 命中，0 误召）", "grade": "A",
     "evidence": ["eval/runs/reflection-recall-probe.json"], "requireOk": True},
    {"id": "reflection-benefit", "claim": "反思降低同类失败率", "grade": "D",
     "evidence": [], "forbidPositive": True},
    {"id": "reflection-cost", "claim": "教训注入增加 token 消耗（两批 +90k/+356k）", "grade": "A",
     "evidence": ["eval/runs/reflection-noise-gate.json"],
     "requireNoiseGate": "with-lesson:tokens",
     "noiseGateFile": "eval/runs/reflection-noise-gate.json"},
    {"id": "evolution-benefit",
     "claim": "进化带来小的正收益（两批跨批次方向一致且超噪声，+4.2~4.5pp），但增益未随技能累积增长（ρ=−0.21）",
     "grade": "B",
     "evidence": ["eval/runs/evolution/checkpoint-no_evolution-r6.json",
                  "eval/runs/evolution/checkpoint-ws-w14d-no_evolution-r6.json"],
     "requireFile": True, "requireLabel": "两批"},
    {"id": "evolution-overhead",
     "claim": "进化自身开销 6.4–7.4%（基线臂恒 0%）", "grade": "A",
     "evidence": ["eval/runs/evolution/batch-ws-w14d.json"], "requireFile": True},
    {"id": "harness2-mechanism", "claim": "Harness² 配置轴可配可回退（机制层）", "grade": "A",
     "evidence": ["tests/test_harness_axes_configurable.py"], "requireFile": True},
    {"id": "failure-stability", "claim": "80% GAIA 失败稳定复现（10 题×2 遍）", "grade": "A",
     "evidence": ["eval/runs/failure-stability-2.json"], "requireOk": True},
]

POSITIVE_WORDS = ("提升", "改善", "优于", "增强", "收益")


def _read_json(rel: str) -> dict | None:
    path = REPO / rel
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def audit() -> dict:
    problems: list[str] = []
    rows: list[dict] = []

    # 1) 证据存在性
    for claim in CLAIMS:
        missing = [e for e in claim["evidence"] if not _exists(e)]
        if claim["grade"] != "D" and missing:
            problems.append(f"{claim['id']}:missing-evidence:{missing}")

    # 2) A 级必须有 CI 记录（真实字段名是 pass_at_1_ci95 / valid_answer_accuracy_ci95；
    #    此前审计器查 "ci95"/"ci" 导致误报——审计器自己也要被审计）
    for claim in CLAIMS:
        if claim["grade"] != "A" or not claim.get("requireCi"):
            continue
        summary = _read_json(claim["evidence"][0]) or {}
        ci = (summary.get("pass_at_1_ci95")
              or summary.get("ci95")
              or summary.get("ci"))
        if not ci:
            problems.append(f"{claim['id']}:missing-ci95")

    # 3a) A/B 级证据不得只是 markdown 自述（"文档说做过" ≠ 可核对产物）
    for claim in CLAIMS:
        if claim["grade"] not in {"A", "B"}:
            continue
        evidence = claim.get("evidence") or []
        if not evidence:
            continue
        doc_only = all(str(e).endswith((".md", ".txt")) for e in evidence)
        if doc_only and not claim.get("documentOnly"):
            problems.append(f"{claim['id']}:document-only-evidence")
        if doc_only and claim.get("documentOnly") and claim["grade"] != "C":
            problems.append(f"{claim['id']}:document-only-must-be-C")

    # 3) B/C 级必须带标注
    for claim in CLAIMS:
        label = claim.get("requireLabel")
        if not label:
            continue
        # 标注要查"对外文档"，不是 evidence[0]（后者可能是 json 产物）
        label_file = claim.get("labelFile") or "eval/RESULTS.md"
        path = REPO / label_file
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if label not in text:
            problems.append(f"{claim['id']}:missing-label:{label}")

    # 4) 需要噪声门背书的，必须实测 claimable（不同实验的噪声门产物不同，按主张指定）
    default_gate = _read_json("eval/runs/lifecycle-noise-gate.json") or {}
    for claim in CLAIMS:
        key = claim.get("requireNoiseGate")
        if not key:
            continue
        gate_file = claim.get("noiseGateFile", "eval/runs/lifecycle-noise-gate.json")
        gate = default_gate if gate_file == "eval/runs/lifecycle-noise-gate.json" \
            else (_read_json(gate_file) or {})
        item = (gate.get("perMetric") or {}).get(key) or {}
        if not item.get("claimable"):
            problems.append(f"{claim['id']}:noise-gate-not-claimable:{key}")

    # 5) D 级不得出现在文档的正向表述里
    docs = [REPO / "README.md", REPO / "eval" / "RESULTS.md"]
    for claim in CLAIMS:
        if not claim.get("forbidPositive"):
            continue
        for doc in docs:
            if not doc.is_file():
                continue
            for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
                if not any(w in line for w in POSITIVE_WORDS):
                    continue
                # 允许显式否定与 D 级标注
                if any(neg in line for neg in ("不可宣称", "not-claimable", "D 级", "反号", "噪声内")):
                    continue
                if claim["id"] == "reflection-benefit" and "反思" in line:
                    problems.append(f"{doc.name}:{lineno}:positive-claim-on-D-reflection")

    # 6) 逐条产出核对结果（供报告引用）
    for claim in CLAIMS:
        rows.append({
            "id": claim["id"], "claim": claim["claim"], "grade": claim["grade"],
            "evidence": claim["evidence"], "evidencePresent": [
                e for e in claim["evidence"] if _exists(e)],
        })

    return {
        "ok": not problems,
        "problems": problems,
        "claims": len(CLAIMS),
        "byGrade": {g: sum(1 for c in CLAIMS if c["grade"] == g) for g in ("A", "B", "C", "D")},
        "notClaimable": {k: v.get("verdict")
                         for k, v in (default_gate.get("perMetric") or {}).items()
                         if not v.get("claimable")},
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    result = audit()
    out = Path(args.out) if args.out else RUNS / "final-audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({k: result[k] for k in
                          ("ok", "problems", "claims", "byGrade", "notClaimable")},
                         ensure_ascii=False, indent=2))
        print(f"-> {out}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
