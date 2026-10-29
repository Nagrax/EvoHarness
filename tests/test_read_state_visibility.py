"""read_file_state 登记失败的可见性回归（2026-10-29 判读 agents/tools.py）。

判读结论：该处 `except OSError: pass` 是**安全降级**（不登记 ≠ 登记错误值），
但缺可见性——频繁发生会让 edit_file 的"写前重核"大面积静默失效。
处置：保持降级 + 让降级可见（正常路径零输出）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

TOOLS = REPO / "agents" / "tools.py"


def test_mtime_registration_failures_are_logged():
    text = TOOLS.read_text(encoding="utf-8")
    assert "[read-state] cannot record mtime" in text, "登记失败必须可见"
    # 两处都要覆盖：读之后、写之后
    assert text.count("[read-state] cannot record mtime") >= 2, (
        "读取后与写入后两处都要留痕")


def test_fail_safe_direction_preserved():
    """降级方向必须仍是"不登记"（而不是登记一个错误值）——否则会误判为未变更。"""
    text = TOOLS.read_text(encoding="utf-8")
    for marker in ("[read-state] cannot record mtime for",
                   "[read-state] cannot record mtime after write for"):
        idx = text.find(marker)
        assert idx > 0, f"缺少留痕：{marker}"
        before = text[max(0, idx - 400): idx]
        assert "except OSError as error" in before, "必须捕获为具名变量"
        # 关键：except 体内不得写入 read_file_state
        after = text[idx: idx + 300]
        assert "read_file_state[abs_path] =" not in after, (
            "失败时不得写入任何值（否则会把未知状态伪装成已知）")


def test_normal_path_has_no_output():
    """正常路径不该有输出（否则日志会变成噪声）。"""
    text = TOOLS.read_text(encoding="utf-8")
    idx = text.find("read_file_state[abs_path] =  os.path.getmtime(abs_path)")
    assert idx > 0
    window = text[idx: idx + 60]
    assert "print" not in window.split("except")[0], "成功路径不得打印"
