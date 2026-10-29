"""API 契约**深度**回归（2026-10-29，目标 2「后端每个 API 被检验」的第二层）。

覆盖率 100% 只说明"碰到过"。这层固定的是：**每个端点都有负向或安全用例**
（只验 happy 的端点，错误输入/越权/泄漏完全无防线）。

唯一例外是 `GET /`（静态首页，返回 HTML，无输入无状态）——已显式列为合理例外，
不是"忘了补"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.api_cases_depth as depth

# 显式合理例外（静态首页：无输入、无状态、无敏感数据）
ALLOWED_HAPPY_ONLY = {"GET /"}


def test_every_endpoint_has_non_happy_case():
    by_endpoint = depth.collect()
    weak = []
    for endpoint, cases in by_endpoint.items():
        kinds = {c["kind"] for c in cases}
        if kinds <= {"happy"} and endpoint not in ALLOWED_HAPPY_ONLY:
            weak.append(endpoint)
    assert not weak, f"这些端点只有 happy 用例（缺负向/安全防线）：{weak}"


def test_leak_cases_exist_for_listing_endpoints():
    text = (REPO / "eval" / "backend_contract.py").read_text(encoding="utf-8")
    for name in ("no_secret_leak_skills", "no_secret_leak_tiers"):
        assert name in text, f"缺泄漏检查用例 {name}"
        idx = text.find(name)
        window = text[max(0, idx - 400): idx + 300]
        assert '"security"' in window
        # 判据必须包含凭据与绝对路径两类
        assert "api_key" in text and "C:\\\\" in text


def test_security_cases_use_literal_endpoints():
    """**安全类**用例必须用字面量 endpoint——f-string 会让静态分析漏计、
    也让"这个用例到底测哪个端点"变得不可读。

    （循环里批量产生的 happy 用例允许 f-string：它们的端点由循环变量决定，
    深度报告里能通过循环上限核对。）
    """
    text = (REPO / "eval" / "backend_contract.py").read_text(encoding="utf-8")
    for name in ("repair_apply_rejects_outside_root", "no_secret_leak_skills",
                 "no_secret_leak_tiers", "repair_rejects_outside_root"):
        idx = text.find(name)
        assert idx > 0, f"缺安全用例 {name}"
        # 该用例所在行必须是字面量 endpoint
        line_start = text.rfind("\n", 0, idx) + 1
        line_end = text.find("\n", idx)
        line = text[line_start: line_end]
        assert "f\"" not in line, f"{name} 的 record 调用不得用 f-string：{line.strip()[:80]}"


def test_depth_report_kind_distribution():
    path = REPO / "eval" / "runs" / "api-cases-depth.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    dist = data["kindDistribution"]
    assert dist.get("happy", 0) > 0 and dist.get("security", 0) > 0, dist
    assert data["totalCases"] >= 25, f"用例总数不应回退：{data['totalCases']}"


def test_only_static_root_may_be_happy_only():
    """异常必须可控：薄弱清单里只允许出现 GET /。"""
    by_endpoint = depth.collect()
    weak = {ep for ep, cases in by_endpoint.items()
            if {c["kind"] for c in cases} <= {"happy"}}
    assert weak <= ALLOWED_HAPPY_ONLY, f"出现未声明的薄弱端点：{weak - ALLOWED_HAPPY_ONLY}"