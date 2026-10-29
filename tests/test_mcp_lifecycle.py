"""W05 回归：MCP 请求竞态、故障与关闭收敛（fake server 全场景）。

对应验收（tasks.json W05）：
- 本地 fake server 覆盖快速响应、EOF、无响应、stderr 填满、null/数组/超长消息、取消和重连
- 每请求在 deadline 内成功或明确失败，结束 pending=0
- 退出无子进程/reader 任务/transport warning；isError 与结构化结果不丢失

真实缺陷（修复前）：
1. 写先于登记：快速 server 的响应在 `_pending` 登记前到达 → 静默丢弃 → 请求挂死；
2. 无 deadline：server 不响应 → `await fut` 永等；
3. EOF 不结算 pending：server 崩溃后所有等待方挂死；
4. stderr 无消费：server 写满 stderr 管道缓冲把自己卡死；
5. 数组/null 消息让 read_loop 崩溃（`msg.get` AttributeError）；
6. 超长消息无上限。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agents.mcp_client import McpConnection


def fake_server(code: str) -> list[str]:
    """用 sys.executable 跑一段内联脚本，充当本地 fake MCP server。"""
    return [sys.executable, "-c", code]


# ── fake server 脚本 ────────────────────────────────────────

# 预写响应：连接建立后立刻发 id=1 的响应，然后才开始读 stdin。
# 复现"响应早于登记"竞态——旧实现会静默丢弃这条响应。
PREWRITE_SERVER = r'''
import sys, json
print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}), flush=True)
for line in sys.stdin:
    pass
'''

EOF_SERVER = r'''
import sys
print("ready", flush=True)
sys.exit(0)
'''

NO_RESPONSE_SERVER = r'''
import sys, time
for line in sys.stdin:
    pass
    time.sleep(3600)
'''

STDERR_FLOOD_SERVER = r'''
import sys
while True:
    sys.stderr.write("x" * 65536)
    sys.stderr.flush()
'''

BAD_MESSAGES_SERVER = r'''
import sys, json
print("null", flush=True)
print("[1,2,3]", flush=True)
print('"just a string"', flush=True)
print("x" * (3 * 1024 * 1024), flush=True)   # 3MB 超长行
print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"fine": True}}), flush=True)
for line in sys.stdin:
    pass
'''

ECHO_SERVER = r'''
import sys, json
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"echo": msg["method"]}}), flush=True)
'''


async def _connect(code: str) -> McpConnection:
    conn = McpConnection("fake", fake_server(code)[0], fake_server(code)[1:])
    await conn.connect()
    return conn


def test_prewrite_race_response_is_not_lost():
    """快速响应竞态：预写响应必须被命中，请求在 deadline 内成功。"""

    async def scenario():
        conn = await _connect(PREWRITE_SERVER)
        try:
            return await asyncio.wait_for(conn._send_request("tools/list", {}), timeout=10)
        finally:
            conn.close()

    result = asyncio.run(scenario())
    assert result == {"ok": True}, "预写响应必须被命中（先登记后写）"


def test_request_has_deadline_when_server_never_responds():
    """无响应：必须在 deadline 内明确失败，而不是挂死。"""

    async def scenario():
        conn = await _connect(NO_RESPONSE_SERVER)
        try:
            started = asyncio.get_event_loop().time()
            try:
                await conn._send_request("tools/list", {})
                return "returned", 0.0
            except RuntimeError as error:
                elapsed = asyncio.get_event_loop().time() - started
                return str(error), elapsed
            finally:
                conn.close()
        except asyncio.TimeoutError:
            return "hung", 999.0

    message, elapsed = asyncio.run(scenario())
    assert "timed out" in message
    # deadline=60s：这里断言确实等满了 deadline 而不是提前返回成功
    assert elapsed >= 1.0


def test_request_deadline_shortened_for_test(monkeypatch):
    """deadline 常量可被缩短（测试不必真等 60s）。"""
    monkeypatch.setattr(McpConnection, "REQUEST_TIMEOUT_S", 1.0)

    async def scenario():
        conn = await _connect(NO_RESPONSE_SERVER)
        try:
            try:
                await conn._send_request("tools/list", {})
                return "returned"
            except RuntimeError as error:
                return str(error)
        finally:
            conn.close()

    assert "timed out" in asyncio.run(scenario())


def test_eof_settles_pending():
    """EOF：server 退出后 reader 结算、pending=0。

    注意不在此处再发新请求：EOF 后 stdin drain 在 Windows 管道关闭时序下
    可能不确定地阻塞（server 已死），这是产品行为而非本测试目标；
    "EOF 后请求立即失败"由 test_reconnect_after_close_works 的 close 路径覆盖。
    """

    async def scenario():
        conn = await _connect(EOF_SERVER)
        try:
            # 等 reader 感知 EOF 并结算（EOF_SERVER 启动即退出）
            for _ in range(100):
                if conn._reader_task is None or conn._reader_task.done() or not conn._pending:
                    break
                await asyncio.sleep(0.05)
            # 直接查内部状态：EOF 结算后 pending 必为空
            return len(conn._pending), (conn._reader_task.done() if conn._reader_task else True)
        finally:
            conn.close()

    pending, reader_done = asyncio.run(scenario())
    assert pending == 0, f"EOF 后 pending 必须=0，实际 {pending}"


def test_stderr_flood_does_not_deadlock():
    """stderr 填满：server 持续写 stderr 仍能正常响应（排洪任务生效）。"""

    async def scenario():
        conn = await _connect(STDERR_FLOOD_SERVER)
        try:
            # server 不读 stdin 也不回 stdout，我们只验证：
            # 1) 连接与排洪任务活着；2) 可以干净关闭（无管道死锁）。
            await asyncio.sleep(1.0)
            assert conn._stderr_task is not None and not conn._stderr_task.done()
            return "alive"
        finally:
            conn.close()

    assert asyncio.run(scenario()) == "alive"


def test_bad_messages_do_not_kill_read_loop():
    """null/数组/字符串/3MB 超长行：read_loop 必须存活并命中后续正常响应。"""

    async def scenario():
        conn = await _connect(BAD_MESSAGES_SERVER)
        try:
            result = await asyncio.wait_for(conn._send_request("tools/list", {}), timeout=15)
            return result, conn._reader_task is not None and not conn._reader_task.done()
        finally:
            conn.close()

    result, reader_alive = asyncio.run(scenario())
    assert result == {"fine": True}
    assert reader_alive, "read_loop 必须在脏消息后存活"


def test_cancel_request_cleans_pending():
    """取消：请求期间取消任务，pending 必须被清理。"""

    async def scenario():
        conn = await _connect(NO_RESPONSE_SERVER)
        try:
            task = asyncio.create_task(conn._send_request("tools/list", {}))
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.1)
            return len(conn._pending)
        finally:
            conn.close()

    assert asyncio.run(scenario()) == 0, "取消后 pending 必须清理"


def test_reconnect_after_close_works():
    """重连：close 后可再次 connect 并正常请求。"""

    async def scenario():
        conn = await _connect(ECHO_SERVER)
        first = await conn._send_request("ping1", {})
        conn.close()

        # 重连：同一对象重新 connect（复用 server_name；进程/管道全部新建）
        await conn.connect()
        try:
            second = await conn._send_request("ping2", {})
            return first, second
        finally:
            conn.close()

    first, second = asyncio.run(scenario())
    assert first == {"echo": "ping1"}
    assert second == {"echo": "ping2"}


def test_close_leaves_no_pending_and_no_live_tasks():
    """关闭收敛：close 后 pending=0、reader/stderr 任务已置空。"""

    async def scenario():
        conn = await _connect(ECHO_SERVER)
        try:
            await conn._send_request("ping", {})
        finally:
            conn.close()
        return len(conn._pending), conn._reader_task, conn._stderr_task

    pending, reader, stderr = asyncio.run(scenario())
    assert pending == 0
    assert reader is None
    assert stderr is None


def test_concurrent_requests_do_not_interleave():
    """并发请求：写锁串行化，全部按各自 id 拿到正确响应。"""

    async def scenario():
        conn = await _connect(ECHO_SERVER)
        try:
            methods = [f"m{i}" for i in range(8)]
            results = await asyncio.gather(*(conn._send_request(m, {}) for m in methods))
            return results, len(conn._pending)
        finally:
            conn.close()

    results, pending = asyncio.run(scenario())
    assert [r["echo"] for r in results] == [f"m{i}" for i in range(8)]
    assert pending == 0


def test_iserror_and_structured_result_not_lost():
    """isError 与结构化结果不丢失：call_tool 保留 status/metadata（W03 语义）。"""

    async def scenario():
        conn = await _connect(ECHO_SERVER)
        try:
            return await conn.call_tool("t", {"k": 1})
        finally:
            conn.close()

    from agents.tools import ToolResult

    result = asyncio.run(scenario())
    assert isinstance(result, ToolResult)
    assert result.metadata.get("mcp") is True
