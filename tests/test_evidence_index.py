"""证据索引与 document-only 规则回归（W19 ④）。

真实发现（2026-10-29）：`fold-ablation` 的证据指向 `eval/RESULTS.md` 自身——
"文档说做过"，而 `eval/runs/gaia-nofold/` 产物并不存在。这类自证不能算可核对证据。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.evidence_index as ei
from eval.final_audit import CLAIMS, audit


def test_audit_rejects_document_only_evidence_for_ab():
    """A/B 级若证据全是 md/txt，必须显式 documentOnly，否则报错。"""
    result = audit()
    assert not any("document-only-evidence" in p for p in result["problems"]), (
        f"存在未声明的文档自证：{result['problems']}")


def test_document_only_claims_are_grade_c():
    """声明 documentOnly 的主张必须降到 C（不可核对）。"""
    for claim in CLAIMS:
        if claim.get("documentOnly"):
            assert claim["grade"] == "C", (
                f"{claim['id']} 是文档自证却标了 {claim['grade']}")


def test_index_covers_every_claim():
    index = ei.build_index()
    assert index["total"] == len(CLAIMS), "索引必须覆盖全部主张"
    assert index["ok"], f"索引有问题：{index['problems']}"


def test_index_records_sha256_for_every_evidence_file():
    index = ei.build_index()
    for entry in index["claims"]:
        for item in entry["evidence"]:
            assert len(item["sha256"]) == 64, "必须是完整 sha256"
            assert item["bytes"] > 0


def test_index_marks_claims_without_rerun():
    """没有复跑命令的主张必须能被识别（不能假装都能复跑）。"""
    index = ei.build_index()
    missing = [c["id"] for c in index["claims"] if not c["rerun"]]
    # 允许有，但数量必须与统计一致
    assert index["withRerun"] == index["total"] - len(missing)


def test_rerun_commands_point_to_existing_files():
    """复跑命令里引用的脚本必须真实存在（防索引写了跑不通的命令）。"""
    import re

    for claim_id, command in ei.RERUN.items():
        for rel in re.findall(r"(eval/[\w./-]+\.py)", command):
            assert (REPO / rel).is_file(), f"{claim_id} 的复跑脚本不存在：{rel}"


def test_check_mode_detects_no_drift():
    """--check 在当前状态下必须通过（索引与证据一致）。"""
    if not ei.JSON_OUT.is_file():
        return
    proc = subprocess.run([sys.executable, str(REPO / "eval" / "evidence_index.py"), "--check"],
                          capture_output=True, cwd=str(REPO), check=False)
    payload = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    assert payload.get("ok") is True, payload
