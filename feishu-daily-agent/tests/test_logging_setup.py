"""回归：日志必须 UTF-8，否则 Windows 控制台中文全是问号。

背景（2026-11-10 实机联调）：`定时调度已启动` 写成 GBK 后日志里变成
`��ʱ��������`，排查时无法阅读。
"""
from __future__ import annotations

import logging

from app.core.logging import setup_logging


def test_setup_logging_is_idempotent_and_utf8():
    setup_logging()
    setup_logging()
    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    assert handlers, "必须有一个输出 handler"
    # 重复调用不应叠加 handler（历史上会越积越多、日志重复打印）
    assert len(handlers) == 1
    handler = handlers[0]
    assert getattr(handler.stream, "encoding", "").lower() in {"utf-8", "utf8"}


def test_lark_logger_is_quiet():
    setup_logging()
    assert logging.getLogger("lark").level >= logging.WARNING
