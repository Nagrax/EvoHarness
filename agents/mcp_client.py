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


# ─── 单个 MCP 连接：一个 McpConnection 对应一个 MCP Server 子进程 ──────────────────


class McpConnection:
    """管理单个 MCP Server 子进程，以及和它之间的 JSON-RPC 通信。"""

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
        )
        # 后台持续读取 stdout。这里不阻塞 connect()，否则后续无法继续初始化。
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """持续读取 MCP Server stdout 中按行分隔的 JSON-RPC 响应。"""
        assert self._process and self._process.stdout
        while True:
            # MCP stdio 通常是一行一个 JSON-RPC 消息。
            line = await self._process.stdout.readline()
            if not line:
                # 读不到内容说明子进程 stdout 已关闭，通常表示进程退出。
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # 非法 JSON 直接忽略，避免单条脏输出打断整个连接。
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

    async def _send_request(self, method: str, params: dict | None = None) -> Any:
        """发送一条 JSON-RPC request，并等待对应 id 的 response。"""
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
        # stdio 传输使用换行作为消息边界。
        self._process.stdin.write((msg + "\n").encode())
        await self._process.stdin.drain()

        # 创建 Future 并登记到 _pending，等待 _read_loop 收到相同 id 的响应后唤醒。
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        return await fut

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
            }
            for t in result["tools"]
        ]

    async def call_tool(self, name: str, args: dict) -> str:
        """调用当前 MCP Server 上的某个工具，并把结果转换成字符串返回。"""
        result = await self._send_request("tools/call", {"name": name, "arguments": args})
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            # MCP 工具结果通常是 content block 列表。当前 Agent 只消费 text block。
            return "\n".join(
                c["text"] for c in result["content"] if c.get("type") == "text"
            )
        # 如果不是标准 content list，就退回为 JSON 字符串，避免丢失信息。
        return json.dumps(result)

    def close(self) -> None:
        """关闭 MCP Server 子进程，并让所有等待中的请求失败。"""
        if self._reader_task:
            # 停止 stdout 后台读取任务。
            self._reader_task.cancel()
            self._reader_task = None
        if self._process:
            try:
                # 直接杀掉子进程，确保外部 MCP Server 不继续残留。
                self._process.kill()
            except ProcessLookupError:
                # 进程已经退出时 kill 可能抛出该异常，忽略即可。
                pass
            self._process = None
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

    def get_tool_definitions(self) -> list[dict]:
        """返回 Agent/Anthropic 可直接使用的工具定义，并给 MCP 工具名加前缀。"""
        return [
            {
                # 前缀格式：mcp__服务名__工具名。
                # 这样可以避免 MCP 工具和内置工具重名，也方便 call_tool() 反向解析路由。
                "name": f"mcp__{t['serverName']}__{t['name']}",
                "description": t.get("description") or f"MCP tool {t['name']} from {t['serverName']}",
                # Anthropic 工具字段叫 input_schema；MCP 原始工具字段一般叫 inputSchema。
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for t in self._tools
        ]

    def is_mcp_tool(self, name: str) -> bool:
        """判断一个工具名是否是 MCP 工具名。"""
        return name.startswith("mcp__")

    async def call_tool(self, prefixed_name: str, args: dict) -> str:
        """把带前缀的 MCP 工具调用路由到正确的 MCP Server。"""
        # 工具名格式为 mcp__serverName__toolName。
        parts = prefixed_name.split("__")
        if len(parts) < 3:
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        server_name = parts[1]
        # 工具名本身也可能包含 "__"，所以第 3 段之后要重新拼回去。
        tool_name = "__".join(parts[2:])  # tool name might contain __
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        return await conn.call_tool(tool_name, args)

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
            raw = json.loads(path.read_text())
            # 支持两种格式：
            # 1. {"mcpServers": {"name": {...}}}
            # 2. {"name": {...}}
            servers = raw.get("mcpServers", raw)
            for name, config in servers.items():
                # 只接收包含 command 的对象；无效配置直接忽略。
                if isinstance(config, dict) and "command" in config:
                    target[name] = config
        except Exception:
            # 配置文件格式错误时跳过，避免一个坏配置导致整个 Agent 启动失败。
            pass
