"""前端↔后端 API 对应回归（2026-10-29，目标 2 第三层）。

发现（本轮实测）：
- 前端引用 8 个 API，**全部命中后端路由**（没有"调不存在的 API"这种死按钮）
- 后端 3 个 API **前端完全没引用**：`/api/skills/tiers`、`/api/skills/tiers/action`、
  `/api/eval/skills`；且 `skill_tiers.py` 无 CLI、`eval/ui/buttons.mjs` 也不提 tiers
  → **这三个功能只有 HTTP 入口，用户只能 curl**

这条测试把上述事实固定下来：不是"以为接线了"，是**明确未接线**。
哪天补了前端入口，这条测试会失败并提醒改写结论。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.frontend_api_map as fam

# 当前明确"后端有、前端无"的端点（不含根路径 /）
KNOWN_UNWIRED = {
    "/api/eval/skills",
    "/api/skills/tiers",
    "/api/skills/tiers/action",
}


def test_scan_is_not_empty():
    """扫不到任何引用时分析无效——必须报警而不是安静输出 0。"""
    refs, sources = fam.frontend_refs()
    assert refs, "前端 API 引用为空：扫描路径或前端形态变了，结论不可信"
    assert sources, "必须记录来源文件"


def test_no_frontend_calls_missing_backend_route():
    """前端不得引用后端不存在的路由（否则就是死按钮）。"""
    routes = {fam._normalize(p) for p in fam.backend_routes()}
    refs = {fam._normalize(r) for r in fam.frontend_refs()[0]}
    missing = sorted(refs - routes)
    assert not missing, f"前端调用了不存在的 API：{missing}"


def test_unwired_endpoints_match_known_set():
    """后端未被前端引用的端点集合 = 已知的 3 个（+ 根路径）。"""
    routes = {fam._normalize(p) for p in fam.backend_routes()}
    refs = {fam._normalize(r) for r in fam.frontend_refs()[0]}
    unwired = routes - refs - {""}
    assert unwired == KNOWN_UNWIRED, (
        f"未接线集合变化了：实际 {sorted(unwired)}，"
        f"预期 {sorted(KNOWN_UNWIRED)}（若补了前端入口，请更新本测试与检查点结论）")


def test_tiers_have_no_other_entry_point():
    """这三项目前也没有 CLI 入口——即"只能 curl"。"""
    tiers_src = (REPO / "agents" / "skill_tiers.py").read_text(encoding="utf-8")
    assert "argparse" not in tiers_src, "若 skill_tiers 新增了 CLI，请更新本条与检查点"
    buttons = REPO / "eval" / "ui" / "buttons.mjs"
    if buttons.is_file():
        text = buttons.read_text(encoding="utf-8")
        assert "tier" not in text.lower(), "若 UI 测试开始覆盖 tiers，请更新本条"


def test_map_report_written():
    path = REPO / "eval" / "runs" / "frontend-api-map.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["matched"] >= 8, f"前端匹配数不应回退：{data['matched']}"
    assert data["frontendOnly"] == [], "前端不得引用不存在的 API"
    assert data["ok"] is True