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


# ---------------------------------------------------------------------------
# Skills 观测台：只读聚合 .bear/skill-evolution/ 审计产物，不重写核心逻辑
# ---------------------------------------------------------------------------

EVOLUTION_DIR = ROOT / ".bear" / "skill-evolution"
ONLINE_EVAL_DIR = EVOLUTION_DIR / "online-eval"

# 六门槛中文名（顺序：证据量三项 + 质量三项）
GATE_META = [
    ("replay", "回放样本", "count", "min_replay_samples"),
    ("promotion", "晋级集样本", "count", "min_promotion_tests"),
    ("retrieved", "检索判断", "count", "min_retrieved"),
    ("used_rate", "使用率", "rate", "min_used_rate"),
    ("relevance_rate", "相关率", "rate", "min_relevance_rate"),
    ("rule_pass", "规则通过率", "rate", "min_rule_pass_rate"),
]


def _read_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _read_jsonl_file(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except Exception:  # noqa: BLE001
        pass
    return rows


@app.get("/api/history")
async def history(session_id: str):
    """返回服务端 Agent 内存中的会话历史（仅用户输入与助手最终文本）。"""
    agent = AGENTS.get(session_id)
    if not agent:
        return {"messages": []}
    out: list[dict] = []
    if agent.use_openai:
        for m in agent._openai_messages:
            role = m.get("role")
            content = m.get("content")
            # 只保留真实对话：系统提示、tool 调用/结果、注入内容都不展示
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                # 剥离检索注入的 skills 上下文块（非用户真实输入）
                text = content.split("<retrieved_skills>")[0].rstrip()
                if text.strip():
                    out.append({"role": role, "text": text[:20000]})
    else:
        for m in agent._anthropic_messages:
            role = m.get("role")
            content = m.get("content")
            if role == "user" and isinstance(content, str) and content.strip():
                text = content.split("<retrieved_skills>")[0].rstrip()
                if text.strip():
                    out.append({"role": "user", "text": text[:20000]})
            elif role == "assistant" and isinstance(content, list):
                text = "".join(
                    str(b.get("text") or "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
                if text:
                    out.append({"role": "assistant", "text": text[:20000]})
    return {"messages": out}


@app.get("/api/skills")
async def skills_overview():
    report = _read_json_file(EVOLUTION_DIR / "online_eval_report.json") or {}
    usage_stats = _read_json_file(EVOLUTION_DIR / "skill_usage_stats.json") or {}
    champions = (_read_json_file(ONLINE_EVAL_DIR / "champions.json") or {}).get("champions", {})
    events = _read_jsonl_file(EVOLUTION_DIR / "usage.jsonl")

    thresholds = report.get("thresholds") or {
        "min_replay_samples": 2, "min_promotion_tests": 1, "min_retrieved": 5,
        "min_used_rate": 0.2, "min_relevance_rate": 0.35, "min_rule_pass_rate": 0.8,
    }

    skills_out: list[dict] = []
    for item in report.get("skills", []):
        name = str(item.get("skill") or "")
        usage = usage_stats.get(name, {}) if isinstance(usage_stats, dict) else {}
        replay = item.get("replay") or {}
        ev = item.get("eval") or {}
        current = {
            "replay": int(replay.get("count", 0) or 0),
            "promotion": int(replay.get("promotion_test", 0) or 0),
            "retrieved": int(item.get("retrieved", 0) or 0),
            "used_rate": float(item.get("used_rate", 0.0) or 0.0),
            "relevance_rate": float(item.get("relevance_rate", 0.0) or 0.0),
            "rule_pass": float(ev.get("pass_rate", 0.0) or 0.0),
        }
        gates = []
        for key, label, kind, threshold_key in GATE_META:
            required = thresholds.get(threshold_key, 0)
            gates.append({
                "key": key, "label": label, "kind": kind,
                "current": current[key], "required": required,
                "ok": current[key] >= float(required),
            })
        # 检索权重（与核心运行时约定一致：healthy ×1.25 / watch ×0.75，孵化期不动）
        status = str(item.get("status") or "")
        weight = 1.0
        if status == "healthy":
            weight = 1.25
        elif status == "watch":
            weight = 0.75
        skills_out.append({
            "skill": name,
            "status": status,
            "reasons": item.get("reasons", []),
            "lineage_id": item.get("lineage_id", ""),
            "current_version": item.get("current_version", ""),
            "source": usage.get("source", ""),
            "last_action": item.get("last_action", ""),
            "last_time": item.get("last_time", ""),
            "counts": {
                "retrieved": int(item.get("retrieved", 0) or 0),
                "relevant": int(item.get("relevant", 0) or 0),
                "used": int(item.get("used", 0) or 0),
                "created": int(item.get("created", 0) or 0),
                "evolutions": int(item.get("evolutions", 0) or 0),
                "invocations": int(item.get("invocations", 0) or 0),
                "feedback": int(item.get("feedback", 0) or 0),
            },
            "rates": {
                "used_rate": current["used_rate"],
                "relevance_rate": current["relevance_rate"],
                "used_when_relevant_rate": float(item.get("used_when_relevant_rate", 0.0) or 0.0),
            },
            "replay": {
                "count": current["replay"],
                "mutate_dev": int(replay.get("mutate_dev", 0) or 0),
                "promotion_test": current["promotion"],
                "sources": replay.get("sources", []),
            },
            "eval": {
                "total_score": float(ev.get("total_score", 0.0) or 0.0),
                "average_score": float(ev.get("average_score", 0.0) or 0.0),
                "pass_rate": current["rule_pass"],
                "hard_failures": int(ev.get("hard_failures", 0) or 0),
                "promotion_test_pass_rate": float(ev.get("promotion_test_pass_rate", 0.0) or 0.0),
                "by_rule": ev.get("by_rule", []),
            },
            "candidate_eval": {
                "candidate_count": int((item.get("candidate_eval") or {}).get("candidate_count", 0) or 0),
                "best_candidate": (item.get("candidate_eval") or {}).get("best_candidate", {}),
                "has_promotion_test_eval": bool((item.get("candidate_eval") or {}).get("has_promotion_test_eval")),
            },
            "gates": gates,
            "retrieval_weight": weight,
            "champion": champions.get(str(item.get("lineage_id") or "")),
            "last_reason": usage.get("last_reason", ""),
            "last_score": float(usage.get("last_score", 0.0) or 0.0),
        })

    events_out = [
        {
            "skill": str(e.get("skill") or ""),
            "event": str(e.get("event") or ""),
            "time": str(e.get("time") or ""),
            "context": str(e.get("context") or ""),
            "source": str(e.get("source") or ""),
            "args_preview": str(e.get("args_preview") or "")[:120],
        }
        for e in events[-200:]
    ][::-1]

    return {
        "generated_at": report.get("generated_at", ""),
        "thresholds": thresholds,
        "aggregate": report.get("aggregate", {}),
        "skills": skills_out,
        "events": events_out,
        "champion_count": len(champions) if isinstance(champions, dict) else 0,
        "has_report": bool(report),
    }


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
            try:
                _current_queue.reset(token)
            except ValueError:
                # agent.chat 的后台任务可能切换了 asyncio Context，导致 token
                # 无法在当前 Context 中 reset；直接清空当前值即可。
                _current_queue.set(None)
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

    # 部署平台（HuggingFace Spaces 等）通过 PORT 注入端口，默认 7860；
    # 本地默认 8800。绑定 0.0.0.0 以便容器外访问。
    uvicorn.run(
        "server.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8800")),
        reload=False,
    )
