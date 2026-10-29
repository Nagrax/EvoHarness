"""三处静默丢失修复回归（2026-10-29 批量判读）。

共同点：都属"数据/配置静默丢失"——
自定义 agent 文件消失、评测 JSONL 坏行被吞（样本变少）、token 用量未记账（预算偏小）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SUBAGENT = REPO / "agents" / "subagent.py"
BENCH = REPO / "eval" / "run_benchmark.py"
AGENT = REPO / "agents" / "agent.py"


def test_subagent_load_failure_is_logged():
    text = SUBAGENT.read_text(encoding="utf-8")
    assert "[subagent]" in text, "自定义代理加载失败必须留痕"
    idx = text.find("[subagent]")
    window = text[max(0, idx - 300): idx]
    assert "except Exception as error" in window


def test_subagent_failure_still_skips_only_one():
    """仍然只跳过单个文件，不中断整体加载（保留原设计意图）。"""
    text = SUBAGENT.read_text(encoding="utf-8")
    idx = text.find("[subagent]")
    tail = text[idx: idx + 300]
    assert "raise" not in tail, "单文件失败不得中断整体加载"


def test_benchmark_bad_lines_are_counted(tmp_path, capsys):
    from eval.run_benchmark import _read_predictions

    f = tmp_path / "p.jsonl"
    f.write_text('{"a": 1}\nnot-json\n{"b": 2}\n', encoding="utf-8")
    records = _read_predictions(f)
    out = capsys.readouterr().out
    assert len(records) == 2, "好行必须保留"
    assert "跳过 1 行" in out, f"坏行必须计数提示：{out!r}"


def test_benchmark_clean_file_has_no_noise(tmp_path, capsys):
    from eval.run_benchmark import _read_predictions

    f = tmp_path / "q.jsonl"
    f.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
    _read_predictions(f)
    out = capsys.readouterr().out
    assert "[benchmark]" not in out, f"干净文件不得打印：{out!r}"


def test_token_parse_failure_is_reported():
    text = AGENT.read_text(encoding="utf-8")
    assert "token usage parse failed" in text, "用量解析失败必须可见（否则预算偏小）"
    idx = text.find("token usage parse failed")
    window = text[max(0, idx - 320): idx]
    assert "except (TypeError, ValueError) as error" in window
    # 仍要 return（不改变控制流）
    assert "return" in text[idx: idx + 400]
