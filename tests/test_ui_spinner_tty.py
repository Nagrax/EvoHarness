"""spinner 在非 TTY 下不得输出（2026-10-30 dogfooding 发现）。

实测：`python -m agents.main ... > out.txt 2> err.txt` 时
stdout 有 16038 字符，**99% 是 `⠼ Thinking...` 帧**——真实结果被淹没。
修法：`start_spinner` 加 `sys.stdout.isatty()` 判断。

这个测试固定住"非 TTY 必须安静"，防止将来有人去掉判断。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import agents.ui as ui


class _PipeLike(io.StringIO):
    """模拟管道/重定向：isatty() 返回 False。"""

    def isatty(self) -> bool:  # noqa: D102
        return False


class _TtyLike(io.StringIO):
    def isatty(self) -> bool:  # noqa: D102
        return True


def test_spinner_disabled_when_not_a_tty(monkeypatch):
    pipe = _PipeLike()
    monkeypatch.setattr(sys, "stdout", pipe)
    ui.stop_spinner()  # 清理可能残留的线程
    ui.start_spinner("Thinking")
    try:
        assert ui._spinner_thread is None, "非 TTY 下不应启动 spinner 线程"
        assert pipe.getvalue() == "", "非 TTY 下 spinner 不得写任何内容"
    finally:
        ui.stop_spinner()


def test_spinner_enabled_flag_reflects_isatty(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _PipeLike())
    assert ui._spinner_enabled() is False
    monkeypatch.setattr(sys, "stdout", _TtyLike())
    assert ui._spinner_enabled() is True


def test_spinner_enabled_handles_missing_isatty(monkeypatch):
    class _NoIsatty:
        def write(self, _text):  # noqa: D102
            return 0

        def flush(self):  # noqa: D102
            return None

    monkeypatch.setattr(sys, "stdout", _NoIsatty())
    assert ui._spinner_enabled() is False, "缺 isatty 时应保守地当非 TTY"


def test_stop_spinner_is_noop_when_never_started():
    ui.stop_spinner()
    ui.stop_spinner()  # 幂等，不应抛异常


def test_tty_path_still_writes_frames(monkeypatch):
    """真正的终端仍应工作（不能把功能修没了）。"""
    tty = _TtyLike()
    monkeypatch.setattr(sys, "stdout", tty)
    ui.stop_spinner()
    ui.start_spinner("Thinking")
    try:
        assert ui._spinner_thread is not None, "TTY 下应启动线程"
    finally:
        ui.stop_spinner()
    assert tty.getvalue(), "TTY 下应至少写出一帧"