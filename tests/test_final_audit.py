"""W19 最终声明审计回归：每条对外主张必须能点到证据。

真实踩坑：审计器初版查 `ci95`/`ci` 字段，而 summary.json 实际是
`pass_at_1_ci95` → 误报两条 A 级主张缺 CI。**审计器自己也要被审计**。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.final_audit import CLAIMS, audit


def test_real_audit_passes():
    result = audit()
    assert result["ok"], f"当前主张审计未通过：{result['problems']}"


def test_grade_distribution_is_explicit():
    result = audit()
    grades = result["byGrade"]
    assert grades["A"] >= 5, "必须有若干 A 级可对外声明的主张"
    assert grades["D"] >= 1, "必须有 D 级不可宣称项被显式列出"
    assert set(grades) == {"A", "B", "C", "D"}, "四个等级都要出现在清单里"


def test_every_ab_claim_has_evidence_file():
    for claim in CLAIMS:
        if claim["grade"] in {"A", "B", "C"}:
            assert claim["evidence"], f"{claim['id']} 缺证据路径"
            for rel in claim["evidence"]:
                assert (REPO / rel).is_file(), f"{claim['id']} 证据不存在：{rel}"


def test_d_grade_claims_carry_no_evidence_and_forbid_positive():
    """D 级主张故意没有证据路径——它们就是要被禁止宣称的东西。"""
    for claim in CLAIMS:
        if claim["grade"] == "D":
            assert claim.get("forbidPositive") is True
            assert not claim["evidence"], "D 级不应有正向证据（有就不该是 D 级）"


def test_ci_field_name_matches_real_summary():
    """CI 字段名必须与真实 summary 一致，否则审计误报。"""
    import json

    summary = json.loads((REPO / "eval/runs/gaia/summary.json").read_text(encoding="utf-8"))
    assert "pass_at_1_ci95" in summary, "真实字段名变了，审计器需同步"
    result = audit()
    assert not any("missing-ci95" in p for p in result["problems"])


def test_not_claimable_surface_is_exposed():
    """不可宣称项必须被审计器显式列出，不能藏在清单里。"""
    result = audit()
    assert result["notClaimable"], "噪声门判定为不可宣称的指标必须出现在审计结果里"
    assert all(v.startswith("not-claimable") for v in result["notClaimable"].values())
