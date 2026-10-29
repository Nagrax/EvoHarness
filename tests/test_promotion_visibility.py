"""晋级失败可见性回归（2026-10-29 判读 agents/online_skill_eval.py:2224）。

判读结论：`apply_eval_promotions` 的 fail-open 设计正确（分区故障不阻塞评测主流程），
但完全静默会让"晋级没生效、报告却正常"被误读为技能已转正。
处置：保持 fail-open，把错误写进返回的报告。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SRC = REPO / "agents" / "online_skill_eval.py"


def test_promotion_error_recorded_in_report():
    text = SRC.read_text(encoding="utf-8")
    assert "promotion_errors" in text, "晋级失败必须写进报告"
    assert "promotion failed" in text, "必须打印一行提示"
    idx = text.find('report.setdefault("promotion_errors", []).append(detail)')
    assert idx > 0, "必须 append 到 report（返回值可见），而非只打印"


def test_fail_open_semantics_preserved():
    """fail-open 不得改成 raise：分区故障不能阻塞评测主流程。"""
    text = SRC.read_text(encoding="utf-8")
    idx = text.find('report.setdefault("promotion_errors"')
    assert idx > 0
    window = text[idx: idx + 260]
    assert "raise" not in window, "记录后不得再抛（否则破坏 fail-open）"
    after = text[idx: text.find("return report", idx)]
    assert "return report" in text[text.find("except Exception as error", idx - 400):][:600]


def test_report_still_returned_on_failure():
    """异常路径必须仍然 return report（调用方拿到的是报告，不是异常）。"""
    text = SRC.read_text(encoding="utf-8")
    except_idx = text.find("except Exception as error:  # noqa: BLE001\n        # fail-open 保留")
    assert except_idx > 0, "未找到修复后的 except 分支"
    tail = text[except_idx: except_idx + 500]
    assert "return report" in tail
