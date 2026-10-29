"""声明审计回归：守卫必须能拦住违规，而不是空跑通过。

真实约束：对外文档出现 D 级结论即不合格（README/RESULTS 是交付物）。
本文件用"故意写坏的文档"验证 R1/R2/R3 三条规则真的会失败。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.claim_audit as audit


def test_real_deliverables_pass():
    """真实交付物必须通过（否则说明文档违反口径）。"""
    problems = (audit.check_readme_grades() + audit.check_results_section()
                + audit.check_no_positive_claim_for_unclaimable())
    assert problems == [], f"交付物违规：{problems}"


def test_unclaimable_loaded_from_noise_gate():
    if not audit.NOISE.is_file():
        pytest.skip("噪声门产物不存在")
    items = audit.load_not_claimable()
    assert "soft:recallRate" in items
    assert items["soft:recallRate"].startswith("not-claimable")


def test_missing_grade_column_is_caught(tmp_path, monkeypatch):
    fake = tmp_path / "README.md"
    fake.write_text("| 层 | 结果 | 对照 |\n| --- | --- | --- |\n"
                    "| **bench** | 49.1 | 无等级列 |\n", encoding="utf-8")
    monkeypatch.setattr(audit, "README", fake)
    problems = audit.check_readme_grades()
    assert any(p.startswith("readme-row-missing-grade") for p in problems), (
        f"缺等级列必须被检出，实际 {problems}")


def test_invalid_grade_letter_is_caught(tmp_path, monkeypatch):
    fake = tmp_path / "README.md"
    fake.write_text("| 层 | 结果 | 等级 | 对照 |\n| --- | --- | --- | --- |\n"
                    "| **bench** | 49.1 | Z | x |\n", encoding="utf-8")
    monkeypatch.setattr(audit, "README", fake)
    problems = audit.check_readme_grades()
    assert any("invalid-grade" in p for p in problems), problems


def test_missing_grade_section_is_caught(tmp_path, monkeypatch):
    fake = tmp_path / "RESULTS.md"
    fake.write_text("# 结果\n\n没有分级节。\n", encoding="utf-8")
    monkeypatch.setattr(audit, "RESULTS", fake)
    problems = audit.check_results_section()
    assert "results-missing-grade-section" in problems


def test_positive_claim_on_unclaimable_is_caught(tmp_path, monkeypatch):
    """把"方向反号"的召回维度写成"提升召回"必须被拦下。"""
    fake = tmp_path / "RESULTS.md"
    fake.write_text("# 结果\n\n## 六、证据强度分级\n\n- 生命周期标注提升召回 3.3pp\n",
                    encoding="utf-8")
    monkeypatch.setattr(audit, "RESULTS", fake)
    fake_readme = tmp_path / "README.md"
    fake_readme.write_text("# readme\n", encoding="utf-8")
    monkeypatch.setattr(audit, "README", fake_readme)
    problems = audit.check_no_positive_claim_for_unclaimable()
    assert any("positive-claim-on-unclaimable-recall" in p for p in problems), problems


def test_negated_claim_is_allowed(tmp_path, monkeypatch):
    """显式否定的表述（不可宣称/方向反号）必须放行，否则会逼人删正确内容。"""
    fake = tmp_path / "RESULTS.md"
    fake.write_text("# 结果\n\n## 六、证据强度分级\n\n"
                    "- 软标注的召回收益不可宣称（方向反号）\n", encoding="utf-8")
    monkeypatch.setattr(audit, "RESULTS", fake)
    fake_readme = tmp_path / "README.md"
    fake_readme.write_text("# readme\n", encoding="utf-8")
    monkeypatch.setattr(audit, "README", fake_readme)
    assert audit.check_no_positive_claim_for_unclaimable() == []
