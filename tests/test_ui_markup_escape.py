"""agents/ui markup 转义回归测试（纯本地，零模型）。

背景：工具输出 / 异常文本原样塞进 rich Panel（markup=True），内容含 '[/]'、'[bold]'
等方括号时 rich 会当成标记并抛 MarkupError，打断当前轮。修复：渲染前对用户内容做
rich.markup.escape。这里复现修复前的崩溃形态，证明修复后能按字面安全渲染。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console  # noqa: E402

import agents.ui as ui  # noqa: E402


def _capture(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(
        ui, "console",
        Console(file=buf, width=200, force_terminal=False, highlight=False),
    )
    return buf


def test_print_tool_result_escapes_markup(monkeypatch):
    buf = _capture(monkeypatch)
    ui.print_tool_result("run_shell", "raw output with [/] and [bold]not-a-tag[/bold]")
    text = buf.getvalue()
    assert "[/]" in text
    assert "[bold]not-a-tag[/bold]" in text


def test_print_error_escapes_markup(monkeypatch):
    buf = _capture(monkeypatch)
    ui.print_error("boom [/] [red]x[/red]")
    assert "[/]" in buf.getvalue()


def test_print_warning_escapes_markup(monkeypatch):
    buf = _capture(monkeypatch)
    ui.print_warning("notice [/] here")
    assert "[/]" in buf.getvalue()


def test_print_retry_escapes_reason(monkeypatch):
    buf = _capture(monkeypatch)
    ui.print_retry(1, 3, "bad [/] reason")
    assert "bad [/] reason" in buf.getvalue()


def test_print_plan_for_approval_escapes_markup(monkeypatch):
    buf = _capture(monkeypatch)
    ui.print_plan_for_approval("- step [x]\nends with [/]")
    assert "[/]" in buf.getvalue()


def test_print_sub_agent_start_escapes_agent_type(monkeypatch):
    # agent_type 曾直接内插进 rich Panel 标题（未转义）：含 '[/]' 时抛 MarkupError、打断当轮。
    buf = _capture(monkeypatch)
    ui.print_sub_agent_start("agent_type=[/]", "desc [bold]x[/bold]")
    assert "[/]" in buf.getvalue()


def test_print_sub_agent_end_escapes_agent_type(monkeypatch):
    buf = _capture(monkeypatch)
    ui.print_sub_agent_end("type=[/] end", "desc")
    assert "[/]" in buf.getvalue()


def test_print_tool_call_escapes_tool_name_markup(monkeypatch):
    # 工具名含 '[/]'：修复前原样入 rich markup → print_tool_call（在权限校验之前）抛
    # MarkupError，异常冒出 chat() 并残留 _turn_output_buffer。
    buf = _capture(monkeypatch)
    ui.print_tool_call("weird[/]name", {})
    assert "weird[/]name" in buf.getvalue()
