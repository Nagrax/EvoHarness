"""在线进化链路静默修复回归（2026-10-29 判读 agents/agent.py L0059）。

三处 `silent-except-return-none` 让"进化没发生"与"进化失败"无法区分：
- 模块导入失败 → 整个进化被静默跳过
- 使用样本记录失败 → 评测数据缺失无人知
- 外层异常 → 整条链路失败而无提示
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

AGENT = REPO / "agents" / "agent.py"


def test_import_failure_is_reported():
    text = AGENT.read_text(encoding="utf-8")
    assert "Online skill evolution unavailable" in text, "导入失败必须可见"
    idx = text.find("Online skill evolution unavailable")
    window = text[max(0, idx - 400): idx]
    assert "except Exception as error" in window


def test_usage_sample_failure_is_reported():
    text = AGENT.read_text(encoding="utf-8")
    assert "record_usage_samples failed" in text


def test_outer_failure_is_reported():
    """整条在线进化失败不能静默——否则与"没发生"无法区分。"""
    text = AGENT.read_text(encoding="utf-8")
    assert "Online skill evolution failed" in text
    idx = text.find("Online skill evolution failed")
    tail = text[idx: idx + 300]
    assert "return" in tail, "记录后仍要 return（不改变控制流）"


def test_no_silent_pass_left_in_evolution_chain():
    """该函数内不得再有裸 pass 吞掉进化失败。"""
    text = AGENT.read_text(encoding="utf-8")
    start = text.find("from .online_skill_evolution import online_ingest")
    assert start > 0
    end = text.find("async def extract_now", start)
    body = text[start:end]
    # 允许 print_error 之后的 pass（stderr 兜底），但不允许纯粹的静默分支
    silent = [line.strip() for line in body.splitlines() if line.strip() == "pass"]
    assert len(silent) <= 1, f"进化链路仍有多个静默 pass：{len(silent)}"
