"""EvoHarness Web Server — FastAPI + SSE bridge for the Agent runtime.

不改动 agents/ 核心代码：Agent 的所有终端输出都经过 agents.ui 的打印函数，
而 agent.py 以 `from agents.ui import ...` 方式导入了这些函数。这里在
agents.agent 命名空间里原地替换它们，把助手文本流 / 工具调用 / 工具结果
转为 SSE 事件推送给浏览器。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import time
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(find_dotenv(usecwd=True) or str(ROOT / ".env"), override=False)

# 必须在替换 ui 函数之后再 import Agent（agent.py 会在导入时绑定这些名字）
import agents.agent as agent_module  # noqa: E402
from agents.agent import Agent  # noqa: E402

# ---------------------------------------------------------------------------
# 事件桥：ContextVar 保存当前请求的事件队列，实现多会话并发隔离
# ---------------------------------------------------------------------------

_current_queue: contextvars.ContextVar[asyncio.Queue | None] = contextvars.ContextVar(
    "evoharness_event_queue", default=None
)


def _emit(event: dict) -> None:
    queue = _current_queue.get()
    if queue is not None:
        queue.put_nowait(event)


def _patch_agent_ui() -> None:
    """把 agent.py 绑定的终端打印函数替换为事件发射器。"""

    def print_assistant_text(text: str) -> None:
        _emit({"type": "text", "data": text})

    def print_tool_call(name: str, inp: dict) -> None:
        _emit({"type": "tool_call", "name": name, "input": _jsonable(inp)})

    def print_tool_result(name: str, result: str) -> None:
        _emit({"type": "tool_result", "name": name, "result": str(result)[:20000]})

    def print_info(msg: str) -> None:
        _emit({"type": "info", "data": str(msg)})

    def print_error(msg: str) -> None:
        _emit({"type": "error", "data": str(msg)})

    def print_retry(*args, **kwargs) -> None:
        _emit({"type": "warn", "data": f"重试中: {args} {kwargs}"})

    def print_confirmation(*args, **kwargs) -> None:
        _emit({"type": "warn", "data": "权限请求（Web 模式自动批准）"})

    def print_sub_agent_start(name: str, *args, **kwargs) -> None:
        _emit({"type": "subagent_start", "data": str(name)})

    def print_sub_agent_end(name: str, *args, **kwargs) -> None:
        _emit({"type": "subagent_end", "data": str(name)})

    def print_cost(*args, **kwargs) -> None:
        _emit({"type": "cost", "data": " ".join(str(a) for a in args)})

    def start_spinner(label: str = "Thinking") -> None:
        _emit({"type": "spinner", "data": label})

    def stop_spinner() -> None:
        _emit({"type": "spinner", "data": None})

    def print_divider(*args, **kwargs) -> None:
        pass

    for name, fn in {
        "print_assistant_text": print_assistant_text,
        "print_tool_call": print_tool_call,
        "print_tool_result": print_tool_result,
        "print_info": print_info,
        "print_error": print_error,
        "print_retry": print_retry,
        "print_confirmation": print_confirmation,
        "print_sub_agent_start": print_sub_agent_start,
        "print_sub_agent_end": print_sub_agent_end,
        "print_cost": print_cost,
        "start_spinner": start_spinner,
        "stop_spinner": stop_spinner,
        "print_divider": print_divider,
    }.items():
        setattr(agent_module, name, fn)


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


_patch_agent_ui()

# ---------------------------------------------------------------------------
# API 配置解析（复刻 agents/main.py 的 .env 约定）
# ---------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    return value.strip() or None if value else None


def _resolve_api_config() -> tuple[str | None, str | None, bool]:
    generic_key = _clean(os.environ.get("APIKEY")) or _clean(os.environ.get("EVOHARNESS_API_KEY"))
    anthropic_key = _clean(os.environ.get("ANTHROPIC_API_KEY"))
    openai_key = _clean(os.environ.get("OPENAI_API_KEY"))

    generic_base = _clean(os.environ.get("API")) or _clean(os.environ.get("EVOHARNESS_API_BASE"))
    anthropic_base = _clean(os.environ.get("ANTHROPIC_BASE_URL"))
    openai_base = _clean(os.environ.get("OPENAI_BASE_URL"))

    base = generic_base or openai_base or anthropic_base
    key = generic_key or openai_key or anthropic_key
    use_openai = True
    if base and (base.rstrip("/").lower().endswith("/anthropic")):
        use_openai = False
    elif not openai_base and (anthropic_base or anthropic_key) and not generic_base:
        use_openai = False
    return (base if use_openai else None) or None, key, use_openai


API_BASE, API_KEY, USE_OPENAI = _resolve_api_config()
MODEL = _clean(os.environ.get("MODEL")) or "deepseek-chat"

# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------

AGENTS: dict[str, Agent] = {}
LOCKS: dict[str, asyncio.Lock] = {}
RUNNING: dict[str, bool] = {}

app = FastAPI(title="EvoHarness")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_agent(session_id: str, permission_mode: str) -> Agent:
    if session_id not in AGENTS:
        AGENTS[session_id] = Agent(
            permission_mode=permission_mode,
            model=MODEL,
            api_base=API_BASE if USE_OPENAI else None,
            anthropic_base_url=API_BASE if not USE_OPENAI else None,
            api_key=API_KEY,
        )

        async def auto_confirm(message: str) -> bool:
            _emit({"type": "warn", "data": f"自动批准: {message}"})
            return True

        async def auto_plan_approval(plan_content: str) -> dict:
            _emit({"type": "info", "data": "Plan 已生成，Web 模式自动继续执行"})
            return {"choice": "execute"}

        agent = AGENTS[session_id]
        agent.set_confirm_fn(auto_confirm)
        agent.set_plan_approval_fn(auto_plan_approval)
    return AGENTS[session_id]


class ChatRequest(BaseModel):
    session_id: str
    message: str
    permission_mode: str = "acceptEdits"


class StopRequest(BaseModel):
    session_id: str


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "model": MODEL,
        "protocol": "openai-compatible" if USE_OPENAI else "anthropic",
        "sessions": len(AGENTS),
    }


@app.post("/api/stop")
async def stop(req: StopRequest):
    agent = AGENTS.get(req.session_id)
    if agent:
        agent.abort()
        return {"ok": True}
    return {"ok": False}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    session_id = req.session_id
    LOCKS.setdefault(session_id, asyncio.Lock())
    lock = LOCKS[session_id]

    queue: asyncio.Queue = asyncio.Queue()
    token = _current_queue.set(queue)

    async def live_stream():
        started = time.time()
        yield _sse({"type": "start", "model": MODEL})
        try:
            async with lock:
                RUNNING[session_id] = True
                agent = _get_agent(session_id, req.permission_mode)
                chat_task = asyncio.create_task(agent.chat(req.message))
                while True:
                    get_event = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        {get_event, chat_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if get_event.done() and not get_event.cancelled():
                        yield _sse(get_event.result())
                    elif not get_event.done():
                        get_event.cancel()
                    if chat_task.done():
                        break
                    while not queue.empty():
                        yield _sse(queue.get_nowait())
                try:
                    await chat_task
                except Exception as error:  # noqa: BLE001
                    yield _sse({"type": "error", "data": f"{type(error).__name__}: {error}"})
                yield _sse({
                    "type": "done",
                    "tokens": agent.get_token_usage(),
                    "turns": agent.current_turns,
                    "elapsed": round(time.time() - started, 1),
                })
        finally:
            RUNNING[session_id] = False
            _current_queue.reset(token)
            while not queue.empty():
                yield _sse(queue.get_nowait())
            yield _sse({"type": "end"})

    return StreamingResponse(
        live_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/")
async def index():
    return FileResponse(ROOT / "frontend" / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.app:app", host="127.0.0.1", port=8800, reload=False)
