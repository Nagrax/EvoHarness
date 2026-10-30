"""文档一致性回归（2026-10-29，目标 5「如实区分」的直接验证）。

动机：110+ 轮后检查点 4200+ 行、HANDOFF/RESULTS/EVIDENCE_INDEX 各自迭代。
**长文档最大的风险不是写错，而是过期**——某个数字在某一轮是对的，
之后事实变了文档没跟上，就变成"文档在撒谎"。

本测试固定：
① 一致性脚本能跑且当前无 issue
② 关键声明（门禁数 / 预算 / backlog）必须与磁盘事实一致
③ HANDOFF 不得残留旧门禁数
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.docs_consistency as dc


def test_script_runs_and_reports():
    proc = subprocess.run([sys.executable, "eval/docs_consistency.py"],
                          cwd=str(REPO), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"存在文档不一致：\n{proc.stdout[-1500:]}"


def test_report_has_no_issues():
    path = REPO / "eval" / "runs" / "docs-consistency.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ok"] is True, f"不一致项：{data['issues']}"


def test_facts_are_nonzero():
    """事实读取不得全为 0（字段名错会导致静默读 0，看起来"一致"）。"""
    facts = dc.facts()
    assert facts.get("gateTests", 0) > 500, f"gateTests 异常：{facts.get('gateTests')}"
    assert facts.get("spent", 0) > 0, f"spent 读成 0（字段名可能不对）：{facts}"
    assert facts.get("remaining", 0) > 0, f"remaining 读成 0：{facts}"
    assert "backlogOpen" in facts


def test_handoff_has_no_stale_gate_count():
    """HANDOFF 里的门禁数必须与最新一次成功 gate 一致。"""
    handoff = Path(r"D:/claudecode/.zcode/workflows/evoharness-optimize-audit/HANDOFF.md")
    if not handoff.is_file():
        return
    text = handoff.read_text(encoding="utf-8")
    gate = dc.latest_gate()
    if not gate:
        return
    tests = min((r.get("counts") or {}).get("tests", 0) for r in gate["runs"])
    assert tests > 0
    # 不得出现"其他测试数 tests /"这种过时写法
    import re

    found = {int(m) for m in re.findall(r"(\d{3,4})\s*tests\s*/", text)}
    # 只拦"小于最近一次成功 gate"的数（那才是真过时）；
    # 允许更大——HANDOFF 声明的是"当前全量"，可能刚加了测试还没成功跑过。
    stale = {n for n in found if n < tests}
    assert not stale, (
        f"HANDOFF 残留旧门禁数 {stale}（最近一次成功 gate 为 {tests}）；"
        "若确实刚加了测试、当前全量更大，请把 HANDOFF 改成更大的值")