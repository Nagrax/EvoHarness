"""交接文档完整性守护：链接有效、引用文件存在、关键章节不缺。

为什么需要这条测试：交接文档最容易"写完全对、过三个月全错"。
链接指向已删除的文件、章节被改写但引用没跟上，接手者会照着错文档操作，
比没有文档更糟。这里把"文档可用"变成可回归的断言。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "docs" / "AGENT-HANDOFF.md"
DIFF_DOC = ROOT / "docs" / "与原版的差异.md"
TWIN_DOC = ROOT / "docs" / "数字分身-交接文档.md"
MANUAL = ROOT / "docs" / "飞书机器人使用手册.md"
HUMAN_GATES = ROOT / "docs" / "人工门操作手册.md"


def _run_verifier() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "eval" / "verify_handoff_docs.py")],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


def test_handoff_docs_exist():
    for doc in (HANDOFF, DIFF_DOC, TWIN_DOC, MANUAL, HUMAN_GATES):
        assert doc.is_file(), f"交接文档缺失: {doc.name}"


def test_human_gates_playbook_is_actionable():
    """人工门手册必须可照做：命令、词表、口径、产物位置都不能丢。

    为什么守护：这四项（H1~H4）都靠人执行，文档一旦写成"理论上应该……"，
    执行者就会停在半路；这里钉住可执行要素。
    """
    text = HUMAN_GATES.read_text(encoding="utf-8")
    required = [
        "eval/human_gate.py",      # 统一入口
        "h1-check",                # 三个核验子命令
        "h2-check",
        "h3-check",
        "SKILL_ISSUE",             # 固定五类词表（H2 判定口径）
        "UNCERTAIN",
        "net_seconds",             # H1 计时字段
        "eval/runs/human-gates",   # 产物位置（且不入库）
    ]
    for item in required:
        assert item in text, f"人工门手册缺少可执行要素：{item}"
    # 必须写明"不伪造结论"，否则容易被当成已完成
    assert "不伪造" in text or "不代劳" in text or "不得宣称" in text
    # 产物目录必须在 .gitignore 覆盖范围内，避免运行数据入库
    assert "eval/runs/" in text


def test_human_gate_tool_module_is_importable():
    """手册指向的工具必须真实存在且可导入，否则文档即谎言。"""
    r = subprocess.run(
        [sys.executable, "-X", "utf8", "-c",
         "import sys; sys.path.insert(0,'.'); import eval.human_gate as m; "
         "assert callable(m.h1_check) and callable(m.h2_check) and callable(m.h3_check)"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, f"eval/human_gate.py 不可导入：{r.stderr[-500:]}"


def test_handoff_links_are_valid():
    """所有本地链接必须指向真实存在的文件。"""
    result = _run_verifier()
    assert result.returncode == 0, f"文档校验失败：\n{result.stdout}\n{result.stderr}"


def test_handoff_covers_required_sections():
    """接手必知的关键内容不能丢。"""
    text = HANDOFF.read_text(encoding="utf-8")
    required = [
        "数字分身",          # 核心定位
        "授权边界",          # 最容易违反的约束
        "已知未做",          # 诚实边界
        "别踩的坑",          # 踩坑清单
        "关键文件索引",       # 能定位代码
    ]
    for item in required:
        assert item in text, f"AGENT-HANDOFF 缺少关键章节：{item}"


def test_handoff_mentions_upstream_gap():
    """必须明确告知：从 GitHub 克隆的版本不含本地能力。"""
    text = HANDOFF.read_text(encoding="utf-8")
    assert "GitHub" in text and ("上游" in text or "克隆" in text), (
        "AGENT-HANDOFF 必须说明上游版与本地版的区别"
    )


def test_readme_points_to_handoff():
    """README 是 agent 的第一入口，必须链接到交接文档。"""
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "docs/AGENT-HANDOFF.md" in text, (
        "README 必须包含指向 AGENT-HANDOFF 的入口，否则 agent 找不到"
    )


def test_feishu_readme_marks_local_changes():
    """飞书子 README 是上游原文，必须标注本地变更，否则误导接手者。"""
    text = (ROOT / "feishu-daily-agent" / "README.md").read_text(encoding="utf-8")
    assert "LOCAL-FORK-NOTICE" in text
    assert "【本地变更】" in text or "【本地新增】" in text


def test_guarding_tests_are_referenced():
    """交接文档必须点名"别改弱"的守护测试。"""
    text = HANDOFF.read_text(encoding="utf-8")
    for name in (
        "test_digital_twin_boundary.py",
        "test_digital_twin_ownership.py",
        "test_twin_send_identity.py",
    ):
        assert name in text, f"交接文档应点名守护测试 {name}"


def test_diff_doc_states_baseline():
    """差异文档必须写明对比基准，否则数字无从复核。"""
    text = DIFF_DOC.read_text(encoding="utf-8")
    assert "aa0e781" in text, "差异文档必须注明上游基准提交"
    assert "561feed" in text, "差异文档必须注明本地基线提交"
