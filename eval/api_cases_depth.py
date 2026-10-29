#!/usr/bin/env python3
"""API 契约**深度**分析：每个端点有多少用例、覆盖哪些类型（happy/negative/security）。

覆盖率 100% 只说明"碰到过"；真正的风险在**只有 happy 用例的端点**——
它只验证"正常时能跑"，对错误输入、越权、边界完全没有防线。

输出：
- 每个端点的用例清单与类型分布
- 薄弱端点（只有 happy / 只有一个用例）
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
CONTRACT = REPO / "eval" / "backend_contract.py"

# record(name, endpoint, kind, ok, detail, ...) —— 从源码里静态提取
RECORD_RE = re.compile(
    r'record\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']+)["\']')


def collect() -> dict[str, list[dict]]:
    text = CONTRACT.read_text(encoding="utf-8")
    by_endpoint: dict[str, list[dict]] = {}
    for name, endpoint, kind in RECORD_RE.findall(text):
        by_endpoint.setdefault(endpoint, []).append({"name": name, "kind": kind})
    return by_endpoint


def main() -> int:
    by_endpoint = collect()
    total_cases = sum(len(v) for v in by_endpoint.values())

    weak: list[dict] = []
    for endpoint, cases in sorted(by_endpoint.items()):
        kinds = {c["kind"] for c in cases}
        if kinds == {"happy"} or len(cases) == 1:
            weak.append({
                "endpoint": endpoint,
                "cases": len(cases),
                "kinds": sorted(kinds),
                "reason": ("只有 happy 用例（无负向/安全防线）" if kinds == {"happy"}
                           else "只有单个用例"),
            })

    report = {
        "ok": True,
        "endpoints": len(by_endpoint),
        "totalCases": total_cases,
        "kindDistribution": {
            kind: sum(1 for v in by_endpoint.values() for c in v if c["kind"] == kind)
            for kind in sorted({c["kind"] for v in by_endpoint.values() for c in v})
        },
        "perEndpoint": {
            ep: {"cases": len(cases), "kinds": sorted({c["kind"] for c in cases})}
            for ep, cases in sorted(by_endpoint.items())
        },
        "weak": weak,
    }

    out = REPO / "eval" / "runs" / "api-cases-depth.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("perEndpoint", "weak")}, ensure_ascii=False, indent=2))
    print("\nper-endpoint:")
    for ep, info in report["perEndpoint"].items():
        print(f"  {ep:44s} cases={info['cases']:2d} kinds={info['kinds']}")
    if weak:
        print("\nWEAK（缺负向/安全防线）:")
        for item in weak:
            print(f"  {item['endpoint']:44s} {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())