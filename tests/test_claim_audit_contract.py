"""Evidence grades govern numerical claims, without requiring invented results."""
import eval.claim_audit as audit


def test_product_readme_without_benchmark_claims_is_valid(tmp_path, monkeypatch):
    path = tmp_path / 'README.md'
    path.write_text('# EvoHarness\n\n组织、工作和验收。\n', encoding='utf-8')
    monkeypatch.setattr(audit, 'README', path)
    assert audit.check_readme_grades() == []


def test_prose_benchmark_claim_still_requires_evidence_grade(tmp_path, monkeypatch):
    path = tmp_path / 'README.md'
    path.write_text('# EvoHarness\n\nGAIA 准确率 95%。\n', encoding='utf-8')
    monkeypatch.setattr(audit, 'README', path)
    assert audit.check_readme_grades() == ['readme-benchmark-claim-missing-grade']
