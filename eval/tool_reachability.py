#!/usr/bin/env python3
"""工具可达性审计：找出「没人调用 + 跑不起来」的僵尸脚本。

为什么做（2026-10-31 的直接推广）：
`silent_triage.py` 因为 `detail.rule` 是 list 而**完全崩掉**，
但它**没有被任何代码调用**，所以崩溃一直没人发现——而它的 docstring
还声称「供 loop.py 消费」。

这类"僵尸工具"的特征：① 零引用 ② 实际跑不起来 ③ 文档声称在用。

本脚本对 `eval/*.py` 逐个：
- 查是否被 tests/ 或其他 eval 脚本引用
- 用 `--help` 探活（安全，不修改数据）
- 输出三类：healthy / unused-but-runs / **broken**
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
EVAL = REPO / "eval"
TESTS = REPO / "tests"


# 与评测无关、混在 eval/ 目录里的脚本（多为早期数学研究残留）。
# 不计入 broken —— 它们的 bug 不影响 EvoHarness，但**也不擅自删除**（用户资产）。
NON_EVAL_SCRIPTS = {
    "final_check", "compute_irreps", "s7_dimensions", "check_self_conjugate",
    "list_small_partitions", "dimension_stats", "check_boundary", "check_max_dim",
}

SELF_NAME = "tool_reachability"

def references(name: str) -> list[str]:
    """谁引用了这个脚本（tests/ + 其他 eval 脚本 + workflows）。"""
    hits: list[str] = []
    for path in list(TESTS.glob("test_*.py")) + list(EVAL.glob("*.py")):
        if path.name == f"{name}.py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if f"{name}.py" in text or re.search(rf"\b{re.escape(name)}\b", text):
            hits.append(path.relative_to(REPO).as_posix())
    return hits


def _is_encoding_limit(detail: str) -> bool:
    """编码类失败：GBK 环境下输出非 GBK 字符导致，不是逻辑缺陷。"""
    lowered = detail.lower()
    return ("unicodeencodeerror" in lowered or "codec can't encode" in lowered
            or "'gbk' codec" in lowered)


def probe(script: Path) -> tuple[str, str]:
    """用 --help 探活（不执行副作用）。"""
    try:
        proc = subprocess.run([sys.executable, str(script), "--help"],
                              cwd=str(REPO), capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=30)
    except subprocess.TimeoutExpired:
        return "timeout", ">30s"
    if proc.returncode == 0:
        return "ok", ""
    err = (proc.stderr or "").strip().splitlines()
    tail = err[-1][:120] if err else f"exit={proc.returncode}"
    return "fail", tail


def main() -> int:
    scripts = sorted(p for p in EVAL.glob("*.py")
                     if not p.name.startswith("__") and p.stem != SELF_NAME)
    broken: list[dict] = []
    non_eval_broken: list[dict] = []
    encoding_limited: list[dict] = []
    unused: list[dict] = []
    healthy = 0

    for script in scripts:
        name = script.stem
        status, detail = probe(script)
        refs = references(name)
        entry = {"script": f"eval/{script.name}", "probe": status,
                 "detail": detail, "referencedBy": refs[:3],
                 "refCount": len(refs),
                 "nonEval": name in NON_EVAL_SCRIPTS}
        if status in {"fail", "timeout"}:
            if _is_encoding_limit(entry["detail"]):
                encoding_limited.append(entry)
                continue
            # 非评测脚本的失败单独归类，不混淆"评测工具坏了"
            (non_eval_broken if entry["nonEval"] else broken).append(entry)
            continue
        if not refs:
            unused.append(entry)
        else:
            healthy += 1

    report = {
        "ok": not broken,
        "totalScripts": len(scripts),
        "healthy": healthy,
        "unusedButRuns": len(unused),
        "broken": len(broken),
        "brokenDetail": broken,
        "nonEvalBrokenCount": len(non_eval_broken),
        "encodingLimitedCount": len(encoding_limited),
        "encodingLimitedDetail": [{"script": e["script"]} for e in encoding_limited],
        "nonEvalBrokenDetail": [{"script": e["script"], "detail": e["detail"]}
                                for e in non_eval_broken],
        "unusedDetail": [{"script": e["script"]} for e in unused],
    }
    out = REPO / "eval" / "runs" / "tool-reachability.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"scripts: {len(scripts)} | healthy: {healthy} | "
          f"unused-but-runs: {len(unused)} | **broken: {len(broken)}** "
          f"| non-eval-broken: {len(non_eval_broken)} "
          f"| encoding-limited: {len(encoding_limited)}")
    if non_eval_broken:
        print("\n(非评测脚本，单独归类):")
        for entry in non_eval_broken:
            print(f"  {entry['script']:38s} {entry['detail'][:70]}")
    if broken:
        print("\n=== 跑不起来的脚本 ===")
        for entry in broken:
            print(f"  {entry['script']:38s} {entry['probe']:8s} {entry['detail'][:80]}")
            print(f"      引用: {entry['refCount']} {entry['referencedBy']}")
    return 0 if not broken else 1


if __name__ == "__main__":
    raise SystemExit(main())