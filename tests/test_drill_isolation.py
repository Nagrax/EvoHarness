"""演练隔离回归（2026-10-29 发现的真实污染事故）。

事故：`rollback_drill.py` 隔离了 HOME，但 `get_evolution_dir()` 用的是
**cwd** → 演练把 **1547 行**记录写进真实 `tier_audit.jsonl`。
清理时才发现（清理后才跑通 CLI 的 list）。

本测试固定：演练前后，**真实仓库的审计日志行数不变**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

AUDIT = REPO / ".evoharness" / "skill-evolution" / "tier_audit.jsonl"


def _audit_rows() -> int:
    if not AUDIT.is_file():
        return 0
    return len([l for l in AUDIT.read_text(encoding="utf-8").splitlines() if l.strip()])


def test_drill_does_not_touch_real_audit_log(monkeypatch):
    """跑一次演练，真实审计日志行数必须不变（隔离含 cwd）。"""
    before = _audit_rows()
    from eval.rollback_drill import main as drill_main

    # main() 内部用 argparse 解析 sys.argv；不换掉会吃掉 pytest 的参数
    monkeypatch.setattr(sys, "argv", ["rollback_drill"])
    rc = drill_main()
    after = _audit_rows()
    assert rc == 0, "演练本身应通过"
    assert after == before, (
        f"演练污染了真实审计日志：{before} → {after} 行"
        f"（隔离必须同时 chdir，因为 get_evolution_dir() 用 cwd）")


def test_drill_source_isolates_cwd():
    src = (REPO / "eval" / "rollback_drill.py").read_text(encoding="utf-8")
    assert "os.chdir(home)" in src, "演练必须 chdir 到临时目录"
    assert "os.chdir(original_cwd)" in src, "必须恢复原 cwd（否则污染后续测试）"


def test_drill_artifact_has_all_steps():
    path = REPO / "eval" / "runs" / "rollback-drill.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ok"] is True
    names = [s["step"] for s in data["steps"]]
    for required in ("bad-rolled-back", "audit-logged", "disabled-means-no-rollback"):
        assert required in names, f"演练缺少步骤 {required}"


def test_tier_audit_stays_clean_of_drill_entries():
    """真实审计里不应再有 drill-* 残留（已清理，防回归）。"""
    if not AUDIT.is_file():
        return
    rows = [json.loads(l) for l in AUDIT.read_text(encoding="utf-8").splitlines() if l.strip()]
    drills = [r for r in rows if str(r.get("skill") or "").startswith("drill-")]
    assert not drills, f"审计里又有 drill-* 残留：{len(drills)} 行"