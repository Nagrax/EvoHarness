#!/usr/bin/env python3
"""ROI 评测器：回答 RSI 论文的真正问题——自进化的收益能否对冲它消耗的 token。

这不是"分数越高越好"的评测，而是一套**成本-收益核算**：
  · 收益侧：holdout/benchmark 上的正确数增量（配对，不是绝对分）
  · 成本侧：进化本身花掉的 token（沉淀抽取、评测、promotion、judge、holdup 重跑）
  · 判据：ROI = Δcorrect / Δtokens，并给出"每多对一题要花多少 token"与"回本所需轮数"

关键设计（避免自欺）：
1. **配对**：只比同题、同模型、同预算下的正确集合差异，不用绝对 pass@1 相减；
2. **成本必须含进化开销**，不能只算主线对话——否则"进化免费"是记账错觉；
3. **负收益照报**：Δcorrect < 0 时 ROI 为负，不得用"token 效率提高"掩盖；
4. **不确定区间**：n 小的时候用 bootstrap，区间跨 0 就不得宣称"值得"。

数据来源（本仓库真实文件，非估算）：
  eval/runs/<tag>/summary.json        —— pass@1、tokens_M、planned/attempted
  eval/runs/<tag>/predictions.jsonl   —— 逐题 correct 标记（用于配对）
  eval/runs/evolution/checkpoint-*.json —— 三臂进化触发、skill 数、token 细分

用法：
  python eval/roi.py report                        # 汇总所有已知 tag
  python eval/roi.py pair <tagA> <tagB>            # 同题配对比较
  python eval/roi.py evolution                     # 三臂进化的 ROI 核算
  python eval/roi.py all --json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "eval" / "runs"


# ── 逐题预测读取 ───────────────────────────────────────────────


def load_predictions(tag: str) -> dict[str, dict]:
    """返回 {task_key: {correct, tokens, ...}}；缺失文件返回空 dict（如实标注）。"""
    path = RUNS / tag / "predictions.jsonl"
    if not path.is_file():
        return {}
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        key = str(row.get("task_id") or row.get("id") or row.get("sample_id") or "")
        if not key:
            continue
        tokens = row.get("tokens") or {}
        total = int(tokens.get("input") or 0) + int(tokens.get("output") or 0)
        if not total:
            total = int(row.get("total_tokens") or 0)
        out[key] = {
            "correct": bool(row.get("correct")),
            "tokens": total,
            "timed_out": bool(row.get("timed_out")),
            "error": row.get("error") or "",
        }
    return out


def load_summary(tag: str) -> dict:
    path = RUNS / tag / "summary.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def available_tags() -> list[str]:
    """只把**真正的 benchmark run**当 tag：目录下要有 summary.json，
    且 summary 里必须有 planned 或 attempted（harness2 的汇总是分析产物，不是 benchmark）。"""
    tags = []
    if not RUNS.is_dir():
        return tags
    for directory in sorted(RUNS.iterdir()):
        summary_path = directory / "summary.json"
        if not directory.is_dir() or not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not isinstance(summary, dict):
            continue
        if "planned" not in summary and "attempted" not in summary:
            continue
        tags.append(directory.name)
    return tags


# ── 统计工具 ──────────────────────────────────────────────────


def bootstrap_diff_ci(only_a: list[int], only_b: list[int], *, rounds: int = 4000,
                      alpha: float = 0.05, seed: int = 20260119) -> dict:
    """对配对差异做 bootstrap：a 独对 vs b 独对，返回 Δ=(a-b) 的区间。"""
    rng = random.Random(seed)
    pooled = [1] * len(only_a) + [-1] * len(only_b)
    n = len(pooled)
    if n == 0:
        return {"delta": 0, "ci95": [0, 0], "n": 0, "note": "no-discordant-pairs"}
    observed = len(only_a) - len(only_b)
    samples = []
    for _ in range(rounds):
        total = 0
        for _ in range(n):
            total += pooled[rng.randrange(n)]
        samples.append(total)
    samples.sort()
    lo = samples[int(rounds * (alpha / 2))]
    hi = samples[int(rounds * (1 - alpha / 2)) - 1]
    return {"delta": observed, "ci95": [lo, hi], "n": n}


def paired_compare(tag_a: str, tag_b: str) -> dict:
    preds_a = load_predictions(tag_a)
    preds_b = load_predictions(tag_b)
    if not preds_a or not preds_b:
        return {"ok": False, "reason": "predictions-missing",
                "haveA": bool(preds_a), "haveB": bool(preds_b)}
    common = sorted(set(preds_a) & set(preds_b))
    if not common:
        return {"ok": False, "reason": "no-common-tasks"}
    only_a = [k for k in common if preds_a[k]["correct"] and not preds_b[k]["correct"]]
    only_b = [k for k in common if preds_b[k]["correct"] and not preds_a[k]["correct"]]
    both = [k for k in common if preds_a[k]["correct"] and preds_b[k]["correct"]]
    neither = [k for k in common if not preds_a[k]["correct"] and not preds_b[k]["correct"]]
    tokens_a = sum(preds_a[k]["tokens"] for k in common)
    tokens_b = sum(preds_b[k]["tokens"] for k in common)
    diff = len(only_a) - len(only_b)
    delta_tokens = tokens_a - tokens_b
    ci = bootstrap_diff_ci(only_a, only_b)

    roi = None
    if delta_tokens > 0 and diff != 0:
        roi = diff / (delta_tokens / 1_000_000)  # 每 1M tokens 换来多少道对的题
    return {
        "ok": True,
        "a": tag_a, "b": tag_b,
        "commonTasks": len(common),
        "aCorrect": sum(1 for k in common if preds_a[k]["correct"]),
        "bCorrect": sum(1 for k in common if preds_b[k]["correct"]),
        "discordant": {"onlyA": len(only_a), "onlyB": len(only_b),
                       "both": len(both), "neither": len(neither)},
        "deltaCorrect": diff,
        "deltaCorrectCI95": ci["ci95"],
        "tokensA": tokens_a, "tokensB": tokens_b, "deltaTokens": delta_tokens,
        "roiPerMillionTokens": roi,
        "verdict": ("a-better" if diff > 0 and ci["ci95"][0] > 0
                    else "b-better" if diff < 0 and ci["ci95"][1] < 0
                    else "inconclusive"),
        "note": "verdict=inconclusive 时不得宣称收益；ROI 只在 deltaTokens>0 时有意义（省 token 不是收益）",
    }


# ── 三臂进化的 ROI ────────────────────────────────────────────


def evolution_roi(curve_path: Path | None = None) -> dict:
    path = curve_path or (RUNS / "evolution" / "curve.json")
    if not path.is_file():
        return {"ok": False, "reason": "curve-missing"}
    curve = json.loads(path.read_text(encoding="utf-8"))
    points = curve.get("points") or []
    arms = curve.get("arms") or []
    by_arm: dict[str, list[dict]] = {}
    for point in points:
        by_arm.setdefault(point.get("arm") or "", []).append(point)

    def final_pass(arm: str) -> float | None:
        pts = by_arm.get(arm) or []
        if not pts:
            return None
        return float(pts[-1].get("pass_at_1") or 0.0)

    def total_tokens_m(arm: str) -> float:
        return round(sum(float(p.get("tokens_M") or 0.0) for p in by_arm.get(arm) or []), 3)

    rows = []
    gated = final_pass("gated_evolution")
    for arm in arms:
        rows.append({
            "arm": arm,
            "finalPassAt1": final_pass(arm),
            "tokensM": total_tokens_m(arm),
            "evolutionFired": (curve.get("evolution_fired_by_arm") or {}).get(arm),
            "newSkills": (curve.get("new_skills_by_arm") or {}).get(arm),
        })

    # 相对 no_evolution 的增量 ROI：进化的收益 / 多花的 token
    baseline_tokens = total_tokens_m("no_evolution")
    baseline_pass = final_pass("no_evolution")
    n_point = int(points[0].get("n") or 0) if points else 0
    min_n_for_claim = 30
    deltas = []
    for row in rows:
        if row["arm"] == "no_evolution" or row["finalPassAt1"] is None or baseline_pass is None:
            continue
        delta_pass = row["finalPassAt1"] - baseline_pass
        delta_tokens = row["tokensM"] - baseline_tokens
        raw_verdict = ("no-extra-cost" if delta_tokens <= 0 else
                       "profitable" if delta_pass > 0 else
                       "loss" if delta_pass < 0 else "break-even")
        # 小样本不得宣称收益：单点 n 太小或没有 CI 时，一律降级为 not-claimable。
        if raw_verdict == "profitable" and (n_point and n_point < min_n_for_claim):
            verdict = "not-claimable:undersampled"
        else:
            verdict = raw_verdict
        deltas.append({
            "arm": row["arm"],
            "deltaPassAt1": round(delta_pass, 2),
            "deltaTokensM": round(delta_tokens, 3),
            "roiPerMillionTokens": (round(delta_pass / delta_tokens, 2) if delta_tokens > 0 else None),
            "rawVerdict": raw_verdict,
            "verdict": verdict,
        })

    side = float(curve.get("side_query_tokens_M") or 0)
    total = float(curve.get("total_tokens_M") or 0)
    return {
        "ok": True,
        "source": str(path),
        "batchTag": curve.get("batchTag") or "legacy-untagged",

        "nPerPoint": (points[0].get("n") if points else None),
        "arms": rows,
        "relativeToBaseline": deltas,
        "costSplit": {
            "totalTokensM": total,
            "sideQueryTokensM": side,
            "evolutionOverheadPct": (round(side / total * 100, 1) if total else None),
            "note": "side_query 是进化自身的开销（沉淀抽取/评测判定），必须与主线分开看",
        },
        "claimsAllowed": [
            "三臂管线真实执行（tokens>0、耗时为真）",
            "进化确实发生（newSkills>0）",
            "holdout 只读未被破坏",
        ],
        "claimsNotAllowed": [
            "自进化带来泛化收益——n 太小，CI 覆盖全域",
            "ROI 为正——样本不足，且 cost 主要来自主线而非进化",
        ],
        "sampleWarning": (
            f"每点 n={n_point}，低于 {min_n_for_claim} 时任何'profitable'都降级为 not-claimable；"
            "扩样本请走 W14 正式曲线"
        ),
    }


# ── 汇总 ──────────────────────────────────────────────────────


def build_report(include_json: bool = False) -> dict:
    tags = available_tags()
    rows = []
    for tag in tags:
        summary = load_summary(tag)
        rows.append({
            "tag": tag,
            "planned": summary.get("planned"),
            "pass_at_1": summary.get("pass_at_1"),
            "pass_at_1_ci95": summary.get("pass_at_1_ci95"),
            "tokensM": summary.get("tokens_M"),
            "timed_out": summary.get("timed_out"),
            "errored": summary.get("errored"),
            "tokensPerCorrect": (round(float(summary.get("tokens_M") or 0) * 1e6
                                       / int(summary.get("correct") or 1), 0)
                                 if summary.get("correct") else None),
        })
    report = {
        "ok": True,
        "tags": tags,
        "benchmarks": rows,
        "pairs": [],
        "evolution": evolution_roi(),
        "costDefinition": [
            "总 tokens 含主线推理 + 工具输出 + side_query（进化开销）",
            "tokensPerCorrect = 总 tokens / 正确题数：回答'每对一题要烧多少 token'",
            "ROI(每 M tokens 换来多少对题) = Δcorrect / ΔtokensM，仅在配对且 Δtokens>0 时有定义",
        ],
        "redLines": [
            "不得用省 token 冒充收益（Δcorrect 必须为正）",
            "CI 跨 0 一律 inconclusive，不得宣称改进",
            "进化开销必须单列，不能摊进主线后说'进化免费'",
        ],
    }
    # 已知可比对：同题集的不同配置
    for a, b in (("gaia", "gaia-fold03"),):
        if a in tags and b in tags:
            report["pairs"].append(paired_compare(a, b))
    if include_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="EvoHarness ROI 评测（成本-收益核算）")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("report"); p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda a: (print_report(a.json))
                   if False else _report(a))
    p = sub.add_parser("pair"); p.add_argument("a"); p.add_argument("b")
    p.set_defaults(func=lambda a: _pair(a.a, a.b))
    p = sub.add_parser("evolution")
    p.set_defaults(func=lambda a: _evolution())
    p = sub.add_parser("all")
    p.set_defaults(func=lambda a: _report(argparse.Namespace(json=True)))
    args = parser.parse_args()
    return int(args.func(args) or 0)


def _report(args) -> int:
    report = build_report(include_json=bool(getattr(args, "json", False)))
    if not getattr(args, "json", False):
        print(f"benchmarks: {len(report['benchmarks'])}")
        for row in report["benchmarks"]:
            print(f"  {row['tag']:14s} pass@1={row['pass_at_1']} CI={row['pass_at_1_ci95']} "
                  f"tokens={row['tokensM']}M tokens/correct={row['tokensPerCorrect']}")
        ev = report["evolution"]
        if ev.get("ok"):
            print("evolution ROI:")
            for rel in ev.get("relativeToBaseline") or []:
                print(f"  {rel['arm']:20s} Δpass={rel['deltaPassAt1']:+} ΔtokM={rel['deltaTokensM']:+} "
                      f"roi={rel['roiPerMillionTokens']} verdict={rel['verdict']}")
            print(f"  costSplit: {json.dumps(ev['costSplit'], ensure_ascii=False)}")
    return 0


def _pair(a: str, b: str) -> int:
    print(json.dumps(paired_compare(a, b), ensure_ascii=False, indent=2))
    return 0


def _evolution() -> int:
    print(json.dumps(evolution_roi(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
