"""闭环信号源健康检查回归（2026-10-29）。

动机（延续"检查器会失效"）：12 个信号源里若某个从不产出 finding，
"它很干净"与"它坏了"外观相同。

本轮实测结论：
- `memory` / `calibration` / `skillCombo` / `judgeProbe` 无产出但**脚本能跑** → 真干净
- `failureStability` **没有闭环内脚本**（loop 只读产物）→ 必须查产物新鲜度
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.loop_source_health as health


def test_health_report_passes():
    proc = subprocess.run([sys.executable, "eval/loop_source_health.py"],
                          cwd=str(REPO), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, f"健康检查失败：\n{proc.stdout[-1200:]}"


def test_report_distinguishes_clean_from_broken():
    path = REPO / "eval" / "runs" / "loop-source-health.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data["perSource"]:
        assert entry["state"] in {"working", "clean", "broken"}
        if entry["state"] == "clean":
            assert entry["scriptRunnable"], "clean 的前提是脚本能跑（否则是 broken）"


def test_failure_stability_checked_by_artifact_not_script():
    """它没有闭环内脚本，必须走产物新鲜度检查。"""
    path = REPO / "eval" / "runs" / "loop-source-health.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    checks = {c["source"] for c in data.get("artifactChecks") or []}
    assert "failureStability" in checks, "必须检查其产物新鲜度"
    assert "failureStability" not in health.SCRIPT_MAP, "它没有独立脚本"


def test_script_map_paths_exist():
    for source, scripts in health.SCRIPT_MAP.items():
        for rel in scripts:
            assert (REPO / rel).is_file(), f"{source} 的脚本不存在：{rel}"


def test_no_script_sources_are_documented():
    """没有独立脚本的源必须显式登记（防"从没检查过"）。"""
    assert health.NO_SCRIPT, "NO_SCRIPT 不能为空"
    for source in health.NO_SCRIPT:
        assert isinstance(source, str) and source