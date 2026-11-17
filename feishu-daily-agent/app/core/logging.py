"""日志配置。

Windows 控制台默认 GBK：中文日志会变成乱码（2026-11-10 实机联调看到
"定时调度已启动" 变成问号）。这里把输出流与各 handler 都显式切到 UTF-8。
"""
from __future__ import annotations

import logging
import sys

from app.core.config import get_settings


def setup_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 被重定向到非文本流时忽略
            pass

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # 飞书 SDK 自身日志收敛到 WARNING，避免刷屏
    logging.getLogger("lark").setLevel(max(logging.WARNING, level))
    logging.getLogger("Lark").setLevel(max(logging.WARNING, level))
