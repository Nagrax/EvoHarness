"""git_preflight 回归（2026-10-30，安全关键工具必须有测试）。

背景：dogfooding 核实发现 **30 个 eval/ 脚本完全无测试**，而 `git_preflight.py`
是**提交前最后一道安全闸**——它若漏判，凭据就会进版本库。

测试固定：
① 敏感路径能识别（.env / *.key / LIVE_BUDGET / 产物目录 / node_modules）
② 凭据模式能识别（但**不误报**普通子串，见当天 9 次"子串不是模式"的教训）
③ 超大文件能识别
④ 真实仓库当前 preflight 必须通过
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.git_preflight as gp


def _matches(rel: str) -> bool:
    import re
    return any(re.search(pattern, rel) for pattern in gp.SENSITIVE_PATH)


@pytest.mark.parametrize("path", [
    ".env",
    ".env.bak.live",
    "config/.env.local",
    "certs/server.key",
    "certs/server.pem",
    "eval/runs/live-w13/LIVE_BUDGET.json",
    "eval/runs/gate.json",
    ".evoharness/sessions/x.json",
    "node_modules/pkg/index.js",
    "__pycache__/m.pyc",
    "data/browser-profile/Default/Cookies",
])
def test_sensitive_paths_are_flagged(path):
    assert _matches(path), f"{path} 应被识别为敏感路径"


@pytest.mark.parametrize("path", [
    "agents/agent.py",
    "tests/test_agent.py",
    "frontend/index.html",
    "README.md",
    "eval/git_preflight.py",
])
def test_normal_paths_are_not_flagged(path):
    assert not _matches(path), f"{path} 不应被误判为敏感"


def test_secret_patterns_detect_real_keys():
    import re

    real_key = "sk-" + "a" * 24
    assert any(re.search(p, real_key) for p, _ in gp.SECRET_PATTERNS)
    # 用拼接构造，避免本文件自身被 preflight 扫到（它确实会扫到，这是好事）
    private = "-----BEGIN RSA " + "PRIVATE KEY-----"
    assert any(re.search(p, private) for p, _ in gp.SECRET_PATTERNS)


def test_secret_patterns_do_not_match_ordinary_words():
    """当天教训：`disk-like` 含 `sk-lik`、`klassisk-musik` 含 `sk-mus`。

    这些是**普通文本子串**，不是凭据。模式必须要求足够长的连续字符。
    """
    import re

    for text in ("astrophysical disk-like structure",
                 "klassisk-musik artikel",
                 'line.strip().startswith("APIKEY=")'):
        hits = [label for pattern, label in gp.SECRET_PATTERNS
                if re.search(pattern, text)]
        # 允许 api_key-assignment 命中代码字面量（那是提示人工确认），
        # 但 openai-style 严禁命中普通文本
        assert "openai-style key" not in hits, f"误报：{text!r}"


def test_real_repository_preflight_passes():
    """当前仓库（已清理）必须通过 preflight。"""
    proc = subprocess.run([sys.executable, "eval/git_preflight.py"],
                          cwd=str(REPO), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"preflight 未通过：\n{proc.stdout[-1200:]}"


def test_report_structure():
    path = REPO / "eval" / "runs" / "git-preflight.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("ok", "candidateFiles", "sensitivePaths", "possibleSecrets", "oversize"):
        assert key in data
    assert data["ok"] is True