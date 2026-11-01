"""工具可达性审计回归（2026-10-31）。

固定三件事：
① 所有**评测工具**都能被 `--help` 安全调起（broken 必须为 0）
② 非评测脚本（数学研究残留）**单独归类**，不混进"评测工具坏了"
③ 审计必须**跳过自身**（否则 `--help` 会递归触发整轮审计）
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.tool_reachability as reach


def test_audit_runs_and_reports_no_broken_eval_tools():
    proc = subprocess.run([sys.executable, "eval/tool_reachability.py"],
                          cwd=str(REPO), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, f"存在跑不起来的评测工具：\n{proc.stdout[-1500:]}"


def test_report_structure():
    path = REPO / "eval" / "runs" / "tool-reachability.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("ok", "totalScripts", "healthy", "unusedButRuns",
                "broken", "nonEvalBrokenCount"):
        assert key in data, f"缺字段 {key}"
    assert data["ok"] is True
    assert data["broken"] == 0, f"仍有 broken：{data['brokenDetail']}"


def test_self_is_skipped():
    """审计必须跳过自身，否则 --help 会递归。"""
    assert "SELF_NAME" in Path(reach.__file__).read_text(encoding="utf-8")
    scripts = [p.stem for p in (REPO / "eval").glob("*.py")]
    assert reach.SELF_NAME in scripts


def test_non_eval_scripts_are_classified():
    """数学研究残留必须与非评测脚本清单对得上。"""
    assert reach.NON_EVAL_SCRIPTS, "分类清单不能为空"
    for name in ("final_check", "compute_irreps", "s7_dimensions"):
        assert name in reach.NON_EVAL_SCRIPTS


def test_usage_help_works_for_cli_tools():
    """几个常用 CLI 必须能 --help 且 exit 0。"""
    for script in ("eval/curve_summary.py", "eval/loop_context.py",
                   "eval/triage_file.py", "eval/loop_source_health.py"):
        proc = subprocess.run([sys.executable, script, "--help"],
                              cwd=str(REPO), capture_output=True, text=True, timeout=90)
        assert proc.returncode == 0, f"{script} --help 失败：{proc.stderr[-300:]}"
        assert "用法" in proc.stdout, f"{script} --help 未输出用法"