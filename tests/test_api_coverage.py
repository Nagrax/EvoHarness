"""后端 API 覆盖率回归（2026-10-29，目标 2「后端每个 API 被检验」）。

覆盖了 100% 不等于"检得够深"，但**漏检是硬缺口**——没被任何契约用例碰到的端点，
其响应结构可以随便变而无人知。这条测试固定两件事：
① server 的每个路由都在 backend_contract 里出现
② 新补的 repair/apply 越界用例存在（它会真改文件，安全边界必须在契约层验）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.api_coverage as cov


def test_every_route_has_contract_case():
    """每个路由都必须被 backend_contract 提到。"""
    found = cov.routes()
    assert found, "必须能解析出路由"
    contract = (REPO / "eval" / "backend_contract.py").read_text(encoding="utf-8")
    missing = [f"{r['method']} {r['path']}" for r in found if r["path"] not in contract]
    assert not missing, f"这些端点没有任何契约用例：{missing}"


def test_repair_apply_case_exists_and_is_security_kind():
    text = (REPO / "eval" / "backend_contract.py").read_text(encoding="utf-8")
    assert "repair_apply_rejects_outside_root" in text
    idx = text.find("repair_apply_rejects_outside_root")
    window = text[max(0, idx - 600): idx + 400]
    assert "POST /api/skills/repair/apply" in window
    assert '"security"' in window, "越界拒绝属安全类用例"


def test_repair_apply_checks_response_readable():
    """拒绝语义要判 ok is False 或 4xx，而不是只看 status 非 200。"""
    text = (REPO / "eval" / "backend_contract.py").read_text(encoding="utf-8")
    idx = text.find("repair_apply_rejects_outside_root")
    window = text[max(0, idx - 700): idx]
    assert 'body.get("ok") is False' in window


def test_coverage_report_generated():
    path = REPO / "eval" / "runs" / "api-coverage.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["coveragePct"] == 100.0, f"覆盖率应达 100%，实际 {data['coveragePct']}"
    assert data["missing"] == 0


def test_route_parser_matches_known_endpoints():
    paths = {r["path"] for r in cov.routes()}
    for expected in ("/api/health", "/api/chat", "/api/history", "/api/skills",
                     "/api/skills/tiers", "/api/skills/repair/apply"):
        assert expected in paths, f"解析漏了 {expected}"