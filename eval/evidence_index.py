#!/usr/bin/env python3
"""生成统一证据索引：每条对外主张 → 证据文件 → sha256 → 复跑方式。

W19 第 4 条要求"交付 REPORT/原始证据索引/变更与回滚说明"。
本脚本把 `eval/final_audit.py` 里登记的主张逐条落成可核对索引——
评审者拿到索引后能：① 定位证据文件 ② 校验没被篡改 ③ 知道怎么复跑。

用法：
  python eval/evidence_index.py            # 写入 eval/EVIDENCE_INDEX.md + .json
  python eval/evidence_index.py --check     # 只校验现有证据是否仍然一致
退出码：0 成功；2 证据缺失或校验失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.final_audit import CLAIMS  # noqa: E402

MD_OUT = REPO / "eval" / "EVIDENCE_INDEX.md"
JSON_OUT = REPO / "eval" / "runs" / "evidence-index.json"

# 复跑方式（按主张 id；没有则留空，索引里明确"无独立复跑命令"）
RERUN: dict[str, str] = {
    "gaia-pass1": "python -m eval.run_benchmark run --dataset gaia --workers 4 --max-turns 60 --timeout 1200",
    "hle-pass1": "python -m eval.run_benchmark run --dataset hle",
    "lifecycle-soft-decontam": "python eval/memory_lifecycle_ab.py --repeats 2",
    "lifecycle-filter-cost": "python eval/lifecycle_noise_gate.py <batch1.json> <batch2.json>",
    "reflection-recall": "python eval/reflection_recall_probe.py",
    "reflection-cost": "python eval/lifecycle_noise_gate.py eval/runs/reflection-effect-highconf-a.json eval/runs/reflection-effect-highconf-b.json",
    "failure-stability": "python eval/failure_stability.py --limit 10 --repeats 2 --source full",
    "evolution-benefit": "python eval/batch_results.py show --since 2026-10-29T01:30 && python eval/batch_results.py show --tag ws-w14d",
    "evolution-overhead": "python eval/batch_results.py show --tag ws-w14d",
    "harness2-mechanism": "python -m pytest tests/test_harness_axes_configurable.py -q",
    "rollback-drill": "python eval/rollback_drill.py",
}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_index() -> dict:
    entries = []
    problems = []
    for claim in CLAIMS:
        files = []
        for rel in claim.get("evidence") or []:
            path = REPO / rel
            if not path.is_file():
                problems.append(f"{claim['id']}:missing:{rel}")
                continue
            files.append({
                "path": rel,
                "bytes": path.stat().st_size,
                "sha256": sha256_of(path),
            })
        if claim["grade"] in {"A", "B", "C"} and not files:
            problems.append(f"{claim['id']}:no-usable-evidence")
        entries.append({
            "id": claim["id"],
            "claim": claim["claim"],
            "grade": claim["grade"],
            "evidence": files,
            "rerun": RERUN.get(claim["id"], ""),
            "limits": [k for k in ("requireLabel", "forbidPositive") if claim.get(k)],
        })
    return {
        "generatedBy": "eval/evidence_index.py",
        "claims": entries,
        "total": len(entries),
        "byGrade": {g: sum(1 for c in entries if c["grade"] == g) for g in ("A", "B", "C", "D")},
        "withRerun": sum(1 for c in entries if c["rerun"]),
        "problems": problems,
        "ok": not problems,
    }


def render_markdown(index: dict) -> str:
    lines = [
        "# 证据索引（EvoHarness）",
        "",
        "本文件由 `eval/evidence_index.py` 生成，**不要手改**。",
        "评审者可用它逐条核对：主张 → 证据文件 → sha256 → 复跑方式。",
        "",
        f"- 主张总数：**{index['total']}**（A {index['byGrade']['A']} / B {index['byGrade']['B']}"
        f" / C {index['byGrade']['C']} / D {index['byGrade']['D']}）",
        f"- 有独立复跑命令：**{index['withRerun']}/{index['total']}**",
        "- 等级定义见 [`eval/RESULTS.md` §六](RESULTS.md)",
        "",
        "| # | 主张 | 等级 | 证据 | sha256(前 12) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, entry in enumerate(index["claims"], 1):
        files = entry["evidence"]
        paths = "<br>".join(f"`{f['path']}`" for f in files) if files else "**（无：不可宣称）**"
        sums = "<br>".join(f"`{f['sha256'][:12]}`" for f in files) if files else "—"
        lines.append(f"| {i} | {entry['claim']} | **{entry['grade']}** | {paths} | {sums} |")

    lines += ["", "## 复跑方式", ""]
    for entry in index["claims"]:
        if entry["rerun"]:
            lines.append(f"- **{entry['id']}**：`{entry['rerun']}`")
    no_rerun = [e["id"] for e in index["claims"] if not e["rerun"]]
    if no_rerun:
        lines += ["", "以下主张**没有独立复跑命令**（属文档/历史记录类，或需要外部条件）：", ""]
        lines += [f"- `{cid}`" for cid in no_rerun]

    if index["problems"]:
        lines += ["", "## 问题", ""] + [f"- {p}" for p in index["problems"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="只校验现有索引与新算结果是否一致")
    args = parser.parse_args()

    index = build_index()
    if args.check:
        if not JSON_OUT.is_file():
            print(json.dumps({"ok": False, "reason": "index-missing"}, ensure_ascii=False))
            return 2
        old = json.loads(JSON_OUT.read_text(encoding="utf-8"))
        old_sums = {(c["id"], f["path"]): f["sha256"]
                    for c in old.get("claims", []) for f in c["evidence"]}
        new_sums = {(c["id"], f["path"]): f["sha256"]
                    for c in index["claims"] for f in c["evidence"]}
        changed = [key for key in old_sums
                   if key in new_sums and old_sums[key] != new_sums[key]]
        result = {"ok": index["ok"] and not changed,
                  "changedEvidence": changed,
                  "problems": index["problems"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2

    MD_OUT.write_text(render_markdown(index), encoding="utf-8")
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(json.dumps({"ok": index["ok"], "total": index["total"],
                      "byGrade": index["byGrade"], "withRerun": index["withRerun"],
                      "problems": index["problems"],
                      "markdown": str(MD_OUT)}, ensure_ascii=False, indent=2))
    return 0 if index["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
