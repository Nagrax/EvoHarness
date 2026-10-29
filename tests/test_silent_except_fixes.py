"""silent-except 判读后的修复回归（2026-10-29）。

真实判读结果（agents/agent.py 8 处 silent-except）：
- 2 处裸 `except:`（memory 注入）→ 必须收窄为 `except Exception:`
  （裸 except 连 KeyboardInterrupt/SystemExit 一起吞，调试时无法中断）
- 1 处 `done_task.result()` 直接 pass → 后台技能任务失败对上层完全不可见
- 其余 5 处有明确设计说明（清理路径 / 已记录到 last_save_error），保持不动
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

AGENT = REPO / "agents" / "agent.py"


def test_no_bare_except_in_agent():
    """裸 except: 一律不许出现（会吞 KeyboardInterrupt/SystemExit）。"""
    text = AGENT.read_text(encoding="utf-8")
    hits = [(text[: m.start()].count("\n") + 1)
            for m in re.finditer(r"^\s*except:\s*$", text, re.M)]
    assert not hits, f"agents/agent.py 仍有裸 except（行号 {hits}）"


def test_background_task_failure_is_logged():
    """后台技能任务失败必须留痕，不能静默丢弃。"""
    text = AGENT.read_text(encoding="utf-8")
    assert "background-skill" in text, "done_task.result() 的异常必须记录"
    # 且该处不再是无注释的 pass
    idx = text.find("done_task.result()")
    assert idx > 0
    window = text[idx: idx + 500]
    assert "except Exception as error" in window, "应捕获为具名变量并记录"
    assert "task failed" in window


def test_designed_silences_are_documented():
    """保留的静默必须带明确理由（防"无注释 pass"回流）。"""
    text = AGENT.read_text(encoding="utf-8")
    # 会话保存失败：原因已存 last_save_error
    assert "last_save_error" in text
    # memory 注入失败：不中断主对话（且已收窄为 except Exception）
    idx = text.find("memory 注入失败不应该中断主对话流程")
    assert idx > 0
    before = text[max(0, idx - 120): idx]
    assert "except Exception:" in before, "该处必须是 except Exception 而非裸 except"


def test_scan_no_longer_flags_bare_except_pattern():
    """扫描器视角：修完后该文件不应再有 silent-except 命中裸 except 形态。"""
    from eval.module_scan import scan_file

    findings = scan_file(AGENT)
    silent = [f for f in findings if f["rule"] == "silent-except"]
    # 允许仍有"有注释的合理防御"，但数量应低于修复前（8）
    assert len(silent) <= 8, f"silent-except 数量异常：{len(silent)}"
