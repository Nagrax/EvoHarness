#!/usr/bin/env python3
"""更新门禁 baseline —— 从最近一次**成功**的 gate 结果生成。

为什么需要这个脚本（2026-10-29 真实事故）：
独立审计发现 `.zcode/workflows/evoharness-optimize-audit/baseline.json` 的
`nodeIds` 是**空数组** → `compare_baseline` 的"baseline 项是否都跑了"退化成
"空集都跑完了"，**永远通过**。结果：门禁跑 840 个用例也判绿、跑 1083 个也判绿，
**新增测试整批漏跑完全无法发现**。

本脚本做三件事：
1. 找到最近的、`ok: true` 且双编码一致的 gate.json
2. 校验它的用例数不低于现有 baseline（防用更差的运行去覆盖）
3. 复制为 baseline，并打印前后对比

用法：
  python eval/update_gate_baseline.py            # 预览
  python eval/update_gate_baseline.py --apply    # 写入
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
RUNS = REPO / "eval" / "runs"
BASELINE = Path(r"D:/claudecode/.zcode/workflows/evoharness-optimize-audit/baseline.json")


def _counts(gate: dict) -> tuple[int, int]:
    runs = gate.get("runs") or []
    if not runs:
        return 0, 0
    sizes = [(r.get("counts") or {}).get("tests", 0) for r in runs]
    nodes = [len(r.get("nodeIds") or []) for r in runs]
    return min(sizes), min(nodes)


def candidate_gates() -> list[dict]:
    out = []
    for path in sorted(RUNS.glob("*/gate.json"), key=lambda p: -p.stat().st_mtime):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not data.get("ok"):
            continue
        agreement = data.get("environmentAgreement") or {}
        if not agreement.get("ok", False):
            continue
        tests, nodes = _counts(data)
        if tests <= 0 or nodes <= 0:
            continue
        out.append({"path": path, "tests": tests, "nodes": nodes, "mtime": path.stat().st_mtime})
    return out


def current_baseline_size() -> tuple[int, int]:
    if not BASELINE.is_file():
        return 0, 0
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, 0
    return _counts(data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true", help="允许用更小的运行覆盖")
    args = parser.parse_args()

    current_tests, current_nodes = current_baseline_size()
    print(f"current baseline: tests={current_tests} nodes={current_nodes}")

    candidates = candidate_gates()
    if not candidates:
        print(json.dumps({"ok": False, "reason": "no-successful-gate-with-nodes"}, ensure_ascii=False))
        return 2

    best = candidates[0]
    print(f"best candidate: {best['path'].relative_to(REPO)} "
          f"(tests={best['tests']} nodes={best['nodes']})")

    if best["nodes"] < current_nodes and not args.force:
        print(json.dumps({"ok": False,
                          "reason": f"candidate-smaller-than-current "
                                    f"({best['nodes']} < {current_nodes})；用 --force 覆盖",
                          }, ensure_ascii=False))
        return 2

    if not args.apply:
        print(json.dumps({"ok": True, "wouldApply": True,
                          "from": str(best["path"]), "to": str(BASELINE),
                          "tests": best["tests"], "nodes": best["nodes"]},
                         ensure_ascii=False, indent=2))
        return 0

    shutil.copyfile(best["path"], BASELINE)
    after_tests, after_nodes = current_baseline_size()
    print(json.dumps({"ok": True, "applied": True,
                      "before": {"tests": current_tests, "nodes": current_nodes},
                      "after": {"tests": after_tests, "nodes": after_nodes}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())