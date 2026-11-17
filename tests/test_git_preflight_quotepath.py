"""回归：git_preflight 必须能处理中文/非 ASCII 路径。

背景（2026-11-10）：`git status --porcelain` 默认把非 ASCII 路径转义成
`"\\346\\234\\254..."`，旧实现直接 `REPO / path` 再 `relative_to(REPO)`，
遇到 `本地部署指南.md` 直接抛 ValueError，提交前预检整个不可用。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

import eval.git_preflight as gp  # noqa: E402


def test_git_reader_disables_quotepath():
    """真实仓库里存在中文文件名时，candidate_files 不能崩且路径可解析。"""
    files = gp.candidate_files()
    for path in files:
        # 每个候选都必须能相对化到仓库根（回归点）
        path.relative_to(gp.REPO)


def test_quotepath_flag_is_used(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["encoding"] = kwargs.get("encoding")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(gp.subprocess, "run", fake_run)
    gp.git("status", "--porcelain")
    assert "core.quotepath=false" in captured["cmd"]
    assert captured["encoding"] == "utf-8"


def test_non_ascii_status_line_resolves(monkeypatch):
    monkeypatch.setattr(
        gp, "git", lambda *a: ' M "\u672c\u5730\u90e8\u7f72\u6307\u5357.md"\n'
    )
    files = gp.candidate_files()
    assert len(files) == 1
    assert files[0].name == "本地部署指南.md"
    files[0].relative_to(gp.REPO)
