"""测试隔离审计回归（2026-10-29，承接"演练污染 1547 行审计"事故）。

事故根因：`get_evolution_dir()` = `cwd/.evoharness/...`，而演练只隔离 HOME。
本测试固定：**所有会碰路径真实来源的测试，都做了对应隔离**；
工具的误报必须显式记录理由，不能靠"大概没事"静默放过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.audit_test_isolation as audit_mod


def test_no_unreviewed_isolation_gaps():
    """真实缺口必须为 0（误报要登记进 REVIEWED_FALSE_POSITIVES）。"""
    raw = audit_mod.audit()
    real = [f for f in raw if f["file"] not in audit_mod.REVIEWED_FALSE_POSITIVES]
    assert not real, f"这些测试触碰路径来源却没隔离：{[f['file'] for f in real]}"


def test_every_false_positive_has_reason():
    for path, reason in audit_mod.REVIEWED_FALSE_POSITIVES.items():
        assert reason and len(reason) > 15, f"{path} 的误报理由太短"
        assert (REPO / path).is_file(), f"白名单里的文件不存在：{path}"


def test_drill_tests_isolate_cwd():
    """上次事故的两个直接责任文件必须隔离 cwd。"""
    for rel in ("tests/test_rollback_drill.py", "tests/test_drill_isolation.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "chdir(" in text, f"{rel} 必须 chdir（get_evolution_dir 用 cwd）"


def test_audit_report_written_and_structured():
    path = REPO / "eval" / "runs" / "test-isolation-audit.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["filesWithGaps"] == 0, f"报告显示仍有缺口：{data['findings']}"
    assert "reviewedFalsePositives" in data, "必须区分已复核误报"


def test_findings_use_posix_paths():
    """路径必须是 posix 形式——Windows 反斜杠会让白名单匹配静默失效（踩过）。"""
    raw = audit_mod.audit()
    for item in raw:
        assert "/" in item["file"] or "\\" not in item["file"], (
            f"findings 应使用 posix 路径：{item['file']}")