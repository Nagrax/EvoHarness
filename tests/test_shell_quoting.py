"""回归：run_shell 在 Windows 上必须原样传递带引号的命令。

背景（2026-11-10 实测）：旧实现 `asyncio.create_subprocess_exec("cmd", "/c", command)`
会走 `list2cmdline` 把命令里的引号转义成 `\\"`，而 cmd 不认这种转义，
于是**任何带引号的命令**（`python -c "import time; time.sleep(1)"`）都 exit 1。
本机表现为：用户点"标记完成"之类的卡片后无反应、任何带引号的探活全挂。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.tools as tools  # noqa: E402

IS_WIN = os.name == "nt"


@pytest.mark.skipif(not IS_WIN, reason="Windows 引号语义专项")
def test_quoted_command_survives_shell():
    cmd = f'{sys.executable} -c "print(\'quoted-ok\')"'
    result = asyncio.run(tools.execute_tool("run_shell", {"command": cmd, "timeout": 30000}))
    assert result.status == tools.TOOL_OK, (
        f"带引号的命令必须能跑通：status={result.status} exit={result.exit_code}"
    )


@pytest.mark.skipif(not IS_WIN, reason="Windows 引号语义专项")
def test_semicolon_inside_quotes_is_not_split():
    cmd = f'{sys.executable} -c "import time; time.sleep(0.1); print(\'semi-ok\')"'
    result = asyncio.run(tools.execute_tool("run_shell", {"command": cmd, "timeout": 30000}))
    assert result.status == tools.TOOL_OK, f"分号在引号内不能被当成命令分隔符：{result.status}"


def test_nonzero_exit_is_still_reported():
    result = asyncio.run(tools.execute_tool("run_shell", {"command": "exit 4", "timeout": 30000}))
    assert result.status == tools.TOOL_FAILED
    assert result.exit_code == 4
