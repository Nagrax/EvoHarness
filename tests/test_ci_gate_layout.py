"""DE-01 CI 布局回归：产品/证据分层、手动触发、矩阵，不用 continue-on-error。

背景（复审）：旧的根套件 job 在 clean runner 上必然因冻结证据类文件报红，
与绿色 product job 形成两套结论。本测试固定统一门禁布局，防止回退。
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]
CI = REPO / ".github" / "workflows" / "ci.yml"


def _text() -> str:
    return CI.read_text(encoding="utf-8")


def test_ci_is_manually_dispatchable():
    assert "workflow_dispatch:" in _text()


def test_product_job_uses_unified_gate_not_raw_full_suite():
    text = _text()
    assert "eval/digital_employee_gate.py --jobs product" in text
    # 旧的两套口径：clean runner 上全量根套件（含冻结证据文件）会红
    assert "python -m pytest tests/ -v" not in text
    assert "python -m pytest tests/ -q" not in text


def test_evidence_job_only_runs_with_archives_and_never_masks_failures():
    text = _text()
    assert "steps.evidence.outputs.present == 'true'" in text
    assert "missing_evidence" in text
    assert "continue-on-error:" not in text


def test_matrix_covers_windows_linux_and_two_python_versions():
    text = _text()
    for token in ('ubuntu-latest', 'windows-latest', '"3.11"', '"3.12"'):
        assert token in text
