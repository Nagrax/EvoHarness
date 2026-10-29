#!/usr/bin/env python3
"""后端 API 覆盖分析：server/app.py 的每个路由是否被 eval/backend_contract.py 检验。

用户目标 2 要求"后端每个 API/功能被检验"。这个脚本回答：**还差哪些**。
输出未覆盖清单（供补契约用例），并给出每个路由的 HTTP 方法。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
APP = REPO / "server" / "app.py"
CONTRACT = REPO / "eval" / "backend_contract.py"

ROUTE_RE = re.compile(r'@app\.(get|post|delete|put|patch)\(\s*["\']([^"\']+)["\']')


def routes() -> list[dict]:
    text = APP.read_text(encoding="utf-8")
    out = []
    for method, path in ROUTE_RE.findall(text):
        out.append({"method": method.upper(), "path": path})
    return out


def main() -> int:
    found = routes()
    contract_text = CONTRACT.read_text(encoding="utf-8")

    covered, missing = [], []
    for route in found:
        if route["path"] in contract_text:
            covered.append(route)
        else:
            missing.append(route)

    report = {
        "ok": True,
        "totalRoutes": len(found),
        "covered": len(covered),
        "missing": len(missing),
        "coveragePct": round(len(covered) / len(found) * 100, 1) if found else 0.0,
        "missingRoutes": missing,
    }
    out = REPO / "eval" / "runs" / "api-coverage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({k: v for k, v in report.items() if k != "missingRoutes"},
                     ensure_ascii=False, indent=2))
    for route in missing:
        print(f"  MISSING  {route['method']:6s} {route['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())