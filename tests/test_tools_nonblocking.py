"""W04 回归：shell 非阻塞、超时/取消回收进程树、有界输出。

历史缺陷（已修）：`execute_tool` 是 async，但内部 `subprocess.run` 同步阻塞事件循环——
4 秒命令期间另一协程只拿到 3 次调度机会（相当于 /api/health 无响应）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.tools as tools
from agents.tools import TOOL_OK, TOOL_TIMEOUT, execute_tool


IS_WIN = os.name == "nt"


def _slow_command(seconds: int) -> str:
    """构造约 seconds 秒的命令。

    Windows 的 `ping -n N` 首包立即返回，之后每包间隔 1 秒，故 N 包约 (N-1) 秒。
    """
    if IS_WIN:
        return f"cmd /c ping -n {seconds + 1} 127.0.0.1 > nul"
    return f"sleep {seconds}"


# ─── ① 事件循环不被阻塞 ──────────────────────────────────────


def test_long_shell_does_not_block_event_loop():
    """健康检查协程在长命令期间必须持续拿到调度机会。"""
    if not IS_WIN:
        pytest.skip("本机为 Windows 专项测量")

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.05)

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.1)
        started = time.perf_counter()
        result = await execute_tool("run_shell", {"command": _slow_command(2), "timeout": 60000})
        elapsed = time.perf_counter() - started
        stop = True
        await task
        return result, ticks, elapsed

    result, ticks, elapsed = asyncio.run(scenario())
    assert result.status == TOOL_OK, f"命令应在超时内完成：{result.status} {result.error_code}"
    assert elapsed >= 1.5
    # 旧实现同步阻塞：2 秒内几乎拿不到 tick；非阻塞时应远多于 10 次
    assert ticks >= 10, f"事件循环被阻塞，只有 {ticks} 次调度机会"
    # 健康检查 p95 目标：每个 tick 间隔近似 50ms，断言不至于超出一个数量级
    assert ticks >= int(elapsed / 0.2)


def test_health_like_coroutine_latency_is_small():
    """模拟 /api/health：长命令期间单次响应延迟应远小于命令时长。"""

    async def scenario():
        max_latency = 0.0
        stop = False

        async def probe():
            nonlocal max_latency
            while not stop:
                t0 = time.perf_counter()
                await asyncio.sleep(0)
                max_latency = max(max_latency, time.perf_counter() - t0)
                await asyncio.sleep(0.05)

        task = asyncio.create_task(probe())
        await asyncio.sleep(0.1)
        await execute_tool("run_shell", {"command": _slow_command(2), "timeout": 30000})
        stop = True
        await task
        return max_latency

    latency = asyncio.run(scenario())
    assert latency < 0.2, f"health 类协程被阻塞 {latency:.3f}s"


# ─── ② 超时与取消回收整棵进程树 ────────────────────────────


def test_timeout_kills_and_reports_timeout_status():
    if not IS_WIN:
        pytest.skip("进程树回收为 Windows 专项")

    async def scenario():
        return await execute_tool(
            "run_shell", {"command": _slow_command(20), "timeout": 800})

    result = asyncio.run(scenario())
    assert result.status == TOOL_TIMEOUT
    assert result.metadata.get("treeKilled") is True
    assert result.metadata.get("pid")


def test_cancel_terminates_process_tree(monkeypatch):
    if not IS_WIN:
        pytest.skip("进程树回收为 Windows 专项")

    killed: list[int] = []
    real_kill = tools._kill_process_tree

    def spy(pid):
        killed.append(pid)
        real_kill(pid)

    monkeypatch.setattr(tools, "_kill_process_tree", spy)

    async def scenario():
        task = asyncio.create_task(execute_tool(
            "run_shell", {"command": _slow_command(30), "timeout": 60000}))
        await asyncio.sleep(1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "finished"

    outcome = asyncio.run(scenario())
    assert outcome == "cancelled"
    assert killed, "取消时必须调用进程树终止"


def test_no_orphan_after_cancel():
    if not IS_WIN:
        pytest.skip("进程树回收为 Windows 专项")
    import subprocess

    marker = f"W04ORPHAN_{int(time.time())}"

    def pids():
        try:
            out = subprocess.run(
                ["wmic", "process", "where", "name='cmd.exe'", "get", "ProcessId,CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=20).stdout
        except Exception:
            return []
        found = []
        for line in out.splitlines():
            if marker in line:
                tail = line.strip().split(",")[-1].strip()
                if tail.isdigit():
                    found.append(int(tail))
        return found

    async def scenario():
        task = asyncio.create_task(execute_tool(
            "run_shell",
            {"command": f"cmd /c ping -n 60 127.0.0.1 > nul & rem {marker}", "timeout": 120000}))
        await asyncio.sleep(1.2)
        assert pids(), "前提：命令进程应已启动"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for _ in range(20):
            if not pids():
                return 0
            await asyncio.sleep(0.3)
        return len(pids())

    leftover = asyncio.run(scenario())
    assert leftover == 0, f"取消后仍有 {leftover} 个残留进程"


# ─── ③ 既有行为保持 ────────────────────────────────────────


def test_shell_success_and_nonzero_still_structured():
    ok = asyncio.run(execute_tool("run_shell", {"command": "cmd /c echo evo_w04"}))
    assert ok.status == TOOL_OK and "evo_w04" in ok

    bad = asyncio.run(execute_tool("run_shell", {"command": "cmd /c exit 4"}))
    assert bad.status == tools.TOOL_FAILED and bad.exit_code == 4


def test_timeout_coercion_unchanged():
    # 既有契约：非法/非正超时回退 30000ms
    assert tools._coerce_timeout_ms(None) == 30000.0
    assert tools._coerce_timeout_ms("abc") == 30000.0
    assert tools._coerce_timeout_ms(0) == 30000.0
    assert tools._coerce_timeout_ms(-5) == 30000.0
    assert tools._coerce_timeout_ms(1500) == 1500.0


def test_sync_run_shell_still_available_for_tests():
    # 既有测试直接调 _run_shell 同步版，必须保留且行为一致
    out = tools._run_shell({"command": "cmd /c echo sync_path"})
    assert out.status == TOOL_OK and "sync_path" in out
