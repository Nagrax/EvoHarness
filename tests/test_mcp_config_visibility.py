"""MCP 坏配置必须可见的回归（2026-10-29 判读 agents/mcp_client.py:567）。

判读结论：跳过坏配置的设计方向对（不拖垮 Agent 启动），但完全静默会让用户
以为 MCP server 正常工作——实际一个都没加载。属"安全降级但缺可见性"。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

MCP = REPO / "agents" / "mcp_client.py"


def test_config_errors_is_initialized_per_load():
    text = MCP.read_text(encoding="utf-8")
    assert "self.config_errors = []" in text, "每次加载前必须重置错误列表"
    assert "__dict__.setdefault" not in text, "不得用 __dict__.setdefault hack"


def test_bad_config_is_recorded_and_printed():
    text = MCP.read_text(encoding="utf-8")
    assert "self.config_errors.append({" in text
    assert "[mcp] config skipped" in text, "必须打印可见提示"
    idx = text.find("self.config_errors.append({")
    window = text[idx: idx + 400]
    assert '"path"' in window and '"error"' in window, "必须记录路径与原因"


def test_skip_still_does_not_raise():
    """跳过语义必须保留：一个坏配置不能拖垮整个加载。"""
    text = MCP.read_text(encoding="utf-8")
    idx = text.find("self.config_errors.append({")
    assert idx > 0
    before = text[max(0, idx - 600): idx]
    assert "except Exception as error" in before
    after = text[idx: idx + 500]
    assert "raise" not in after, "记录后不得再抛（否则等于没跳过）"


def test_load_configs_resets_between_calls():
    """多次加载不得累积上一次的错误（否则诊断会越来越脏）。"""
    text = MCP.read_text(encoding="utf-8")
    init_idx = text.find("self.config_errors = []")
    load_idx = text.find("def _load_configs")
    assert load_idx > 0 and init_idx > load_idx, (
        "初始化必须在 _load_configs 内（每次加载重置），不能只在 __init__")
