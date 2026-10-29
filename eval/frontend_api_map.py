#!/usr/bin/env python3
"""前端→后端 API 对应分析（目标 2 第三层）。

问题：两个方向都可能出错
- **前端调了不存在的 API**（死按钮 / 404）——真实 bug，用户点了没反应
- **后端有 API 但前端没人调**（死端点 / 未接线）——功能做了但用不到

本脚本静态提取前端 JS 里的 `/api/...` 引用，与 server 路由比对，输出两个方向的差集。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
# 前端是单文件 frontend/index.html（内联 JS）；server/static 留作兼容
STATIC_DIRS = [REPO / "frontend", REPO / "server" / "static"]
APP = REPO / "server" / "app.py"

ROUTE_RE = re.compile(r'@app\.(get|post|delete|put|patch)\(\s*["\']([^"\']+)["\']')
API_REF_RE = re.compile(r'["\'`](/api/[A-Za-z0-9/_-]*)["\'`]')
FETCH_RE = re.compile(r'fetch\(\s*[\'"`]([^\'"`?]+)')


def backend_routes() -> dict[str, str]:
    text = APP.read_text(encoding="utf-8")
    return {path: method.upper() for method, path in ROUTE_RE.findall(text)}


def frontend_refs() -> tuple[set[str], list[dict]]:
    refs: set[str] = set()
    sources: list[dict] = []
    for base in STATIC_DIRS:
        if not base.is_dir():
            continue
        for pattern in ("*.js", "*.mjs", "*.html"):
            for path in base.rglob(pattern):
                text = path.read_text(encoding="utf-8", errors="replace")
                found = set(API_REF_RE.findall(text)) | set(FETCH_RE.findall(text))
                found = {f for f in found if f.startswith("/api/")}
                if found:
                    sources.append({"file": str(path.relative_to(REPO)),
                                     "apis": sorted(found)})
                    refs |= found
    return refs, sources


def _normalize(path: str) -> str:
    """把 /api/history?x=1 或模板变量统一成路由形状。"""
    return path.split("?")[0].rstrip("/")


def main() -> int:
    routes = backend_routes()
    refs, sources = frontend_refs()

    route_paths = {_normalize(p) for p in routes}
    norm_refs = {_normalize(r) for r in refs}

    # 前端引用但后端没有（含模板/动态拼接的误报，需人工复核）
    frontend_only = sorted(norm_refs - route_paths)
    # 后端有但前端没引用
    backend_only = sorted(route_paths - norm_refs)
    matched = sorted(norm_refs & route_paths)

    # 扫不到任何前端引用 = 分析无效，不能安静输出 0（会让人以为"查过了"）
    problems = [] if norm_refs else ["frontendRefs-empty: 扫描路径或前端形态变了，结果不可信"]

    report = {
        "ok": not problems,
        "problems": problems,
        "backendRoutes": len(route_paths),
        "frontendRefs": len(norm_refs),
        "matched": len(matched),
        "frontendOnly": frontend_only,
        "backendOnly": backend_only,
        "sources": sources,
    }
    out = REPO / "eval" / "runs" / "frontend-api-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({k: v for k, v in report.items() if k != "sources"},
                     ensure_ascii=False, indent=2))
    if frontend_only:
        print("\n前端引用但后端无此路由（可能是真 404）：")
        for p in frontend_only:
            print("  ", p)
    if backend_only:
        print("\n后端有但前端未引用（可能未接线）：")
        for p in backend_only:
            print("  ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())