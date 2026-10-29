"""
MCP 客户端模块。

这个文件负责连接基于 stdio 的 MCP Server，完成工具发现，并把 Agent 发起的工具调用
转发到对应的 MCP Server。

实现方式：
- 不依赖 MCP SDK，直接通过标准输入/标准输出传输 JSON-RPC 消息。
- 每个 MCP Server 都由一个子进程承载。
- 每个 MCP 工具都会被包装成 `mcp__serverName__toolName` 形式，避免和本地工具重名。

配置来源：
- 全局配置：`~/.evoharness/settings.json`
- 项目配置：`.evoharness/settings.json`
- Claude Code 约定配置：`.mcp.json`

配置格式示例：
{
    "mcpServers": {
        "name": {
            "command": "...",
            "args": [...],
            "env": {...}
        }
    }
}
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .ui import print_error, print_info


# ─── 工具 hint（tool annotations）语义 ──────────────────────────────────────────
# MCP 工具可声明 readOnlyHint 等注解。未声明的「未知工具」必须按可能有副作用处理
# （不得默认声称只读 / 非破坏），供 MCP 信任索引与工具目录检查消费。
_HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
_CONSERVATIVE_HINTS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}


def _declared_hints(annotations: Any) -> dict[str, bool] | None:
    """从 MCP tool annotations 抽取已知 hint；无任何已知 hint 时返回 None（表示未知）。

    仅接受真正的 bool（上游可能传入 "false" / 0 / null 等非布尔值，一律视为未声明，
    交给上层按保守默认处理，绝不 bool() 强转把 "false" 当成 True）。
    """
    if not isinstance(annotations, dict):
        return None
    hints = {k: annotations[k] for k in _HINT_KEYS if isinstance(annotations.get(k), bool)}
    return hints or None


# ─── 单个 MCP 连接：一个 McpConnection 对应一个 MCP Server 子进程 ──────────────────


def _close_pipe(pipe: Any) -> None:
    """关闭一条 stdio 管道（StreamWriter / StreamReader），异常一律吞掉。

    StreamWriter 自带 close()；StreamReader 没有 close()，需关闭其底层 transport。
    Windows ProactorEventLoop 下若只 kill 子进程而不关闭管道，传输层会残留未关闭的
    transport（ResourceWarning），并在 GC 时因访问已关闭句柄抛
    `ValueError: I/O operation on closed pipe`。
    """
    if pipe is None:
        return
    try:
        transport = getattr(pipe, "_transport", None)
        if transport is not None:
            transport.close()
            return
    except Exception:
        pass
    close = getattr(pipe, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class McpConnection:
    """管理单个 MCP Server 子进程，以及和它之间的 JSON-RPC 通信。"""

    # 每请求 deadline（秒）：acceptance 要求"每请求在 deadline 内成功或明确失败"。
    # 60s 覆盖慢启动的 stdio server（npx 首次拉包），又不至于把挂死拖成用户可感的卡顿。
    REQUEST_TIMEOUT_S = 60.0
    # 单条消息上限（字节）：超长行直接丢弃。无上限时异常 server 能用一行输出耗尽内存。
    MAX_MESSAGE_BYTES = 1 * 1024 * 1024

    def __init__(self, server_name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None):
        # MCP Server 在配置中的名称，用于后续生成工具名前缀和路由工具调用。
        self.server_name = server_name
        # 启动 MCP Server 的命令，例如 `node`、`python`、某个可执行文件路径等。
        self.command = command
        # 启动命令附带的参数。
        self.args = args or []
        # 额外环境变量。连接时会和当前进程环境变量合并。
        self.env = env or {}
        # MCP Server 子进程对象。连接成功前为空。
        self._process: asyncio.subprocess.Process | None = None
        # JSON-RPC 请求 id 自增计数器，用于把请求和响应对应起来。
        self._next_id = 1
        # 正在等待响应的请求。key 是 JSON-RPC id，value 是 Future。
        self._pending: dict[int, asyncio.Future] = {}
        # 后台读取 stdout 的任务。MCP Server 的响应会从 stdout 持续读出。
        self._reader_task: asyncio.Task | None = None
        # 有界消费 stderr 的后台任务。不消费时 server 写满 stderr 管道缓冲会把自己卡死。
        self._stderr_task: asyncio.Task | None = None
        # 串行化 stdin 写入：并发请求交织写会产生非法 JSON-RPC 流。
        self._write_lock: asyncio.Lock | None = None

    async def connect(self) -> None:
        """启动 MCP Server 子进程，并开始后台读取它的 stdout。"""
        # 子进程环境变量 = 当前进程环境变量 + 配置里声明的额外变量。
        merged_env = {**os.environ, **self.env}
        # Windows 上 npx/node 等命令实际是 .cmd 批处理，create_subprocess_exec
        # 不走 shell 时无法按裸名找到它们；用 shutil.which 解析真实可执行路径。
        resolved = shutil.which(self.command) or self.command
        # 使用 stdio 模式启动 MCP Server：
        # - stdin：客户端向 Server 写 JSON-RPC 请求。
        # - stdout：Server 向客户端返回 JSON-RPC 响应。
        # - stderr：保留错误输出管道，避免 Server 继承当前终端输出。
        self._process = await asyncio.create_subprocess_exec(
            resolved, *self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
            # readline 的行长度上限要与 MAX_MESSAGE_BYTES 一致：默认 64KB 时，
            # 正常的大响应（如 list_tools 返回大 schema）会被 readline 直接抛
            # ValueError（"chunk exceed the limit"），到不了我们的长度检查。
            limit=max(1, self.MAX_MESSAGE_BYTES),
        )
        # 后台持续读取 stdout。这里不阻塞 connect()，否则后续无法继续初始化。
        self._reader_task = asyncio.create_task(self._read_loop())
        # 有界消费 stderr：只计数不保存，防止 server 因 stderr 管道写满而阻塞。
        self._stderr_task = asyncio.create_task(self._drain_stderr_loop())
        self._write_lock = asyncio.Lock()

    async def _read_loop(self) -> None:
        """持续读取 MCP Server stdout 中按行分隔的 JSON-RPC 响应。

        退出纪律：无论正常 EOF、协议错还是读循环自身异常，都必须统一结算
        `_pending`（全部置异常），否则等待方永远挂起——server 崩溃后
        "请求挂死" 会被用户当成 "工具没响应"，无从排查。
        """
        assert self._process and self._process.stdout
        try:
            while True:
                # MCP stdio 通常是一行一个 JSON-RPC 消息。
                try:
                    line = await self._process.stdout.readline()
                except ValueError:
                    # 超过 StreamReader limit 的行：异常本身已消费 limit 字节，
                    # 流会从同一行的下一段继续；continue 后逐段吞到行尾再恢复正常。
                    continue
                if not line:
                    # 读不到内容说明子进程 stdout 已关闭，通常表示进程退出。
                    break
                if len(line) > self.MAX_MESSAGE_BYTES:
                    # 超长消息：readline 已把它整行读出，丢弃即可；
                    # 不设上限的话异常 server 能用一行输出耗尽客户端内存。
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # 非法 JSON 直接忽略，避免单条脏输出打断整个连接。
                    continue
                # JSON-RPC 消息必须是对象：null / 数组 / 标量都没有 .get，
                # 直接走到下面会让 read_loop 崩溃（未捕获异常 → reader 任务死亡 → 全部请求挂死）。
                if not isinstance(msg, dict):
                    continue

                # JSON-RPC 响应会带 id；通知类消息通常没有 id。
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if "error" in msg:
                        # Server 返回 JSON-RPC error 时，把等待中的 Future 标记为异常。
                        e = msg["error"]
                        fut.set_exception(
                            RuntimeError(f"MCP error {e.get('code')}: {e.get('message')}")
                        )
                    else:
                        # 正常响应只把 result 部分交给调用方。
                        fut.set_result(msg.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 读循环自身崩溃（如传输层被关闭）也必须结算 pending，原因要带出去。
            self._settle_pending_on_exit(f"MCP server '{self.server_name}' reader crashed: {exc}")
            raise
        # EOF：server 退出。统一结算所有还在等待的请求。
        self._settle_pending_on_exit(f"MCP server '{self.server_name}' exited (stdout closed)")

    async def _drain_stderr_loop(self) -> None:
        """有界消费 stderr：只丢弃内容并计数，防止 server 写满管道缓冲把自己卡死。

        之前 stderr=PIPE 但无人消费：server 一旦输出超过管道缓冲（Windows ~4-64KB），
        它的下一次 stderr 写就阻塞，连带整个 server 卡死——这是"stderr 填满"用例的根因。
        """
        assert self._process and self._process.stderr
        try:
            while True:
                chunk = await self._process.stderr.read(8192)
                if not chunk:
                    break
                # 丢弃内容（不落盘、不打印，避免把 server 日志泄进主输出）。
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # stderr 读失败不影响主链路：stdout 承载 JSON-RPC，stderr 只是排洪道。
            pass

    def _settle_pending_on_exit(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending.clear()

    async def _send_request(self, method: str, params: dict | None = None) -> Any:
        """发送一条 JSON-RPC request，并在 deadline 内等待对应 id 的 response。

        修复的两个真实竞态（对应验收里的 "快速响应" 与 "无响应" 用例）：
        1. **先写后登记**：旧实现先 `drain()` 再登记 Future。快速 server 在 drain
           返回前就把响应送进 read_loop，`msg_id not in _pending` → 响应被静默丢弃
           → 请求永远挂起。现在**先登记、再写**，早到响应一定能找到 Future。
        2. **无 deadline**：旧实现 `await fut` 永等。server 不响应时请求挂死。
           现在每请求有 REQUEST_TIMEOUT_S 上限，超时显式失败并清理登记。
        并发写 stdin 会交织出非法 JSON-RPC 流，因此用写锁串行化。
        """
        assert self._process and self._process.stdin
        # 为本次请求分配唯一 id。读循环会用这个 id 找到对应 Future。
        req_id = self._next_id
        self._next_id += 1

        # JSON-RPC 2.0 请求格式：
        # {
        #   "jsonrpc": "2.0",
        #   "id": 1,
        #   "method": "...",
        #   "params": {...}
        # }
        msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})

        # 先登记 Future，再写 stdin：早到的响应必须在 _pending 里能找到归宿。
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            async with self._write_lock:
                # stdio 传输使用换行作为消息边界。
                self._process.stdin.write((msg + "\n").encode())
                await self._process.stdin.drain()
            # deadline 内必须成功或明确失败；不允许无限等待。
            return await asyncio.wait_for(fut, timeout=self.REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"MCP request '{method}' timed out after {self.REQUEST_TIMEOUT_S}s "
                f"(server '{self.server_name}')"
            ) from None
        finally:
            # 超时/取消/异常路径都要把登记清掉，避免 pending 泄漏累积。
            self._pending.pop(req_id, None)

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """发送一条 JSON-RPC notification。notification 没有 id，也不等待响应。"""
        if not self._process or not self._process.stdin:
            return
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        self._process.stdin.write((msg + "\n").encode())

    async def initialize(self) -> None:
        """执行 MCP 初始化握手。"""
        # initialize 是 MCP 连接建立后的第一步，用于协商协议版本和客户端信息。
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "evoharness", "version": "1.0.0"},
        })
        # 初始化请求成功后，按 MCP 协议发送 initialized 通知。
        self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict]:
        """从当前 MCP Server 查询可用工具列表。"""
        result = await self._send_request("tools/list")
        if not result or not isinstance(result.get("tools"), list):
            return []
        # 这里保留 MCP 原始 inputSchema，同时附加 serverName，方便上层做工具名加前缀和路由。
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema"),
                "serverName": self.server_name,
                # 透传上游声明的 tool annotations；未声明时置 None，由上层按未知保守处理。
                "hints": _declared_hints(t.get("annotations")),
            }
            for t in result["tools"]
        ]

    async def call_tool(self, name: str, args: dict):
        """调用 MCP Server 工具，返回保留协议状态的 ToolResult。

        MCP 规范用 `isError` 表示工具失败；历史实现把它丢掉，只拼 content 文本，
        于是 Agent 只能靠正文里有没有 "error" 来猜 —— 工具返回一段说明错误的文本
        会被当成失败，而真正的 isError=true 若正文没那个词就会被当成成功。
        """
        from agents.tools import ToolResult, TOOL_FAILED, TOOL_OK

        result = await self._send_request("tools/call", {"name": name, "arguments": args})
        is_error = bool(isinstance(result, dict) and result.get("isError"))
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            # MCP 工具结果通常是 content block 列表。当前 Agent 只消费 text block。
            text = "\n".join(
                str(c.get("text") or "") for c in result["content"]
                if isinstance(c, dict) and c.get("type") == "text"
            )
            return ToolResult(
                text,
                status=TOOL_FAILED if is_error else TOOL_OK,
                error_code="mcp-is-error" if is_error else "",
                metadata={"mcp": True, "serverTool": name, "isError": is_error},
            )
        # 如果不是标准 content list，就退回为 JSON 字符串，避免丢失信息。
        return ToolResult(
            json.dumps(result),
            status=TOOL_FAILED if is_error else TOOL_OK,
            error_code="mcp-is-error" if is_error else "",
            metadata={"mcp": True, "serverTool": name, "isError": is_error},
        )

    def close(self) -> None:
        """关闭 MCP Server 子进程与全部 stdio 管道，并让所有等待中的请求失败。

        幂等：重复调用安全。Windows ProactorEventLoop 下只 kill 进程而不回收传输层，
        会让传输层残留未关闭的管道 / 子进程，触发 `ResourceWarning: unclosed transport`
        与 `ValueError: I/O operation on closed pipe`。
        """
        if self._reader_task:
            # 停止 stdout 后台读取任务。
            self._reader_task.cancel()
            self._reader_task = None
        if self._stderr_task:
            # 停止 stderr 排洪任务。
            self._stderr_task.cancel()
            self._stderr_task = None
        proc = self._process
        self._process = None
        if proc is not None:
            try:
                # 直接杀掉子进程，确保外部 MCP Server 不继续残留。
                proc.kill()
            except (ProcessLookupError, OSError):
                # 进程已经退出时 kill 可能抛出该异常，忽略即可。
                pass
            # 显式关闭子进程传输层：仅 kill 并关闭管道时，_WindowsSubprocessTransport
            # 仍以 "running" 状态被 GC，打印 `unclosed transport`（子进程未 reap）。
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            # 显式关闭 stdin/stdout/stderr 管道，释放传输层资源、避免管道泄漏。
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                _close_pipe(pipe)
            # 置空，保证后续不再误用已关闭的管道。
            proc.stdin = None
            proc.stdout = None
            proc.stderr = None
        # 连接关闭后，所有还没收到响应的请求都不可能再完成，需要显式置为异常。
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP server '{self.server_name}' closed"))
        self._pending.clear()


# ─── MCP 管理器：统一管理多个 MCP Server 连接和工具路由 ─────────────────────────────


class McpManager:
    """
    管理所有 MCP Server 连接。

    使用方式：
    1. 调用 load_and_connect() 读取配置、连接 Server、发现工具。
    2. 调用 get_tool_definitions() 把 MCP 工具暴露给模型。
    3. 当模型调用 mcp__server__tool 形式的工具时，用 call_tool() 路由到对应 Server。
    """

    def __init__(self):
        # 已连接的 MCP Server。key 是 server name，value 是对应连接对象。
        self._connections: dict[str, McpConnection] = {}
        # 所有 MCP Server 发现出来的工具定义，保持接近 MCP 原始格式。
        self._tools: list[dict] = []
        # 防止重复连接。load_and_connect() 只应真正执行一次。
        self._connected = False

    async def load_and_connect(self) -> None:
        """读取配置，连接所有配置的 MCP Server，并发现它们提供的工具。"""
        if self._connected:
            return
        self._connected = True

        # 合并全局、项目和 .mcp.json 配置。后读取的配置会覆盖同名 Server。
        configs = self._load_configs()
        if not configs:
            return

        # 每个 Server 的初始化和工具发现最多等待 15 秒，避免坏配置长期卡住启动。
        timeout = 15.0

        for name, cfg in configs.items():
            # 根据配置创建一个独立连接对象。
            conn = McpConnection(
                name,
                cfg["command"],
                cfg.get("args"),
                cfg.get("env"),
            )
            try:
                # 连接子进程 -> MCP 初始化握手 -> 查询工具列表。
                await conn.connect()
                await asyncio.wait_for(conn.initialize(), timeout=timeout)
                server_tools = await asyncio.wait_for(conn.list_tools(), timeout=timeout)
                # 只有初始化和工具发现都成功，才登记为可用连接。
                self._connections[name] = conn
                self._tools.extend(server_tools)
                print_info(f"MCP connected: {name} ({len(server_tools)} tools)")
            except Exception as e:
                # 单个 Server 失败不影响其他 Server。失败连接需要关闭以清理子进程。
                print_error(f"MCP failed to connect: {name}: {e}")
                conn.close()

    def get_tool_definitions(self, *, with_hints: bool = True) -> list[dict]:
        """返回 Agent 可直接使用的工具定义，并给 MCP 工具名加前缀。

        with_hints=True 时透传上游 Server 声明的 tool hints（readOnlyHint 等）；
        上游未声明的工具按安全默认值补齐（unknown 一律按可能有副作用处理），
        供 MCP 生态信任索引与 OpenAI 工具目录检查消费。
        """
        out: list[dict] = []
        for t in self._tools:
            item = {
                # 前缀格式：mcp__服务名__工具名。
                # 这样可以避免 MCP 工具和内置工具重名，也方便 call_tool() 反向解析路由。
                "name": f"mcp__{t['serverName']}__{t['name']}",
                "description": t.get("description") or f"MCP tool {t['name']} from {t['serverName']}",
                # Anthropic 工具字段叫 input_schema；MCP 原始工具字段一般叫 inputSchema。
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            if with_hints:
                hints = t.get("hints")
                if hints is None:
                    # 上游未声明任何 hint 的未知工具：保守默认，不声称只读 / 不声称非破坏。
                    item["hints"] = dict(_CONSERVATIVE_HINTS)
                else:
                    # 部分声明时，未声明的字段一律取保守值（不得默认声称只读 / 非破坏）。
                    resolved = {
                        key: bool(hints[key]) if key in hints else _CONSERVATIVE_HINTS[key]
                        for key in _HINT_KEYS
                    }
                    # readOnly 与 destructive 互斥：只读工具不得再被补 destructiveHint=True，
                    # 否则同一 tool 的 hint 自相矛盾（readOnly=True 却 destructive=True）。
                    if resolved["readOnlyHint"]:
                        resolved["destructiveHint"] = False
                    item["hints"] = resolved
            out.append(item)
        return out

    def is_mcp_tool(self, name: str) -> bool:
        """判断一个工具名是否是 MCP 工具名。"""
        return name.startswith("mcp__")

    async def call_tool(self, prefixed_name: str, args: dict) -> str:
        """把带前缀的 MCP 工具调用路由到正确的 MCP Server。"""
        # 工具名格式为 mcp__serverName__toolName。serverName 本身可能含 "__"，
        # 因此按「已注册 server 名的最长前缀匹配」解析，而不是简单 split('__')[1]。
        server_name, tool_name = self._split_prefixed_name(prefixed_name)
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        return await conn.call_tool(tool_name, args)

    def _split_prefixed_name(self, prefixed_name: str) -> tuple[str, str]:
        """把 `mcp__<server>__<tool>` 解析为 (server_name, tool_name)。

        server 名可能包含 "__"，故取已连接 server 名中「最长且后接 '__' 的前缀」；
        找不到时退回首个 '__' 后的分段并交由调用方报「未连接」。
        """
        prefix = "mcp__"
        if not isinstance(prefixed_name, str) or not prefixed_name.startswith(prefix):
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        rest = prefixed_name[len(prefix):]
        best = ""
        for name in self._connections:
            if rest.startswith(name + "__") and len(name) > len(best):
                best = name
        if not best:
            # 无已注册 server 匹配：取首个 "__" 前的一段作为 server 名（能给出更准确的报错）。
            idx = rest.find("__")
            if idx == -1:
                raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
            best = rest[:idx]
        tool_name = rest[len(best) + 2:]
        if not tool_name:
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        return best, tool_name

    async def disconnect_all(self) -> None:
        """断开所有 MCP Server 连接，并清空工具缓存。"""
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()
        self._tools.clear()
        self._connected = False

    # ─── 配置加载 ──────────────────────────────────────

    def _load_configs(self) -> dict[str, dict]:
        """按优先级加载并合并 MCP Server 配置。"""
        merged: dict[str, dict] = {}
        # 解析失败的配置文件记入这里（而不是静默跳过）——
        # 否则用户会以为这些 MCP server 正常工作，实际一个都没加载。
        self.config_errors = []

        # 1. 全局配置：~/.evoharness/settings.json
        global_path = Path.home() / ".evoharness" / "settings.json"
        self._merge_config_file(global_path, merged)

        # 2. 当前项目配置：<cwd>/.evoharness/settings.json
        project_path = Path.cwd() / ".evoharness" / "settings.json"
        self._merge_config_file(project_path, merged)

        # 3. Claude Code 约定配置：<cwd>/.mcp.json
        mcp_json_path = Path.cwd() / ".mcp.json"
        self._merge_config_file(mcp_json_path, merged)

        return merged

    def _merge_config_file(self, path: Path, target: dict[str, dict]) -> None:
        """把单个配置文件里的 mcpServers 合并进 target。"""
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            # 支持两种格式：
            # 1. {"mcpServers": {"name": {...}}}
            # 2. {"name": {...}}
            servers = raw.get("mcpServers", raw)
            for name, config in servers.items():
                # 只接收包含 command 的对象；无效配置直接忽略。
                if isinstance(config, dict) and "command" in config:
                    target[name] = config
        except Exception as error:  # noqa: BLE001
            # 配置格式错误时跳过（避免一个坏配置拖垮 Agent 启动），但必须可见：
            # 用户否则会以为这些 MCP server 正常工作，实际一个都没加载。
            self.config_errors.append({
                "path": str(path),
                "error": f"{type(error).__name__}: {error}"[:200],
            })
            print(f"[mcp] config skipped ({path}): {type(error).__name__}: {error}")
