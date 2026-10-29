"""审计器自检回归（2026-10-29，承接门禁 baseline 事故）。

事故教训：**检查器自己会失效**。门禁的 baseline `nodeIds` 为空 →
"baseline 项是否都跑了"退化成"空集都跑完了" → 永远通过。

本测试固定：
① `auditor_selfcheck.py` 当前必须 8/8 通过（任一失败即说明某个审计器失效）
② 自检必须覆盖 5 个关键审计器
③ 自检已接进 `loop_autorun`（否则失效无人知晓）
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_selfcheck_script_passes():
    proc = subprocess.run([sys.executable, "eval/auditor_selfcheck.py"],
                          cwd=str(REPO), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (
        f"审计器自检失败：\n{proc.stdout[-1200:]}\n{proc.stderr[-600:]}")


def test_selfcheck_report_structure():
    path = REPO / "eval" / "runs" / "auditor-selfcheck.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ok"] is True
    checks = [r["check"] for r in data["results"]]
    assert len(checks) >= 8, f"自检项偏少：{checks}"
    for auditor in ("final_audit", "claim_audit", "noise_gate",
                    "evidence_index", "failure_stability"):
        assert any(auditor in c for c in checks), f"自检未覆盖 {auditor}"


def test_selfcheck_wired_into_autorun():
    src = (REPO / "eval" / "loop_autorun.py").read_text(encoding="utf-8")
    assert "auditor_selfcheck.py" in src, "自检必须接进闭环例行"
    assert '"name": "auditorSelfcheck"' in src


def test_selfcheck_precedes_collect():
    """自检应在 collect 之前（先确认检查器工作，再看它报告什么）。"""
    src = (REPO / "eval" / "loop_autorun.py").read_text(encoding="utf-8")
    i_self = src.find("auditor_selfcheck.py")
    i_collect = src.find('"eval/loop.py", "collect"')
    assert 0 < i_self < i_collect, "自检必须先于 collect"