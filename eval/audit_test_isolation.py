#!/usr/bin/env python3
"""测试隔离审计：测试用到的"路径真相源"是否被真正隔离。

背景（2026-10-29 真实事故）：`get_evolution_dir()` = `Path.cwd()/.evoharness/...`，
而 `rollback_drill` 只隔离了 HOME → **1547 行演练记录写进真实审计**。

本脚本对每个测试文件检查：它触碰了哪些路径来源（cwd / HOME / get_evolution_dir /
get_memory_dir），以及 fixture 里是否对**同一个来源**做了隔离。
输出"触碰了但没隔离"的组合，供逐个修。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
TESTS = REPO / "tests"

# 来源 → 隔离它的典型写法（正则片段）
ISOLATION = {
    "cwd": [r"chdir\(", r'"get_evolution_dir"\s*,', r"'get_evolution_dir'\s*,",
            r'"get_memory_dir"\s*,', r"'get_memory_dir'\s*,"],
    "home": [r'setenv\(\s*"HOME"', r"setenv\(\s*'HOME'",
             r'setenv\(\s*"USERPROFILE"', r"setenv\(\s*'USERPROFILE'"],
}

# 使用路径来源的信号
USAGE = {
    "cwd": [r"get_evolution_dir\(", r"get_memory_dir\(", r"Path\.cwd\(\)",
            r"set_tier\(", r"get_tier\(", r"register_evolution_write\("],
    "home": [r"Path\.home\(\)"],
}


# 已人工复核的误报（附理由）。新增条目必须写清"为什么不写盘"。
REVIEWED_FALSE_POSITIVES = {
    "tests/test_skill_tiers_visibility.py":
        "只在断言字符串里出现 get_tier(name)，没有真实调用；文件本身只读 loop.py 源码",
    "tests/test_tools_fixes.py":
        "用 Path.home() 构造参数传给只读函数 _is_protected_config_edit，不写盘",
}


def audit() -> list[dict]:
    findings = []
    for path in sorted(TESTS.glob("test_*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        touched = [src for src, pats in USAGE.items()
                   if any(re.search(p, text) for p in pats)]
        if not touched:
            continue
        isolated = {src for src, pats in ISOLATION.items()
                    if any(re.search(p, text) for p in pats)}
        # cwd 与 home 的隔离写法不同，分别判定
        missing = []
        for src in touched:
            if src == "cwd" and not any(re.search(p, text) for p in ISOLATION["cwd"]):
                missing.append("cwd")
            if src == "home" and not any(re.search(p, text) for p in ISOLATION["home"]):
                missing.append("home")
        if missing:
            findings.append({"file": path.relative_to(REPO).as_posix(),
                             "touches": touched, "missingIsolation": sorted(set(missing))})
    return findings


def main() -> int:
    raw = audit()
    real = [f for f in raw if f["file"] not in REVIEWED_FALSE_POSITIVES]
    reviewed = [{"file": f["file"], "reason": REVIEWED_FALSE_POSITIVES[f["file"]]}
                for f in raw if f["file"] in REVIEWED_FALSE_POSITIVES]
    findings = real
    report = {"ok": True, "filesWithGaps": len(real),
              "findings": real, "reviewedFalsePositives": reviewed}
    out = REPO / "eval" / "runs" / "test-isolation-audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"filesWithGaps": len(findings),
                      "reviewedFalsePositives": len(reviewed)}, ensure_ascii=False))
    for item in reviewed:
        print(f"  [已复核误报] {item['file']}: {item['reason']}")
    for item in findings:
        print(f"  {item['file']:52s} touches={item['touches']} missing={item['missingIsolation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())