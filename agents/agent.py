#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Awaitable, Any

import anthropic
import openai

from agents.mcp_client import McpManager
from agents.memory import MemoryPrefetch, start_memory_prefetch, format_memories_for_injection
from agents.prompt import build_system_prompt
from agents.session_memory import (
    FOLD_SESSION_MEMORY_SYSTEM,
    HANDOVER_SYSTEM,
    build_anthropic_transcript,
    build_folding_user_prompt,
    build_handover_user_prompt,
    build_openai_transcript,
    fallback_folded_memory,
    fallback_handover,
    format_folded_memory,
    format_stage_handover,
    parse_folded_memory,
    parse_handover,
    split_keep_recent_tail,
)
from agents.session import (
    get_stage_dir,
    save_folded_session_memory,
    save_session,
    save_stage_segment,
)
from agents import stages as stage_fsm
from agents.subagent import get_sub_agent_config
from agents.tools import ToolDef, tool_definitions, execute_tool, CONCURRENCY_SAFE_TOOLS, EDIT_TOOLS, check_permission, \
    get_active_tool_definitions
from agents.ui import print_info, print_divider, print_assistant_text, print_sub_agent_start, print_sub_agent_end, \
    start_spinner, stop_spinner, print_cost, print_tool_call, print_tool_result, print_confirmation, print_retry, \
    print_error


# 指数退避重试


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False


def _safe_utf8_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _memory_prefetch_disabled() -> bool:
    """EVOHARNESS_DISABLE_MEMORY_PREFETCH=1 关闭记忆预取。

    这是"轻量 harness design vs 重封装 design"对照的真实开关：
    关掉后每轮少一次旁路模型调用，理论上省 token（是否更划算要实测，不能假设）。
    """
    return _env_flag("EVOHARNESS_DISABLE_MEMORY_PREFETCH")


def _tool_hints_disabled() -> bool:
    """EVOHARNESS_DISABLE_TOOL_HINTS=1 关闭工具安全注解注入。

    提示注入会让系统提示变长（成本）、但可能帮助模型避免危险调用（收益）。
    做成开关才能在真实任务上量出这一取舍。
    """
    return _env_flag("EVOHARNESS_DISABLE_TOOL_HINTS")


def denied_tool_result(kind: str, detail: str = "") -> str:
    """构造"拒绝"类工具结果：文本保持历史措辞，状态机器化为 denied。

    拒绝不是"正文里恰好有 denied 字样"，而是明确的执行事实；调用方读 .status 即可。
    """
    from agents.tools import TOOL_DENIED, ToolResult

    if kind == "deny":
        text = f"Action denied: {detail}"
        code = "permission-denied"
    else:
        text = "User denied this action."
        code = "user-denied"
    return ToolResult(text, status=TOOL_DENIED, error_code=code)


def utf8_tool_result(value: object) -> str:
    """UTF-8 归一化工具结果，但**保留**结构化状态。

    直接 `_safe_utf8_text(raw)` 会把 ToolResult 降级成普通 str，状态随之丢失；
    主循环随后只能退回正文猜测，结构化结果等于白做。
    """
    from agents.tools import FAILURE_STATUSES, ToolResult, coerce_tool_result

    sanitized = _safe_utf8_text(value)
    if isinstance(value, ToolResult):
        return ToolResult(
            sanitized,
            status=value.status,
            error_code=value.error_code,
            exit_code=value.exit_code,
            duration_ms=value.duration_ms,
            artifact_paths=value.artifact_paths,
            metadata=dict(value.metadata),
        )
    # 工具抛异常等历史字符串路径：标记为失败，而不是让正文决定命运。
    lowered = sanitized.lstrip()[:64].lower()
    if lowered.startswith("error executing tool"):
        return ToolResult(sanitized, status="error", error_code="tool-exception")
    return coerce_tool_result(sanitized)


def tool_failure_code(value: object) -> str:
    from agents.tools import coerce_tool_result

    return coerce_tool_result(value).error_code


def _sanitize_for_utf8(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_utf8_text(value)
    if isinstance(value, list):
        return [_sanitize_for_utf8(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_for_utf8(item) for item in value)
    if isinstance(value, dict):
        return {
            _sanitize_for_utf8(key): _sanitize_for_utf8(item)
            for key, item in value.items()
        }
    return value


async def _with_retry(fn, max_retries: int = 3):
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            if attempt >= max_retries or not _is_retryable(error):
                raise
            delay = min(1000 * (2 ** attempt), 30000) / 1000 + (hash(str(time.time())) % 1000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            print_retry(attempt + 1, max_retries, reason)
            await asyncio.sleep(delay)

MODEL_CONTEXT = {
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-sonnet-4-20250514": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "claude-opus-4-20250514": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "deepseek-chat":200000,
    # GAIA 165 题实测：12 题死于 413 上下文超限，且 0.70 折叠阈值从未触发——
    # 200k 假设过大导致折叠窗口计算失真。V3.2 实际窗口 128k。
    "DeepSeek-V3.2": 128000,
}

def _get_context_windows(model:str)->int:
    # 未知模型的 fallback 取保守值：窗口高估会直接 413 崩任务（不可恢复），
    # 低估只是提前折叠（可恢复）——不对称错误结构决定了宁可低估。
    # GAIA 实测 12 题 413 即 200k 高估 fallback 所致。已知模型条目高于此值时
    # 仍以条目为准；可用 EVOHARNESS_CONTEXT_WINDOW 显式覆盖（换 API/代理时）。
    env_override = os.environ.get("EVOHARNESS_CONTEXT_WINDOW")
    if env_override and env_override.strip().isdigit():
        return int(env_override)
    return MODEL_CONTEXT.get(model, 128000)


#多层级压缩常数
SNIP_THRESHOLD = 0.60


def _env_float(name: str, default: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    """环境变量覆盖阈值（越界/非法回退默认）。

    折叠触发点是 Harness² 的核心可优化轴：晚折叠→token 高但状态更全，
    早折叠→省 token 但可能丢状态。把它做成真实可配置，实验才有意义。
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value != value or not (lo < value <= hi):  # NaN / 越界
        return default
    return value


# 折叠触发线：0.70 默认（70% 占用即硬折叠）
AUTO_COMPACT_THRESHOLD = _env_float("EVOHARNESS_AUTO_COMPACT_THRESHOLD", 0.70, lo=0.1, hi=0.95)
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell"}
MICROCOMPACT_IDLE_S = 5 * 60  # 5 minutes

KEEP_RECENT_RESULTS = 3


def _fold_keep_recent_turns() -> int:
    """E-05 尾部保留：折叠时保留最近完整轮数（默认 0 = 沿用整段折叠的旧行为，逐次读取 env）。"""
    try:
        return max(0, int(str(os.environ.get("EVOHARNESS_FOLD_KEEP_RECENT_TURNS", "0")).strip()))
    except (TypeError, ValueError):
        return 0


def _merge_fold_into_user(content: Any, folded_text: str) -> Any | None:
    """把折叠摘要并入首个保留用户消息的内容；含工具结果块等无法安全合并时返回 None。"""
    if isinstance(content, str):
        return folded_text + "\n\n" + content
    if isinstance(content, list) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return [{"type": "text", "text": folded_text}] + content
    return None



def _get_max_output_tokens(model: str) -> int:
    m = model.lower()
    if "opus-4-6" in m:
        return 64000
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384

#转换tool的形式到openai
def _to_openai_tools(tools: list[ToolDef]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


class Agent:
    def __init__(self,
                 *,
                 permission_mode:str="default",
                 model:str="deepseek-chat",
                 api_base: str | None=None,
                 anthropic_base_url: str | None=None,
                 api_key: str | None=None,
                 thinking: bool=False,
                 max_cost_usd: float | None=None,
                 max_turns: int | None=None,
                 confirm_fn:Callable[[str], Awaitable[bool]] | None=None,
                 custom_system_prompt: str | None=None,
                 custom_tools: list[ToolDef] | None=None,
                 is_sub_agent: bool=False,
                 loop_stages: bool=False):
        self.permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        self.use_openai = bool(api_base)
        # 子代理继承父代理的凭据与 base url（父用通用 APIKEY 配置时子代理 api_key=None 会静默失败）。
        self._api_key = api_key
        self._anthropic_base_url = anthropic_base_url
        self.is_sub_agent = is_sub_agent
        self.tools = custom_tools or tool_definitions
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.confirm_fn = confirm_fn
        self._custom_system_prompt = custom_system_prompt
        self.effective_window=_get_context_windows(model) -20000
        self.session_id = uuid.uuid4().hex[:8]
        self.session_start_time= time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        # 最近一次自动存档失败的原因（空串=最近一次成功）。供 UI/CLI 展示脱敏诊断，
        # 避免"会话丢了"与"从未保存过"无法区分。
        self.last_save_error = ""
        self.current_turns = 0
        self.last_api_call_time = 0
        # 旁路查询（side query）单独记账：主循环之外的调用（记忆召回、折叠/交接、
        # 沉淀抽取与使用判定、离线评测）此前不计 token；分开之后才能把"沉淀开销"
        # 从"对话开销"里单独量出来。
        self.side_query_input_tokens = 0
        self.side_query_output_tokens = 0
        self.side_query_tokens_by_label: dict[str, dict[str, int]] = {}


        self._aborted = False
        #存储异步任务
        self._current_task:asyncio.Task | None = None
        #权限白名单
        self._confirmed_paths: set[str] = set()


        # 计划模式”（Plan Mode）状态的变量
        self._pre_plan_mode: str | None=None
        self._plan_file_path: str | None=None
        # 修复（改造二评审 #1）：审批函数是 async，返回 dict（choice/feedback）。
        self._plan_approval_fn : Callable[[str], Awaitable[dict]] | None=None
        self._context_cleared : bool=False

        #思考模式
        self._thinking_mode = self._resolve_thinking_mode()

        #子agent的输出缓存
        self._output_buffer: list[str] | None=None
        self._turn_output_buffer: list[str] | None = None

        # 编辑前读取
        self._read_file_state: dict[str, float] ={}

        #MCP集成
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        #记忆回溯
        #记忆agent已经回答过的信息
        self._already_surfaced_memories: set[str] = set()
        #当前会话占用的字节数
        self._session_memory_bytes = 0

        #区分message的历史消息
        self._anthropic_messages: list[str] = []
        self._openai_messages: list[str] = []
        self._last_retrieved_skill_reference: dict[str, Any] | None = None
        self._last_retrieved_skill_hits: list[dict[str, Any]] = []
        self._pending_skill_extraction_window: dict[str, Any] | None = None
        self._background_skill_tasks: set[asyncio.Task] = set()
        self._folded_session_memories: list[dict[str, Any]] = []
        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._tool_error_streak: int = 0
        self._same_tool_repeat_count: int = 0
        self._last_tool_name: str = ""

        #Loop 阶段状态机（改造二）：仅 --plan CLI 显式置位；REPL /plan toggle、
        #enter_plan_mode 工具与子 Agent 一律不激活——普通 REPL 不进阶段机，
        #子代理（agent.py 两处会被赋 plan 档）不写主 manifest。
        self._loop_stages_active = bool(loop_stages) and not is_sub_agent
        self._stage_manifest: dict[str, Any] | None = None
        self._stage_plan_dir: Path | None = None
        self._stage_close_reason: str = ""
        self._stage_tokens_start: tuple[int, int] = (0, 0)
        self._stage_fold_start: int = 0
        self._stage_artifact_paths: list[dict[str, str]] = []

        #构建系统提示词
        self._base_system_prompt = custom_system_prompt or build_system_prompt()

        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt

        #初始化大模型客户端
        if self.use_openai:
            # openai SDK 在 api_key=None 时**直接抛 OpenAIError**（不是延迟到请求期），
            # 而本地端点（Ollama/vLLM/自建网关）常常免 key。旧代码把 None 原样传进去，
            # 结果是 Web 侧 /api/chat 的 SSE 生成器在构造 Agent 时就炸掉，用户看到的是
            # 一条 Python 异常，而不是"未配置 key"这种可操作提示。这里给本地无凭据情形
            # 一个显式占位串（多数本地端点会忽略 Authorization），并让解析层决定是否允许。
            effective_key = api_key or os.environ.get("EVOHARNESS_LOCAL_PLACEHOLDER_KEY", "local-no-key")
            self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=effective_key)
            self._anthropic_client = None
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        else:
            kwargs : dict[str,Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            if anthropic_base_url:
                kwargs["base_url"] = anthropic_base_url
            self._anthropic_client = anthropic.AsyncAnthropic(**kwargs)
            self._openai_client = None

        self._refresh_runtime_system_prompt()

        if self._loop_stages_active:
            try:
                self._stage_setup()
            except Exception as e:
                # setup 失败按 fail-closed 降级：不激活阶段机，回到普通会话路径。
                self._loop_stages_active = False
                self._stage_manifest = None
                print_error(f"Loop stages setup failed, staging deactivated: {e}")

    #判断返回模型的思考模式
    def _resolve_thinking_mode(self) -> str:
        if not self.thinking:
            return "disabled"
        if not self._model_supports_thinking():
            return "disabled"

        if self._mode_supports_adaptive_thinking(self.model):
            return "adaptive"
        return "enabled"

    def _model_supports_thinking(self) -> bool:
        m = self.model.lower()
        if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
            return False
        if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
            return True
        return False
    def _model_supports_adaptive_thinking(self) -> bool:
        m = self.model.lower()
        return "opus-4-6" in m or "sonnet-4-6" in m

    #生成一个用于保存 AI 计划（Plan）的 Markdown 文件的绝对路径。
    def _generate_plan_file_path(self) -> str:
        d = Path.home() / ".evoharness" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"plan-{self.session_id}.md")

    def _build_plan_mode_prompt(self) -> str:
        return f"""

    # Plan Mode Active

    Plan mode is active. You MUST NOT make any edits (except the plan file below), run non-readonly tools, or make any changes to the system.

    ## Plan File: {self._plan_file_path}
    Write your plan incrementally to this file using write_file or edit_file. This is the ONLY file you are allowed to edit.

    ## Workflow
    1. **Explore**: Read code to understand the task. Use read_file, list_files, grep_search.
    2. **Design**: Design your implementation approach. Use the agent tool with type="plan" if the task is complex.
    3. **Write Plan**: Write a structured plan to the plan file including:
       - **Context**: Why this change is needed
       - **Steps**: Implementation steps with critical file paths
       - **Verification**: How to test the changes
    4. **Exit**: Call exit_plan_mode when your plan is ready for user review.

    IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    #判断当前的任务所有的任务是否完成
    @property
    def is_processing(self)->bool:
        return self._current_task is not None and not self._current_task.done()

    #大模型调用的工厂方法,构建一个用于记忆召回（memory recall）的 sideQuery 可调用对象，兼容anthropic, openai。
    def _build_side_query(self, *, max_tokens: int = 256, label: str = ""):
        if self._anthropic_client:
            client = self._anthropic_client
            model = self.model
            async def _sq(system:str, user_message:str)->str:

                resp = await client.messages.create(
                    model=model, max_tokens=max(1, int(max_tokens)), system=system,
                messages=[{"role": "user", "content": user_message}],
                )
                _usage = getattr(resp, "usage", None)
                self._record_side_query_tokens(
                    getattr(_usage, "input_tokens", 0), getattr(_usage, "output_tokens", 0), label
                )
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                if not text.strip():
                    block_types = [str(getattr(b, "type", "")) for b in getattr(resp, "content", [])]
                    logging.warning(
                        "side_query returned empty Anthropic-compatible response: model=%s stop_reason=%s content_block_types=%s",
                        getattr(resp, "model", model),
                        getattr(resp, "stop_reason", ""),
                        block_types,
                    )
                return text
            return _sq
        if self._openai_client:
            client = self._openai_client
            model = self.model
            async def _sq_openai(system:str, user_message:str)->str:
                resp = await client.chat.completions.create(
                    model=model,
                    max_tokens=max(1, int(max_tokens)),
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],

                )
                _usage = getattr(resp, "usage", None)
                self._record_side_query_tokens(
                    getattr(_usage, "prompt_tokens", 0), getattr(_usage, "completion_tokens", 0), label
                )
                if not resp.choices:
                    logging.warning("side_query returned no OpenAI-compatible choices: model=%s", model)
                    return ""
                choice = resp.choices[0]
                content = choice.message.content or ""
                if not content.strip():
                    logging.warning(
                        "side_query returned empty OpenAI-compatible response: model=%s finish_reason=%s message=%s",
                        model,
                        getattr(choice, "finish_reason", ""),
                        choice.message,
                    )
                return content
            return _sq_openai
        return None

    def _record_side_query_tokens(self, input_tokens: Any = 0, output_tokens: Any = 0, label: str = "") -> None:
        """把一次旁路查询的 token 记进独立计数器（按用途分桶）。

        与主循环的 total_input/output_tokens 分开：主循环每轮全量重发历史，是主要
        开销；旁路查询单次量小但次数多（记忆召回、折叠/交接、沉淀抽取与使用判定、
        离线评测），此前完全没有记账。分开后"沉淀开销"才可被单独量出。
        解析失败或用量缺失时静默跳过，绝不影响调用主流程。
        """
        try:
            inp = max(0, int(input_tokens or 0))
            out = max(0, int(output_tokens or 0))
        except (TypeError, ValueError) as error:  # noqa: BLE001
            # 用量解析失败 = 这次调用没记账 → 预算统计会偏小，不能纯静默。
            print_error(f"token usage parse failed: "
                        f"{type(error).__name__} (in={input_tokens!r}, out={output_tokens!r})")
            return
        if not inp and not out:
            return
        self.side_query_input_tokens += inp
        self.side_query_output_tokens += out
        key = str(label or "unlabeled")
        bucket = self.side_query_tokens_by_label.setdefault(key, {"input": 0, "output": 0})
        bucket["input"] += inp
        bucket["output"] += out

    #异步任务取消（Abort）
    def abort(self) -> None:
        self._aborted = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def set_confirm_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn:Callable[[str], Awaitable[dict]]) -> None:
        self._plan_approval_fn = fn


    #计划模式开关（“状态切换与现场保护”机制）
    def toggle_plan_mode(self) -> str:
        """
               1. 退出计划模式（从 plan 切回原模式）
               当当前模式已经是 plan 时，执行 if 分支：
               恢复之前的状态：self.permission_mode = self._pre_plan_mode or "default"。
                   在进入计划模式时，程序会把原本的模式保存在 _pre_plan_mode 里。退出时，就把它重新拿出来赋值回去，恢复到切换前的状态。
               清理计划模式的痕迹：把 _pre_plan_mode 和 _plan_file_path（计划文件路径）清空，并将系统提示词 _system_prompt 恢复为最基础的 _base_system_prompt。
               同步 OpenAI 消息：如果底层使用的是 OpenAI 接口，它还会同步更新消息列表里的第一条系统提示词，确保 AI 的上下文也跟着切换回来。
               反馈返回：打印退出提示，并返回恢复后的模式名称。

               2. 进入计划模式（从其他模式切入 plan）
       当当前模式不是 plan 时，执行 else 分支：
       保护当前现场：self._pre_plan_mode = self.permission_mode。先把当前正在使用的模式（比如正常模式或自动接受模式）暂存起来，方便以后能原路返回。
       切换并初始化：将当前模式设为 "plan"，生成一个专属的计划文件路径，并扩展系统提示词。通过拼接 _build_plan_mode_prompt()，给 AI 注入“只动脑不动手、输出结构化计划”的专属指令。
       同步 OpenAI 消息：同样地，如果使用 OpenAI，也会实时更新上下文里的系统提示词。
       反馈与返回：打印进入提示（包含计划文件的路径），并返回 "plan"。
        """
        if self.permission_mode == "plan":
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] =self._system_prompt
            print_info(f"Exited plan mode -> {self.permission_mode} mode")
            return self.permission_mode
        else:
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            print_info(f"Entered plan mode. Plan file: {self._plan_file_path}")
            return "plan"

    def get_token_usage(self) -> dict:
        return {"input":self.total_input_tokens, "output":self.total_output_tokens}

    #主入口

    async def  chat(self, user_message:str)->None:
        #懒加载MCP服务在第一次chat的时候
        if not self._mcp_initialized and not self.is_sub_agent:
            self._mcp_initialized = True
            try:
                await self._mcp_manager.load_and_connect()
                # MCP 工具进入 self.tools 后会走协议调用路径，不带 hints
                # （Anthropic/OpenAI 的 tool schema 不认识 MCP hints 字段）
                mcp_defs = self._mcp_manager.get_tool_definitions(with_hints=False)
                if mcp_defs:
                    self.tools = self.tools + mcp_defs
            except Exception as e:
                print_error(f"MCP init failed: {e}")

        original_user_message = _safe_utf8_text(user_message)
        ready_skill_extraction_window: dict[str, Any] | None = None
        self._last_retrieved_skill_reference = None
        self._last_retrieved_skill_hits = []
        if not self.is_sub_agent:
            ready_skill_extraction_window = self._pop_pending_skill_extraction_window(original_user_message)
            user_message, self._last_retrieved_skill_reference = self._augment_user_message_with_skill_context(
                original_user_message
            )

        self._aborted = False
        self._turn_output_buffer = []
        coro = self._chat_openai(user_message) if self.use_openai else self._chat_anthropic(user_message)
        self._current_task = asyncio.create_task(coro)
        try:
            await self._current_task
        except asyncio.CancelledError:
            self._aborted = True

        finally:
            self._current_task = None
        assistant_text = "".join(self._turn_output_buffer or []).strip()
        self._turn_output_buffer = None
        if not self.is_sub_agent and not self._aborted:
            self._schedule_background_skill_task(self._run_skill_usage_tracking(original_user_message, assistant_text))
            if ready_skill_extraction_window:
                self._schedule_background_skill_task(self._run_online_skill_evolution(ready_skill_extraction_window))
            self._set_pending_skill_extraction_window(
                original_user_message=original_user_message,
                assistant_text=assistant_text,
                retrieved_reference=self._last_retrieved_skill_reference,
            )
        if not self.is_sub_agent:
            print_divider()
            # 预算强制收段（改造二评审 #7）：break 前只置标志，收段动作统一在此执行。
            self._finalize_staged_chat()
            self._auto_save()



   #执行一次对话，收集本轮模型输出文本，并返回本轮消耗的 token 数
    async def run_once(self, prompt:str)->None:
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        await self.chat(prompt)
        text = "".join(self._output_buffer)
        self._output_buffer = None
        return {
            "text": text,
            "tokens":{
                "input":self.total_input_tokens-prev_in,
                "output":self.total_output_tokens-prev_out
            },
        }

    #输出工具：统一处理模型输出文本。根据当前是否处于“收集输出”的模式
    # 决定是把文本存进缓冲区，还是直接打印到终端。
    def _emit_text(self, text:str)->None:
        text = _safe_utf8_text(text)
        if self._turn_output_buffer is not None:
            self._turn_output_buffer.append(text)
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            print_assistant_text(text)

    def _build_fold_guidance_section(self) -> str:
        if self._custom_system_prompt is not None:
            return ""
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0.0
        last_fold = "never" if not self._fold_last_time else f"{int((time.time() - self._fold_last_time) / 60)}m ago"
        return (
            "\n\n# Runtime Fold Guidance\n"
            f"- Current context utilization: {utilization:.0%}\n"
            f"- Recent tool error streak: {self._tool_error_streak}\n"
            f"- Same tool repeat count: {self._same_tool_repeat_count}\n"
            f"- Last fold: {last_fold}\n"
            "- If the context is getting long, the same tool is being retried without progress, or tool failures are accumulating, call `compact_context` before trying more tools.\n"
            "- If you folded very recently and the next step is clear, prefer continuing rather than folding again.\n"
        )

    def _refresh_runtime_system_prompt(self) -> None:
        if self._custom_system_prompt is not None:
            return
        self._base_system_prompt = build_system_prompt()
        if self.permission_mode == "plan":
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt
        self._system_prompt += self._build_fold_guidance_section()
        self._system_prompt += self._build_stage_guidance_section()
        if self.use_openai and self._openai_messages:
            self._openai_messages[0]["content"] = self._system_prompt

    def _record_tool_outcome(self, tool_name: str, success: bool) -> None:
        if tool_name == self._last_tool_name:
            self._same_tool_repeat_count += 1
        else:
            self._same_tool_repeat_count = 1
        self._last_tool_name = tool_name
        if success:
            self._tool_error_streak = 0
        else:
            self._tool_error_streak += 1

    def _record_fold_event(self) -> None:
        self._fold_last_time = time.time()
        self._fold_count += 1
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

    #--------------Loop 阶段状态机（改造二）---------------

    def _stage_setup(self) -> None:
        """激活阶段机：plan_id = 本会话 id（uuid hex，目录名安全）；起始段按权限档取
        plan（--plan）或 execute（评测二期强制激活时无 plan 边界的起始段）。"""
        self._stage_plan_dir = get_stage_dir(self.session_id)
        start = "plan" if self.permission_mode == "plan" else "execute"
        self._stage_manifest = stage_fsm.new_manifest(self.session_id, str(self._stage_plan_dir), start_stage=start)
        stage_fsm.write_manifest(self._stage_plan_dir, self._stage_manifest)
        self._stage_tokens_start = (self.total_input_tokens, self.total_output_tokens)
        self._stage_fold_start = self._fold_count
        if self._plan_file_path:
            self._note_artifact(self._plan_file_path, "plan file")
        self._stage_write_segment(stage_fsm.current_record(self._stage_manifest))

    def _note_artifact(self, path: str, why: str = "") -> None:
        """确定性产物采集：write/edit 成功路径、>30KB 工具结果落盘路径、plan 文件。
        确定性工作交给代码，语义筛选才交给模型（advance_stage 的 artifacts 参数）。"""
        if not self._loop_stages_active:
            return
        p = str(path or "").strip()
        if not p:
            return
        if not any(a["path"] == p for a in self._stage_artifact_paths):
            self._stage_artifact_paths.append({"path": p, "why": why})

    def _deterministic_artifacts(self) -> list[dict[str, str]]:
        arts = list(self._stage_artifact_paths)
        if self._plan_file_path and Path(self._plan_file_path).exists():
            arts.append({"path": self._plan_file_path, "why": "plan file"})
        return stage_fsm.normalize_artifacts(arts)

    def _stage_write_segment(self, record: dict[str, Any] | None) -> None:
        """段会话文件（沿用 save_session 载荷形状，restore_session 可直接读回）。"""
        if not self._loop_stages_active or record is None or self._stage_plan_dir is None:
            return
        try:
            save_stage_segment(self._stage_plan_dir, str(record.get("session_file") or ""), {
                "metadata": {
                    "id": self.session_id,
                    "plan_id": self.session_id,
                    "stage": record.get("name"),
                    "status": record.get("status"),
                    "cwd": str(Path.cwd()),
                    "startTime": self.session_start_time,
                    "messageCount": self._get_message_count(),
                    "protocol": "openai" if self.use_openai else "anthropic",
                },
                "anthropicMessages": _sanitize_for_utf8(self._anthropic_messages) if not self.use_openai else None,
                "openaiMessages": _sanitize_for_utf8(self._openai_messages) if self.use_openai else None,
                "foldedSessionMemories": _sanitize_for_utf8(self._folded_session_memories),
            })
        except Exception as error:  # noqa: BLE001
            # 阶段段会话落盘失败不能静默：段文件是阶段交接的唯一载体，
            # 写不进去会让"恢复后接着做"变成"从零重来"，而用户毫无提示。
            self.last_save_error = f"stage-segment-save-failed: {type(error).__name__}: {error}"

    def _stage_record_metrics(self, record: dict[str, Any]) -> None:
        record["tokens"] = {
            "input": self.total_input_tokens - self._stage_tokens_start[0],
            "output": self.total_output_tokens - self._stage_tokens_start[1],
        }
        record["folds"] = self._fold_count - self._stage_fold_start

    def _replace_messages_with_handover(self, content: str) -> None:
        """双协议对称的历史整体替换：OpenAI 保留 system 首消息，Anthropic 以 user 消息开场。"""
        if self.use_openai:
            system_msg = self._openai_messages[0] if self._openai_messages else {"role": "system", "content": self._system_prompt}
            self._openai_messages = [system_msg, {"role": "user", "content": content}]
        else:
            self._anthropic_messages = [{"role": "user", "content": content}]

    async def _generate_handover(self, transcript: str) -> dict[str, Any]:
        """LLM 交接（side_query max_tokens=4000，折叠路径为 6000）；输出不可信 → fallback 兜底。"""
        side_query = self._build_side_query(max_tokens=4000, label="handover")
        if side_query is None:
            return fallback_handover(transcript, self._deterministic_artifacts())
        try:
            raw = await side_query(HANDOVER_SYSTEM, build_handover_user_prompt(transcript))
            payload = parse_handover(raw)
        except Exception:
            return fallback_handover(transcript, self._deterministic_artifacts())
        # 模型产物与确定性采集合并：模型可能漏报，确定性路径不丢。
        payload["artifacts"] = stage_fsm.merge_artifacts(payload.get("artifacts"), self._deterministic_artifacts())
        return payload

    def _reset_segment_counters(self) -> None:
        """收段第⑤步：复位触发状态。同折叠后置 last_input_token_count=0 的既有语义
        （防\"刚折完/刚收完又再触发\"的分叉风暴）。"""
        self.last_input_token_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""
        self._stage_tokens_start = (self.total_input_tokens, self.total_output_tokens)
        self._stage_fold_start = self._fold_count
        self._stage_artifact_paths = []
        if self._plan_file_path:
            self._note_artifact(self._plan_file_path, "plan file")

    async def _close_stage_boundary(
        self,
        *,
        next_stage: str,
        status: str,
        close_reason: str,
        summary: str = "",
        extra_conclusions: list[str] | None = None,
        replace_messages: bool = True,
    ) -> str:
        """通用收段，固定五步（崩溃一致性顺序）：① 交接生成（summary 非空=确定性豁免，
        不调 side_query）→ ② 段文件写盘 → ③ manifest 原子更新 → ④ 内存消息替换 →
        ⑤ 计数复位。磁盘先于内存：中断落在 ①/② manifest 未更新，工作在原段继续。"""
        manifest = self._stage_manifest
        cur = stage_fsm.current_record(manifest)
        cur_name = str(cur.get("name"))
        transcript = (
            build_openai_transcript(_sanitize_for_utf8(self._openai_messages))
            if self.use_openai
            else build_anthropic_transcript(_sanitize_for_utf8(self._anthropic_messages))
        )
        # ① 交接生成
        if summary:
            payload: dict[str, Any] = {
                "conclusions": [summary],
                "artifacts": self._deterministic_artifacts(),
                "constraints": [],
                "next_actions": [],
            }
        else:
            payload = await self._generate_handover(transcript)
        if extra_conclusions:
            payload["conclusions"] = [str(c) for c in extra_conclusions if str(c).strip()] + list(payload.get("conclusions") or [])
        first_conclusion = next((str(c) for c in payload.get("conclusions") or [] if str(c).strip()), "")
        # ② 段文件写盘
        self._stage_record_metrics(cur)
        stage_fsm.close_record(
            cur,
            status=status,
            summary=summary or first_conclusion[:200],
            close_reason=close_reason,
            artifacts=payload.get("artifacts"),
        )
        self._stage_write_segment(cur)
        # ③ manifest 原子更新
        nxt = stage_fsm.open_segment(manifest, next_stage, close_reason=close_reason, advisory_mode=self.permission_mode)
        nxt["handover"] = payload
        stage_fsm.write_manifest(self._stage_plan_dir, manifest)
        # ④ 内存消息替换
        if replace_messages:
            self._replace_messages_with_handover(format_stage_handover(
                payload,
                from_stage=cur_name,
                to_stage=next_stage,
                stage_file=str(Path(self._stage_plan_dir) / str(cur.get("session_file") or "")),
            ))
        # ⑤ 计数复位
        self._reset_segment_counters()
        return summary or first_conclusion[:200]

    async def _stage_on_plan_exit(self, choice: str) -> None:
        """计划→执行边界①（确定性交接，豁免 side_query）：choice ∈ {clear-and-execute,
        execute, manual-execute, auto-exit}。choice=1 清历史重注入计划、choice=2/3 历史延续、
        无审批回退同理——上下文由既有机制承载，只落盘不开新上下文（replace_messages=False）。
        必须在 _clear_history_keep_system() 之前调用（choice=1 要先落盘 plan 段文件）。"""
        if not self._loop_stages_active or self._stage_manifest is None:
            return
        cur = stage_fsm.current_record(self._stage_manifest)
        if cur is None or cur.get("name") != "plan":
            return
        await self._close_stage_boundary(
            next_stage="execute",
            status="completed",
            close_reason=f"plan-approved:{choice}",
            summary=f"Plan approved (choice={choice}); plan file: {self._plan_file_path}",
            replace_messages=False,
        )

    async def _execute_advance_stage_tool(self, inp: dict) -> str:
        if not self._loop_stages_active or self._stage_manifest is None:
            return "advance_stage error: loop stages are not active for this session (staged --plan only)."
        manifest = self._stage_manifest
        cur = stage_fsm.current_record(manifest)
        cur_name = str(cur.get("name"))
        nxt = str(inp.get("stage") or "").strip()
        user_summary = str(inp.get("summary") or "").strip()
        if cur_name == "plan":
            return "advance_stage error: stage 'plan' is closed via exit_plan_mode approval, not advance_stage."
        if cur_name == "deliver":
            return "advance_stage error: 'deliver' is the final stage; wrap up with your final answer instead."
        if not stage_fsm.is_valid_transition(cur_name, nxt):
            expected = stage_fsm.STAGES[stage_fsm.STAGES.index(cur_name) + 1]
            return f"advance_stage error: cannot advance from '{cur_name}' to '{nxt or '(missing)'}'; expected '{expected}'."
        for item in inp.get("artifacts") or []:
            if isinstance(item, dict) and str(item.get("path") or "").strip():
                self._note_artifact(str(item.get("path")), str(item.get("why") or ""))
        await self._close_stage_boundary(
            next_stage=nxt,
            status="completed",
            close_reason=f"advance:{cur_name}->{nxt}",
            extra_conclusions=[user_summary] if user_summary else None,
        )
        # 走既有 _context_cleared 通道（同 compact_context）：结果作为 user 消息追加进
        # 新历史，批内剩余工具 break 掉；旧 assistant 消息连同批内 tool_use 一起消失，
        # Anthropic/OpenAI 均无悬空 tool_use/tool_call。
        self._context_cleared = True
        print_info(f"Stage advanced: {cur_name} -> {nxt}")
        return (
            f"Stage '{cur_name}' closed with handover. You are now in stage '{nxt}'. "
            "Continue from the <stage-handover> now at the start of the conversation."
        )

    async def _force_stage_split(self) -> None:
        """70% 硬阈值改道：同阶段开续段（status=split），段间走 LLM 交接。
        复位在 _close_stage_boundary 第⑤步完成——不复位则段内剩余每轮都会再触发收段。"""
        cur = stage_fsm.current_record(self._stage_manifest)
        name = str(cur.get("name"))
        await self._close_stage_boundary(next_stage=name, status="split", close_reason="threshold:70%")
        print_info(f"Stage segment split (threshold:70%): '{name}' continues from a handover.")

    def _finalize_staged_chat(self) -> None:
        """chat() 收尾的预算强制收段：确定性关闭（不调 side_query——预算已是约束）、
        不替换消息（进程收尾）。续段无交接 payload，resume 时回退最近边界并显式提示。"""
        if not self._loop_stages_active or self._stage_manifest is None or not self._stage_close_reason:
            return
        reason = self._stage_close_reason
        self._stage_close_reason = ""
        manifest = self._stage_manifest
        cur = stage_fsm.current_record(manifest)
        name = str(cur.get("name"))
        self._stage_record_metrics(cur)
        stage_fsm.close_record(
            cur,
            status="split",
            summary=f"Budget stop (deterministic close, no LLM handover): {reason}",
            close_reason=reason,
            artifacts=self._deterministic_artifacts(),
        )
        self._stage_write_segment(cur)
        stage_fsm.open_segment(manifest, name, close_reason=reason, advisory_mode=self.permission_mode)
        stage_fsm.write_manifest(self._stage_plan_dir, manifest)
        self._reset_segment_counters()
        print_info(f"Stage segment closed at budget stop: '{name}' ({reason}).")

    def _build_stage_guidance_section(self) -> str:
        if not self._loop_stages_active or self._stage_manifest is None:
            return ""
        cur = stage_fsm.current_record(self._stage_manifest)
        name = str(cur.get("name"))
        idx = stage_fsm.STAGES.index(name) if name in stage_fsm.STAGES else -1
        nxt = stage_fsm.STAGES[idx + 1] if 0 <= idx < len(stage_fsm.STAGES) - 1 else "(final)"
        return (
            "\n\n# Loop Stages (staged long task)\n"
            f"- Current stage: {name} | Next stage: {nxt}\n"
            "- Stages run plan -> execute -> verify -> deliver. Work within the current stage.\n"
            "- When the stage goal is reached, call `advance_stage` with a summary and key artifacts. "
            "Leave stage 'plan' via exit_plan_mode. At the boundary the raw conversation is replaced "
            "by your handover, so record conclusions and artifact paths before advancing.\n"
        )

    #--------------Loop 阶段状态机结束---------------

    def _looks_like_tool_failure(self, tool_name: str, raw: str, result: str) -> bool:
        """判定工具成功/失败：优先读结构化状态，绝不扫描正文里的词。

        历史实现在正文里找 "error"/"denied"/"timeout"：读一份记录错误码的文档会被误判失败，
        而 `Command failed (exit code 3)` 反而命中不了任何关键词 → 真失败被记成功。
        现在以 ToolResult.status 为准；只有当调用方传入的是裸字符串（历史兼容 / 测试桩 /
        MCP 文本）时才退回保守的失败前缀匹配。
        """
        from agents.tools import FAILURE_STATUSES, ToolResult, coerce_tool_result

        for candidate in (result, raw):
            if isinstance(candidate, ToolResult):
                return coerce_tool_result(candidate).status in FAILURE_STATUSES
        # 裸字符串回退：只认明确表示执行失败的前缀，避免把正文内容当状态。
        for candidate in (raw, result):
            if not candidate:
                continue
            text = str(candidate).lstrip()
            lowered = text[:64].lower()
            if lowered.startswith(("command failed", "command timed out", "error executing tool",
                                   "action denied", "unknown tool", "error: you must read")):
                return True
        if tool_name == "compact_context" and "no context compaction" in f"{raw}\n{result}".lower():
            return True
        return False

    def _augment_user_message_with_skill_context(self, user_message: str) -> tuple[str, dict[str, Any] | None]:
        try:
            from .skills import format_retrieved_skill_context

            context, top_ref = format_retrieved_skill_context(user_message, limit=3)
        except Exception:
            return user_message, None
        if top_ref and isinstance(top_ref.get("all_hits"), list):
            self._last_retrieved_skill_hits = list(top_ref.get("all_hits") or [])
        if not context.strip():
            return user_message, top_ref
        return f"{user_message}\n\n{context}", top_ref

    def _strip_runtime_injections(self, text: str) -> str:
        return re.sub(r"\n*<retrieved_skills>.*?</retrieved_skills>\s*", "", str(text or ""), flags=re.DOTALL).strip()

    def _message_text(self, msg: dict[str, Any]) -> str:
        content = msg.get("content")
        if isinstance(content, str):
            return self._strip_runtime_injections(content)
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif "content" in block and block.get("type") not in {"tool_result", "tool_use"}:
                        parts.append(str(block.get("content") or ""))
            return self._strip_runtime_injections("\n".join(parts))
        return ""

    def _recent_dialog_messages(self, *, max_messages: int = 8) -> list[dict[str, str]]:
        raw_messages = self._openai_messages if self.use_openai else self._anthropic_messages
        out: list[dict[str, str]] = []
        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text = self._message_text(msg)
            if text:
                out.append({"role": role, "content": text})
        return out[-max(2, int(max_messages)) :]

    async def _confirm_online_skill_write(self, summary: str) -> bool:
        if self.permission_mode in {"bypassPermissions", "acceptEdits"}:
            return True
        if self.permission_mode in {"plan", "dontAsk"}:
            return False
        if self.confirm_fn is None:
            return False
        print_confirmation(summary)
        try:
            return bool(await self.confirm_fn(summary))
        except Exception:
            return False

    async def _confirm_background_online_skill_write(self, summary: str) -> bool:
        return self.permission_mode in {"bypassPermissions", "acceptEdits"}

    def _online_evolution_enabled(self) -> bool:
        if os.environ.get("EVOHARNESS_HOLDOUT_READONLY", "0").strip().lower() in {"1", "true", "yes", "on"}:
            return False
        raw = os.environ.get("EVOHARNESS_AUTO_SKILL_EVOLUTION", "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def _schedule_background_skill_task(self, coro) -> None:
        if self.permission_mode == "plan" or not self._online_evolution_enabled():
            try:
                coro.close()
            except Exception:
                pass
            return
        task = asyncio.create_task(coro)
        self._background_skill_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._background_skill_tasks.discard(done_task)
            try:
                done_task.result()
            except Exception as error:  # noqa: BLE001
                # 后台技能任务失败必须留痕：旧实现直接 pass，故障对上层完全不可见
                # （2026-10-29 判读 silent-except 时发现）。
                print(f"[background-skill] task failed: {type(error).__name__}: {error}")

        task.add_done_callback(_done)

    async def drain_background_skill_tasks(self) -> None:
        tasks = [task for task in self._background_skill_tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _pop_pending_skill_extraction_window(self, next_user_feedback: str) -> dict[str, Any] | None:
        pending = self._pending_skill_extraction_window
        self._pending_skill_extraction_window = None
        if not pending:
            return None
        messages = list(pending.get("messages") or [])
        feedback = _safe_utf8_text(next_user_feedback).strip()
        if feedback:
            messages.append({"role": "user", "content": feedback})
        pending["messages"] = messages[-10:]
        pending["next_user_feedback"] = feedback
        return pending

    def _set_pending_skill_extraction_window(
        self,
        *,
        original_user_message: str,
        assistant_text: str,
        retrieved_reference: dict[str, Any] | None,
    ) -> None:
        if os.environ.get("EVOHARNESS_HOLDOUT_READONLY", "0").strip().lower() in {"1", "true", "yes", "on"}:
            return
        if not original_user_message.strip() or not assistant_text.strip():
            return
        self._pending_skill_extraction_window = {
            "messages": self._recent_dialog_messages(max_messages=8),
            "latest_user": original_user_message,
            "latest_assistant": assistant_text,
            "retrieved_reference": self._compact_retrieved_reference(retrieved_reference),
            "session_id": self.session_id,
        }

    def _compact_retrieved_reference(self, ref: dict[str, Any] | None) -> dict[str, Any] | None:
        if not ref:
            return None
        return {k: v for k, v in ref.items() if k != "all_hits"}

    async def _run_online_skill_evolution(self, window: dict[str, Any], *, interactive_confirm: bool = False) -> None:
        if not self._online_evolution_enabled() or self.permission_mode == "plan":
            return
        messages = list(window.get("messages") or [])
        if not messages:
            return

        side_query = self._build_side_query(max_tokens=2200, label="evolution_extract")
        if side_query is None:
            return

        try:
            from .online_skill_evolution import online_ingest
        except Exception as error:  # noqa: BLE001
            # 导入失败 = 整个在线进化被跳过。用户会以为进化在跑，必须可见。
            print_error(f"Online skill evolution unavailable: "
                        f"{type(error).__name__}: {error}")
            return

        result = await online_ingest(
            messages=messages,
            side_query=side_query,
            retrieved_reference=window.get("retrieved_reference") or None,
            hint=str(window.get("hint") or ""),
            confirm_write=self._confirm_online_skill_write if interactive_confirm else self._confirm_background_online_skill_write,
            target=os.environ.get("EVOHARNESS_AUTO_SKILL_TARGET", "project"),
        )
        if result.get("ok"):
            if result.get("action") in {"add", "merge", "revise"}:
                self._refresh_runtime_system_prompt()
                print_info(f"Online skill {result.get('action')}: {result.get('skill')}")
        elif result.get("action") not in {"add_denied", "merge_denied"}:
            print_error(f"Online skill evolution failed: {result.get('error') or result}")

    async def _run_skill_usage_tracking(self, original_user_message: str, assistant_text: str) -> None:
        if not self._online_evolution_enabled() or self.permission_mode == "plan":
            return
        hits = list(self._last_retrieved_skill_hits or [])
        if not hits or not assistant_text.strip():
            return
        side_query = self._build_side_query(max_tokens=700, label="evolution_usage_judge")
        try:
            from .online_skill_evolution import judge_retrieved_skill_usage
            from .skills import record_usage_judgments

            judgments = await judge_retrieved_skill_usage(
                hits=hits,
                user_message=original_user_message,
                assistant_text=assistant_text,
                side_query=side_query,
            )
            result = record_usage_judgments(judgments)
            if result.get("pruned"):
                self._refresh_runtime_system_prompt()
            # 按检索落使用窗口样本（成绩组考卷）：relevant/used 是标注不是门槛，
            # "被检索但没用"的轮次也要记——那是真实的低分答卷。
            try:
                from .skill_evolution import record_usage_samples

                record_usage_samples(
                    judgments,
                    messages=self._recent_dialog_messages(max_messages=8),
                    latest_user=original_user_message,
                    latest_assistant=assistant_text,
                )
            except Exception as error:  # noqa: BLE001
                # 样本缺失会让后续评测/晋升判断少数据，必须可见。
                print_error(f"record_usage_samples failed: "
                            f"{type(error).__name__}: {error}")
        except Exception as error:  # noqa: BLE001
            # 整条在线进化失败不能静默返回：否则"进化没发生"和"进化失败"无法区分。
            print_error(f"Online skill evolution failed: "
                        f"{type(error).__name__}: {error}")
            return

    async def extract_now(self, hint: str = "") -> dict[str, Any]:
        if not self._online_evolution_enabled():
            return {"ok": False, "error": "online skill evolution disabled"}
        pending = self._pending_skill_extraction_window
        if not pending:
            return {"ok": False, "error": "no pending online skill extraction window"}
        window = dict(pending)
        window["hint"] = hint
        await self._run_online_skill_evolution(window, interactive_confirm=True)
        self._pending_skill_extraction_window = None
        return {"ok": True}


    def clear_history(self)->None:
        self._anthropic_messages = []
        self._openai_messages = []
        self._pending_skill_extraction_window = None
        self._last_retrieved_skill_reference = None
        self._last_retrieved_skill_hits = []
        self._fold_last_time = 0.0
        self._fold_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content":self._system_prompt})
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self.side_query_input_tokens = 0
        self.side_query_output_tokens = 0
        self.side_query_tokens_by_label = {}
        print_info("Conversation cleared.")

    def show_cost(self):
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        print_info(
            f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out\n"
            f"  Side-query tokens (memory/fold/evolution): {self.side_query_input_tokens} in / {self.side_query_output_tokens} out\n"
            f"  Estimated cost: ${total:.4f}{budget_info}{turn_info}")

    #获取当前的花费，
    def _get_current_cost_usd(self) -> float:
        return (self.total_input_tokens / 1_000_000) * 3 + (self.total_output_tokens / 1_000_000) * 15

    #检查预算
    def _check_budget(self) -> dict:
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        return {"exceeded": False}

    #压缩会话
    async def compact(self)->None:
        compacted = await self._compact_conversation(trigger="manual")
        if not compacted:
            print_info("Nothing to compact yet.")


    #恢复会话信息
    def restore_session(self, data:dict)->None:
        """恢复会话。

        协议标记（data["protocol"] 或 data["metadata"]["protocol"]）记录上次落盘所用
        协议；跨协议恢复时把来源历史迁移进**当前协议**的列表，避免"上次 OpenAI、
        这次 Anthropic"（或反之）时历史被塞进当前不用的列表而静默丢失。
        """
        protocol = str(
            data.get("protocol") or (data.get("metadata") or {}).get("protocol") or ""
        ).strip().lower()
        anthropic_msgs = data.get("anthropicMessages")
        openai_msgs = data.get("openaiMessages")
        if protocol not in {"openai", "anthropic"}:
            # 无标记（旧会话/未显式传标记）：按 payload 中实际存在的列表推断来源协议。
            if openai_msgs and not anthropic_msgs:
                protocol = "openai"
            elif anthropic_msgs and not openai_msgs:
                protocol = "anthropic"
        current = "openai" if self.use_openai else "anthropic"
        source_msgs = openai_msgs if protocol == "openai" else anthropic_msgs
        if protocol in {"openai", "anthropic"} and protocol != current and source_msgs:
            # 跨协议迁移：来源历史 → 当前协议的表示，灌入当前协议列表。
            migrated = (
                self._anthropic_messages_to_openai(source_msgs)
                if current == "openai"
                else self._openai_messages_to_anthropic(source_msgs)
            )
            if current == "openai":
                system_msg = self._openai_messages[0] if self._openai_messages else {
                    "role": "system", "content": self._system_prompt,
                }
                self._openai_messages = [system_msg] + migrated
            else:
                self._anthropic_messages = migrated
            restored_count = len(migrated)
        else:
            restored_count = 0
            if anthropic_msgs:
                loaded = self._normalize_anthropic_messages(_sanitize_for_utf8(anthropic_msgs))
                self._anthropic_messages = loaded
                restored_count = len(loaded)
            if openai_msgs:
                loaded_openai = _sanitize_for_utf8(openai_msgs)
                self._openai_messages = loaded_openai
                restored_count = len(loaded_openai) if isinstance(loaded_openai, list) else 0
        if isinstance(data.get("foldedSessionMemories"), list):
            self._folded_session_memories = _sanitize_for_utf8(data["foldedSessionMemories"])
        print_info(f"Session restored ({restored_count} messages).")

    def _openai_messages_to_anthropic(self, messages: list[dict]) -> list[dict]:
        """OpenAI 消息 → Anthropic 消息（跨协议迁移用）：跳过 system（走 system 参数）。

        assistant.tool_calls → assistant 的 tool_use 块；role="tool" → user 的
        tool_result 块，与 Anthropic 的成对约束对齐；文本块合并为 content。
        """
        out: list[dict] = []
        for msg in messages if isinstance(messages, list) else []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role == "system":
                continue
            if role == "tool":
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": str(msg.get("tool_call_id") or ""),
                        "content": str(msg.get("content") or ""),
                    }],
                })
                continue
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                blocks: list[dict] = []
                text = msg.get("content")
                if text:
                    blocks.append({"type": "text", "text": str(text)})
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    arguments = fn.get("arguments")
                    try:
                        parsed = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
                    except Exception:
                        parsed = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": str(tc.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "input": parsed if isinstance(parsed, dict) else {},
                    })
                out.append({"role": "assistant", "content": blocks})
                continue
            out.append({"role": role or "user", "content": str(msg.get("content") or "")})
        return self._normalize_anthropic_messages(out)

    def _anthropic_messages_to_openai(self, messages: list[dict]) -> list[dict]:
        """Anthropic 消息 → OpenAI 消息（跨协议迁移用）：tool_use 块 → tool_calls，
        tool_result 块 → role="tool"，文本块合并为 content；system 块不并入 messages。"""
        out: list[dict] = []
        for msg in messages if isinstance(messages, list) else []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role == "system":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                out.append({"role": role or "user", "content": content})
                continue
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            tool_results: list[dict] = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text") or ""))
                elif block_type == "tool_use":
                    tool_calls.append({
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    })
                elif block_type == "tool_result":
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or ""),
                        "content": str(block.get("content") or ""),
                    })
            text = "\n".join(p for p in text_parts if p)
            if tool_calls:
                out.append({"role": "assistant", "content": text or None, "tool_calls": tool_calls})
            elif text:
                out.append({"role": role or "user", "content": text})
            out.extend(tool_results)
        return out



#整理 Anthropic 的历史消息，修正部分角色错误，并丢弃不合法的工具调用消息。
    def _normalize_anthropic_messages(self, messages: list[dict]) -> list[dict]:
        role_normalized = []
        for msg in messages:
            copied = dict(msg)
            content = copied.get("content")
            if copied.get("role") == "user" and isinstance(content, list):
                if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content):
                    copied["role"] = "assistant"
            role_normalized.append(copied)

        normalized = []
        i = 0
        while i < len(role_normalized):
            msg = role_normalized[i]
            tool_use_ids = self._anthropic_tool_use_ids(msg)
            if not tool_use_ids:
                # 孤儿 tool_result（tool_use_id 无对应 tool_use）会产出非法序列，
                # 跨协议迁移尤甚；按上一条已保留的 assistant tool_use 过滤掉无处对应的块。
                cleaned = self._strip_orphan_tool_results(msg, normalized)
                if cleaned is not None:
                    normalized.append(cleaned)
                i += 1
                continue

            # 收集紧跟其后的连续 tool_result 消息：跨协议迁移会把 N 条 OpenAI tool
            # 回复拆成 N 条 user 消息，只按"下一条"补配对会把第 2 条及以后的真实
            # tool_result 当孤儿删除（真实工具输出永久丢失），故一并收集后再合并。
            j = i + 1
            result_msgs = []
            while j < len(role_normalized) and self._anthropic_tool_result_ids(role_normalized[j]):
                result_msgs.append(role_normalized[j])
                j += 1

            if not result_msgs:
                # 无任何对应 tool_result：只剔 tool_use、保留 text 块（正文/推理），
                # 与孤儿 user tool_result 的处理一致，避免连带丢弃正文。
                stripped = self._strip_assistant_tool_use(msg)
                if stripped is not None:
                    normalized.append(stripped)
                i += 1
                continue

            result_ids = set()
            for rm in result_msgs:
                result_ids |= self._anthropic_tool_result_ids(rm)

            # 部分覆盖：保留 assistant 与全部真实 tool_result，仅为缺失的 tool_use
            # 补合成占位结果——保持成对，且绝不用占位覆盖真实输出，也避免产生
            # "只有 tool_result、没有 tool_use"的非法序列。
            missing = tool_use_ids - result_ids
            normalized.append(msg)
            blocks: list = []
            for rm in result_msgs:
                for block in rm.get("content") or []:
                    # 合并后仍需剔除无处对应的孤儿 tool_result（tool_use_id 无对应 tool_use）：
                    # 否则其会与其后合法结果并进同一条 user 消息保留，产出非法序列（API 400）。
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        and block.get("tool_use_id") not in tool_use_ids
                    ):
                        continue
                    blocks.append(block)
            blocks.extend({
                "type": "tool_result", "tool_use_id": tid,
                "content": "Missing tool_result repaired during history normalization.",
            } for tid in missing)
            normalized.append({**result_msgs[0], "content": blocks})
            i = j
        return normalized

    @staticmethod
    def _anthropic_tool_use_ids(msg: dict | None) -> set[str]:
        if not msg or msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            return set()
        return {
            block.get("id")
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
        }

    @staticmethod
    def _anthropic_tool_result_ids(msg: dict | None) -> set[str]:
        if not msg or msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            return set()
        return {
            block.get("tool_use_id")
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")
        }

    @staticmethod
    def _strip_orphan_tool_results(msg: dict, normalized: list[dict]) -> dict | None:
        """过滤消息中无对应 tool_use 的孤儿 tool_result。

        仅对 user 列表消息生效：以上一条已保留消息中的 assistant tool_use id 为有效集合，
        丢弃 tool_use_id 不在其中的 tool_result 块。若过滤后仅剩空内容返回 None（整条丢弃），
        否则返回带过滤后 content 的副本；非目标形态原样返回。
        """
        if not (isinstance(msg, dict) and msg.get("role") == "user" and isinstance(msg.get("content"), list)):
            return msg
        content = msg["content"]
        valid = Agent._anthropic_tool_use_ids(normalized[-1]) if normalized else set()
        filtered = [
            block for block in content
            if not (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") not in valid
            )
        ]
        if len(filtered) == len(content):
            return msg
        if not filtered:
            return None
        return {**msg, "content": filtered}

    @staticmethod
    def _strip_assistant_tool_use(msg: dict) -> dict | None:
        """剔除 assistant 列表消息中的孤儿 tool_use 块，保留 text 块（正文/推理）。

        未配对的 assistant tool_use 会产出非法序列，需剔除；但不得连带丢弃同一
        消息里的 text 块（与对孤儿 user tool_result 的处理一致）。剔除后无剩余
        块返回 None（整条丢弃），非目标形态返回 None。
        """
        if not (isinstance(msg, dict) and msg.get("role") == "assistant" and isinstance(msg.get("content"), list)):
            return None
        filtered = [
            block for block in msg["content"]
            if not (isinstance(block, dict) and block.get("type") == "tool_use")
        ]
        if not filtered:
            return None
        return {**msg, "content": filtered}

    def _get_message_count(self) -> int:
        return len(self._openai_messages) if self.use_openai else len(self._anthropic_messages)

    def _auto_save(self) -> None:
        if os.environ.get("EVOHARNESS_HOLDOUT_READONLY", "0").strip().lower() in {"1", "true", "yes", "on"}:
            return
        try:
            save_session(self.session_id, {
                "metadata": {
                    "id": self.session_id,
                    "model": self.model,
                    "cwd": str(Path.cwd()),
                    "startTime": self.session_start_time,
                    "messageCount": self._get_message_count(),
                    # 协议标记：--resume 跨协议恢复时据此把历史迁进当前协议的列表，
                    # 避免"上次 OpenAI、这次 Anthropic"导致的静默丢历史。
                    "protocol": "openai" if self.use_openai else "anthropic",
                    # staged 会话标记（改造二评审 #8）：--resume 用 plan_dir_abs 绝对路径
                    # 定位阶段目录，跨 cwd 可恢复。
                    **(
                        {"loop_staged": True, "plan_id": self.session_id, "plan_dir_abs": str(self._stage_plan_dir)}
                        if self._loop_stages_active else {}
                    ),
                },
                "anthropicMessages": _sanitize_for_utf8(self._anthropic_messages) if not self.use_openai else None,
                "openaiMessages": _sanitize_for_utf8(self._openai_messages) if self.use_openai else None,
                "foldedSessionMemories": _sanitize_for_utf8(self._folded_session_memories),
            })
            # 复用每轮落盘节奏刷新当前段文件（崩溃最多丢一轮，同全局存档语义）。
            if self._loop_stages_active:
                self._stage_write_segment(stage_fsm.current_record(self._stage_manifest))
            self.last_save_error = ""
        except Exception as error:  # noqa: BLE001
            # 保存失败不能静默：旧实现 `except Exception: pass` 让"会话丢了"与
            # "会话保存失败"在用户侧无法区分（面试反复追问的可见性问题）。
            self.last_save_error = f"{type(error).__name__}: {error}"
            try:
                from agents.memory import record_encoding_event
                record_encoding_event("session_save_failed", Path(self.session_id or "session"),
                                      self.last_save_error[:400])
            except Exception:  # noqa: BLE001
                # 诊断自身失败不再递归抛错；原因已在 last_save_error 中保留。
                pass

    #自动压缩
    async def _check_and_compact(self)->None:
        # E-05 对照组 S1（无折叠）：显式禁用一切自动压缩/折叠。
        # 只挡"自动"路径——模型/用户主动调用 compact_context 工具仍可用，
        # 保证策略对照测的是"被动压缩策略"，不是"完全失去压缩能力"。
        if _env_flag("EVOHARNESS_DISABLE_AUTO_FOLD"):
            return
        if self._loop_stages_active:
            # 阶段机激活：70% 硬线改道为强制收段（LLM 交接，status=split）——
            # 0.70 保留为最后防线；复位在收段第⑤步完成，防"刚收完又收"分叉风暴。
            if self.last_input_token_count > self.effective_window * AUTO_COMPACT_THRESHOLD:
                print_info("Context window filling up, forcing stage segment handover...")
                await self._force_stage_split()
            return
        if self.last_input_token_count > self.effective_window * AUTO_COMPACT_THRESHOLD:
            print_info("Context window filling up, compacting conversation...")
            await self._compact_conversation(trigger="auto")

    async def _compact_conversation(self, *, trigger: str = "manual")->bool:
        if self.use_openai:
            compacted = await self._compact_openai(trigger=trigger)
        else:
            compacted = await self._compact_anthropic(trigger=trigger)
        if compacted:
            print_info("Conversation compacted.")
        return compacted

    async def _compact_anthropic(self, *, trigger: str)->bool:
        if len (self._anthropic_messages)<4:
            return False

        keep = _fold_keep_recent_turns()
        older, recent = split_keep_recent_tail(self._anthropic_messages, keep, anthropic=True)
        if keep > 0 and not older:
            return False  # 无老段可折：不整段折叠，避免丢掉最近轮
        transcript = build_anthropic_transcript(_sanitize_for_utf8(older))
        if not transcript.strip():
            return False
        memory = await self._generate_folded_session_memory(transcript)
        self._record_folded_session_memory(trigger, memory)
        self._record_fold_event()
        folded_text = format_folded_memory(memory)
        if recent:
            # 尾部保留：摘要并入 recent 首个用户消息，维持 Anthropic 角色交替合法。
            merged = _merge_fold_into_user(recent[0].get("content"), folded_text)
            if merged is not None:
                self._anthropic_messages = [{"role": "user", "content": merged}] + recent[1:]
            else:
                self._anthropic_messages = [{"role": "user", "content": folded_text}] + recent
        else:
            self._anthropic_messages = [{"role": "user", "content": folded_text}]
        self._append_fold_archive_hint()
        self.last_input_token_count = 0
        self._refresh_runtime_system_prompt()
        return True

    async def _compact_openai(self, *, trigger: str)->bool:
        if len (self._openai_messages)<4:
            return False
        system_msg = self._openai_messages[0]
        keep = _fold_keep_recent_turns()
        older, recent = split_keep_recent_tail(self._openai_messages[1:], keep, anthropic=False)
        if keep > 0 and not older:
            return False  # 无老段可折：不整段折叠，避免丢掉最近轮
        source = older if keep > 0 else self._openai_messages
        transcript = build_openai_transcript(_sanitize_for_utf8(source))
        if not transcript.strip():
            return False
        memory = await self._generate_folded_session_memory(transcript)
        self._record_folded_session_memory(trigger, memory)
        self._record_fold_event()
        folded_text = format_folded_memory(memory)
        if recent:
            merged = _merge_fold_into_user(recent[0].get("content"), folded_text)
            if merged is not None:
                self._openai_messages = [system_msg, {"role": "user", "content": merged}] + recent[1:]
            else:
                self._openai_messages = [system_msg, {"role": "user", "content": folded_text}] + recent
        else:
            self._openai_messages=[system_msg, {"role": "user", "content": folded_text}]
        self._append_fold_archive_hint()
        self.last_input_token_count=0
        self._refresh_runtime_system_prompt()
        return True

    async def _generate_folded_session_memory(self, transcript: str) -> dict[str, Any]:
        side_query = self._build_side_query(max_tokens=6000, label="fold")
        if side_query is None:
            return fallback_folded_memory(transcript)
        try:
            raw = await side_query(FOLD_SESSION_MEMORY_SYSTEM, build_folding_user_prompt(transcript))
            return parse_folded_memory(raw)
        except Exception:
            return fallback_folded_memory(transcript)

    def _record_folded_session_memory(self, trigger: str, memory: dict[str, Any]) -> None:
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "session_id": self.session_id,
            **memory,
        }
        self._folded_session_memories.append(record)
        try:
            save_folded_session_memory(self.session_id, _sanitize_for_utf8(record))
            # 落盘成功：折叠已被别的路径（内存列表）保住，但"存档文件能否重读"
            # 是折叠安全网的前提，所以这里同样要清掉陈旧诊断。
            self.last_save_error = ""
        except Exception as error:  # noqa: BLE001
            # 折叠记录落盘失败同样不能静默：内存里有、磁盘上没有，重启后
            # `--resume` 会把"折叠过"变成"从未折叠"，用户侧无从分辨。
            self.last_save_error = f"folded-memory-save-failed: {type(error).__name__}: {error}"

    def _append_fold_archive_hint(self) -> None:
        """折叠后在注入文本尾部附存档路径：丢状态时 agent 可 read_file 恢复完整折叠记录。

        依据 GAIA 消融（p=0.011）：折叠状态保真度不足导致返工与超时。存档文件是
        折叠摘要之外的安全网——环境即数据库，上下文丢了可以重读。
        """
        try:
            from .session import get_project_session_dir

            archive = get_project_session_dir() / f"{self.session_id}.folded-memory.latest.json"
            hint = (
                f"\n\n[fold archive] The full folded state (all key_events/tool_memory fields) "
                f"is archived at {archive}. If you notice missing file paths, prior results, "
                f"or task constraints after this fold, read_file that archive to recover them "
                f"instead of redoing work."
            )
            if self.use_openai and self._openai_messages:
                self._openai_messages[-1]["content"] = str(self._openai_messages[-1].get("content") or "") + hint
            elif self._anthropic_messages:
                self._anthropic_messages[-1]["content"] = str(self._anthropic_messages[-1].get("content") or "") + hint
        except Exception:
            pass

    #多层级压缩流水线
    def _run_compression_pipeline(self)->None:
        if self.use_openai:
            self._budget_tool_results_openai()
            self._snip_stale_results_openai()
            self._microcompact_openai()
        else:
            self._budget_tool_results_anthropic()
            self._snip_stale_results_anthropic()
            self._microcompact_anthropic()

    #第一层级压缩，预算压缩
    def _budget_tool_results_anthropic(self)->None:
        #计算利用率：utilization = 已用Token / 有效窗口大小。
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        #如果利用率低于 50%，说明空间还很充裕，直接返回，不做任何处理。
        if utilization < 0.5:
            return
        #动态预算（Budget）：危急状态（>70%）：如果利用率很高，允许单个工具结果保留 15,000 个字符。
        # 警戒状态（50%-70%）：如果利用率中等，只允许保留 30000 个字符。
        budget = 15000 if utilization > 0.7 else 30000

        for msg in self._anthropic_messages:

            #只处理 role 为 "user" 的消息。在工具调用流程中，工具的执行结果通常是以“用户”的身份反馈给模型的。

            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and len(block["content"]) > budget:
                    #计算保留长度 (keep)：keep = (budget - 80) // 2 这里预留了约 80 个字符的空间给中间的提示语，剩下的长度平分给开头和结尾。
                    keep = (budget - 80) // 2
                    #重组新内容 = 开头部分 + 提示语 + 结尾部分
                    block["content"] = block["content"][:keep] + f"\n\n[... budgeted: {len(block['content']) - keep * 2} chars truncated ...]\n\n" + block["content"][-keep:]

    def _budget_tool_results_openai(self)->None:
        #计算利用率：utilization = 已用Token / 有效窗口大小。
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        #如果利用率低于 50%，说明空间还很充裕，直接返回，不做任何处理。
        if utilization < 0.5:
            return
        #动态预算（Budget）：危急状态（>70%）：如果利用率很高，允许单个工具结果保留 15,000 个字符。
        # 警戒状态（50%-70%）：如果利用率中等，只允许保留 30000 个字符。
        budget = 15000 if utilization > 0.7 else 30000

        for msg in self._openai_messages:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > budget:
                keep = (budget - 80) // 2
                msg["content"] = msg["content"][:keep] + f"\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n" + msg["content"][-keep:]


    #第二级策略：修剪过期的工具执行结果
    def _snip_stale_results_anthropic(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        results = []
        for mindex,  msg in enumerate(self._anthropic_messages):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue

            for bindex, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] != SNIP_PLACEHOLDER:
                    tool_use_id = block.get("tool_use_id")
                    # 对每个 tool_result，通过 tool_use_id 反查它来自哪个工具
                    tool_info = self._find_tool_use_by_id(tool_use_id)
                    if tool_info and tool_info["name"] in SNIPPABLE_TOOLS:
                        results.append({"mindex": mindex, "bindex": bindex, "name": tool_info["name"], "file_path": tool_info.get("input", {}).get("file_path")})

        if len(results) <= KEEP_RECENT_RESULTS:
            return

        to_snip =  set()
        seen_files: dict[str, list[int]] = {}

        for i, r in enumerate(results):
            if r["name"] == "read_file" and r.get("file_path"):
                seen_files.setdefault(r["file_path"], []).append(i)
        #如果一个文件被读取了多次，只保留最后一次读取的结果，把前面几次读取的内容全部标记为“修剪”（Snip）。
        for indices in seen_files.values():
            if len (indices) >1 :
                for j in indices[:-1]:
                    to_snip.add (j)

        snip_before = len(results) - KEEP_RECENT_RESULTS
        for i in range (snip_before):
            to_snip.add(i)

        for idx in to_snip:
            r = results[idx]
            self._anthropic_messages[r["mindex"]]["content"][r["bindex"]]["content"] = SNIP_PLACEHOLDER

    def _snip_stale_results_openai(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] != SNIP_PLACEHOLDER:
                tool_msgs.append(i)
        if len(tool_msgs) <= KEEP_RECENT_RESULTS:
            return
        snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(snip_count):
            self._openai_messages[tool_msgs[i]]["content"] = SNIP_PLACEHOLDER

    #微压缩

    #基于“时间”的上下文瘦身策略，
    #如果已经很久没说话了，说明之前的工具执行结果你已经看完了，那就把它们清理掉，腾出空间

    def _microcompact_anthropic(self) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return

        all_results = []
        for mindex, msg in enumerate(self._anthropic_messages):
            if msg.get("role")!="user" or not isinstance(msg.get("content"), list):
                continue
            for bindex, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                    all_results.append((mindex, bindex))

        clear_count = len(all_results) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            mi, bi = all_results[i]
            self._anthropic_messages[mi]["content"][bi]["content"] = "[Old result cleared]"

    def _microcompact_openai(self) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                tool_msgs.append(i)
        clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            self._openai_messages[tool_msgs[i]]["content"] = "[Old result cleared]"

    def _find_tool_use_by_id(self, tool_use_id: int) -> dict | None:
        for msg in self._anthropic_messages:
            if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
                continue

            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                    return {"name": block["name"], "input": block.get("input", {})}

    #大结果持久化
    #如果工具返回的结果太大（超过 30KB），不要硬塞进上下文里，而是把它存成一个临时文件。
    # 然后在对话里只留一个‘文件路径’和‘内容预览’。如果模型后面还需要看完整内容，它可以再次调用工具去读取这个文件

    def _persist_large_result(self, tool_name: str, result: str) -> str:
        THRESHOLD = 30 * 1024  # 30 KB
        #转换成字节
        if (len (result.encode())) <= THRESHOLD:
            return result

        d = Path.home() / ".evoharness" / "tool-results"
        d.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
        filepath = d / filename
        filepath.write_text(result, encoding="utf-8")
        # 确定性产物采集（改造二）：落盘路径进当前段 artifacts 清单。
        self._note_artifact(str(filepath), f"{tool_name} result dump (>30KB)")

        lines = result.split("\n")
        preview = "\n".join(lines[:200])
        size_kb = len(result.encode()) / 1024

        return (
            f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
            f"Full output saved to {filepath}. "
            f"You can use read_file to see the full result.]\n\n"
            f"Preview (first 200 lines):\n{preview}"
        )

    #执行工具入口

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        if name == "compact_context":
            return await self._execute_compact_context_tool(inp)
        if name == "advance_stage":
            # Loop 阶段边界（改造二）：走 _context_cleared 通道，同 compact_context。
            return await self._execute_advance_stage_tool(inp)
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
            # Route MCP tool calls to the MCP manager
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        result = await execute_tool(name, inp, self._read_file_state)
        if name in {"write_file", "edit_file"}:
            # 确定性产物采集（改造二）：只有**真正成功**的写入才登记产物。
            # 失败调用不得留下 artifact，否则"假产物"会污染阶段交接与交付清单。
            from agents.tools import tool_result_ok
            path_in = (inp or {}).get("file_path") or (inp or {}).get("path")
            if path_in and tool_result_ok(result):
                self._note_artifact(str(path_in), name)
        if name in {"skill_create", "skill_evolve"}:
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("ok"):
                    self._refresh_runtime_system_prompt()
            except Exception:
                pass
        return result

    async def _execute_compact_context_tool(self, inp: dict) -> str:
        reason = str(inp.get("reason") or "").strip()
        compacted = await self._compact_conversation(trigger="tool")
        if not compacted:
            self._record_tool_outcome("compact_context", False)
            return "No context compaction was performed because there is not enough conversation history yet."
        self._record_tool_outcome("compact_context", True)
        self._context_cleared = True
        suffix = f"\nReason: {reason}" if reason else ""
        return (
            "Context compacted into structured session memory. "
            "Continue from the folded memory now present in the conversation context."
            f"{suffix}"
        )


    async def _execute_skill_tool(self, inp: dict) -> str:
        from .skills import execute_skill
        result = execute_skill(inp.get("skill_name", ""), inp.get("args", ""))

        if not result:
            return f"Unknown skill: {inp.get('skill_name', '')}"

        #fork 表示这个 skill 不直接把 prompt 塞回当前对话，而是要启动一个子 Agent 单独完成任务。
        if result["context"] == "fork":
            try:
                # fork 禁令统一裁决（试用期状态机门 + 实验区分区门，见 skill_tiers.fork_blocked_reason）：
                # 运行时动态读两张表——另一进程的晋升/回退在下一次 fork 请求即生效，不烤进 skill 缓存。
                from .skill_tiers import fork_blocked_reason

                blocked_reason = fork_blocked_reason(str(inp.get("skill_name") or ""))
            except Exception:
                # fail-open：分区模块缺失时退回放行（缺省 core 可 fork），缺口由审计暴露。
                blocked_reason = ""
            if blocked_reason:
                # 试用期/实验区隔离：未验证的 skill 禁止 fork（带工具的子代理是爆炸半径最大的出口），降级 inline。
                print_info(
                    f"Skill '{inp.get('skill_name', '')}' is gated ({blocked_reason}); fork downgraded to inline."
                )
                return (
                    f'[Skill "{inp.get("skill_name", "")}" activated in inline trial mode ({blocked_reason}); '
                    f"fork execution is disabled until the skill is validated.]\n\n{result['prompt']}"
                )
            # result["allowed_tools"] - 直接访问
            tools = (
                [t for t in self.tools if t["name"] in  result["allowed_tools"] ]
                #result.get("allowed_tools") - 安全访问
                # 存在key：返回对应的值（可能是 None、[]、["tool1"] 等）
                # 不存在key：返回 None（不会抛异常）
                if result.get("allowed_tools")
                else  [t for t in self.tools if t["name"] != "agent"]
            )

            print_sub_agent_start("skill-fork", inp.get("skill_name", ""))
            sub_agent = Agent(
                model=self.model,
                api_base=str(self._openai_client.base_url) if self.use_openai and self._openai_client else None,
                api_key=self._api_key,
                anthropic_base_url=self._anthropic_base_url,
                custom_system_prompt=result["prompt"],
                custom_tools=tools,
                is_sub_agent=True,
                # 子代理继承父代理的权限档：不得默认升级为 bypassPermissions，
                # 否则一次 skill fork 即可绕过五档门禁 / confirm / 用户 deny 规则。
                permission_mode=self.permission_mode,
                # 一并继承父代理的确认通道：子代理在 confirm 判定下必须复用父的
                # confirm_fn（本地自动批准 / CLI y/n / Web 确认），不得退化到 input()
                # ——EOF 时静默拒绝（工具白跑/误判失败），TTY 下阻塞事件循环。
                confirm_fn=self.confirm_fn,
            )
            try:
                sub_result = await sub_agent.run_once(inp.get("args") or "Execute this skill task.")
                self.total_input_tokens += sub_result["tokens"]["input"]
                self.total_output_tokens += sub_result["tokens"]["output"]
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return sub_result["text"] or "(Skill produced no output)"
            except Exception as e:
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return f"Skill fork error: {e}"

        return f'[Skill "{inp.get("skill_name", "")}" activated]\n\n{result["prompt"]}'

    async def _execute_plan_mode_tool(self, name):
        if name == "enter_plan_mode":
            if self.permission_mode == "plan":
                return "Already in plan mode."
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path =  self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info("Entered plan mode (read-only). Plan file: " + self._plan_file_path)
            return f"Entered plan mode. You are now in read-only mode.\n\nYour plan file: {self._plan_file_path}\nWrite your plan to this file. This is the only file you can edit.\n\nWhen your plan is complete, call exit_plan_mode."
        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                return "Not in plan mode."
            plan_content = "(No plan file found)"
            if self._plan_file_path and Path(self._plan_file_path).exists():
                # 审批面板/回显需展示计划正文，而非文件路径。
                # 读取加异常保护：非 UTF-8/IO 失败若直接抛出，会中断审批流程、
                # permission_mode 滞留 plan 档；此处退化为安全兜底文本，保证审批继续。
                try:
                    plan_content = Path(self._plan_file_path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    plan_content = "(Plan file could not be read as UTF-8; approval continues with this placeholder)"
            # 交互式审批流程（如果有审批函数）
            if self._plan_approval_fn:
                # P0 修复（改造二评审 #1）：审批函数是 async，原实现缺 await——拿到的是
                # coroutine，result.get 抛 AttributeError 后被主循环吞成 tool_result，
                # 审批体从未执行、permission_mode 滞留 plan、REPL 4 选项从未真实弹出。
                result = await self._plan_approval_fn(plan_content)
                choice = result.get("choice", "manual-execute")

                if choice =="keep-planning":
                    feedback = result.get("feedback") or "Please revise the plan."
                    return (
                        f"User rejected the plan and wants to keep planning.\n\n"
                        f"User feedback: {feedback}\n\n"
                        f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                    )

                if choice == "clear-and-execute":
                    target_mode = "acceptEdits"
                elif choice == "execute":
                    target_mode = "acceptEdits"
                else:  # manual-execute
                    target_mode = self._pre_plan_mode or "default"

                #离开计划模式
                # P0 修复（改造二评审 #1 补充，探针实证）：原实现误写
                # self._pre_plan_mode = target_mode，permission_mode 从未迁移——
                # 审批通过后模型仍滞留 plan 档（只读），"Executing in acceptEdits
                # mode" 只是提示词。三入口共用此分支，必须真实迁移。
                self.permission_mode = target_mode
                self._pre_plan_mode = None
                # 计划→执行边界①（审批分支，改造二）：确定性交接、豁免 side_query。
                # 顺序约束：在 permission_mode 迁移之后（advisory_mode 记录真实执行档）、
                # _clear_history_keep_system() 之前（choice=1 要先落盘 plan 段文件）。
                await self._stage_on_plan_exit(choice)
                saved_plan_path = self._plan_file_path
                self._plan_file_path = None
                self._system_prompt = self._base_system_prompt
                if self.use_openai and self._openai_messages:
                    self._openai_messages[0]["content"] = self._system_prompt

                if choice == "clear-and-execute":
                    self._clear_history_keep_system()
                    self._context_cleared = True
                    print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                    return (
                        f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                        f"Plan file: {saved_plan_path}\n\n"
                        f"## Approved Plan:\n{plan_content}\n\n"
                        f"Proceed with implementation."
                    )
                print_info(f"Plan approved. Executing in {target_mode} mode.")
                return (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    f"Proceed with implementation."
                )
            # 没有审批函数时的回退（例如子代理）
            # 计划→执行边界①（无审批回退分支，改造二评审 #2）：一次性 --plan 走这里，
            # 同样开 execute 段（确定性交接、豁免 side_query）。钩子在模式恢复之后，
            # 让 advisory_mode 记录真实执行档。
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            await self._stage_on_plan_exit("auto-exit")
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt

            print_info("Exited plan mode. Restored to " + self.permission_mode + " mode.")
            return f"Exited plan mode. Permission mode restored to: {self.permission_mode}\n\n## Your Plan:\n{plan_content}"

        return f"Unknown plan mode tool: {name}"

    def _clear_history_keep_system(self) -> None:
        """清空历史信息，但是保留系统prompt."""
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.last_input_token_count = 0
        self._fold_last_time = 0.0
        self._fold_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

    async def _execute_agent_tool(self, inp:dict) -> str:
        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        print_sub_agent_start(agent_type, description)

        config = get_sub_agent_config(agent_type)

        sub_agent = Agent(
            model=self.model,
            api_base=str(self._openai_client.base_url) if self.use_openai and self._openai_client else None,
            api_key=self._api_key,
            anthropic_base_url=self._anthropic_base_url,
            custom_system_prompt=config["system_prompt"],
            custom_tools=config["tools"],
            is_sub_agent=True,
            # 子代理继承父代理的权限档：不得默认升级为 bypassPermissions，
            # 否则一次 agent 调用即可绕过五档门禁 / confirm / 用户 deny 规则。
            permission_mode=self.permission_mode,
            # 一并继承父代理的确认通道：子代理在 confirm 判定下必须复用父的
            # confirm_fn（本地自动批准 / CLI y/n / Web 确认），不得退化到 input()
            # ——EOF 时静默拒绝（工具白跑/误判失败），TTY 下阻塞事件循环。
            confirm_fn=self.confirm_fn,
        )
        try:
            result = await sub_agent.run_once(prompt)
            self.total_input_tokens += result["tokens"]["input"]
            self.total_output_tokens += result["tokens"]["output"]
            print_sub_agent_end(agent_type, description)
            return result["text"] or "(Sub-agent produced no output)"
        except Exception as e:
            print_sub_agent_end(agent_type, description)
            return f"Sub-agent error: {e}"

#--------------Anthropic 后端---------------
    async def  _chat_anthropic(self, user_message: str) -> None:
        self._anthropic_messages = self._normalize_anthropic_messages(_sanitize_for_utf8(self._anthropic_messages))
        user_message = _safe_utf8_text(user_message)
        # 先把本轮用户输入放入 Anthropic 消息历史，后续每轮模型调用都会带上这段上下文。
        self._anthropic_messages.append({"role": "user", "content": user_message})

        # 异步内存预取：主 agent 才需要查 memory，sub agent 不额外注入记忆。
        # 这里只启动后台任务，不阻塞当前模型调用流程。
        # EVOHARNESS_DISABLE_MEMORY_PREFETCH=1 可关闭整条预取（试验场：轻量设计对照用）。
        memory_prefetch:MemoryPrefetch | None = None
        if not self.is_sub_agent and not _memory_prefetch_disabled():
            sq = self._build_side_query(label="memory_recall")
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    self._already_surfaced_memories, self._session_memory_bytes,
                )
        while True:
            # 外部请求中止时，结束整个 agent loop。
            if self._aborted:
                break

            # 每轮调用模型前尝试压缩上下文，避免消息历史过长。
            self._run_compression_pipeline()

            # 如果记忆预取任务已经完成，就把取回来的 memory 内容追加到最后一条用户消息里。
            # consumed 用来保证同一批 memory 只注入一次。
            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        injection_text = _safe_utf8_text(injection_text)
                        last = self._anthropic_messages[-1] if self._anthropic_messages else None
                        if last and last.get("role") == "user":
                            content = last.get("content", "")
                            if isinstance(content, str):
                                # 字符串不可变，需要重新赋值回 message。
                                last["content"] = content + "\n\n" + injection_text
                            elif isinstance(content, list):
                                # list 是可变对象，append 会直接修改 last["content"] 指向的列表。
                                content.append({"type": "text", "text": injection_text})
                        else:
                            # 如果最后一条不是 user message，就单独追加一条用户消息承载 memory。
                            self._anthropic_messages.append({"role": "user", "content": injection_text})

                        for m in memories:
                            # 记录本 session 已经注入过的 memory，后续检索时可避免重复 surfaced。
                            self._already_surfaced_memories.add(m.path)
                            self._session_memory_bytes += m.size
                except Exception:  # 裸 except 会连 KeyboardInterrupt/SystemExit 一起吞
                    # memory 注入失败不应该中断主对话流程。
                    pass

            if not self.is_sub_agent:
                start_spinner()


            # 保存“提前执行”的工具任务。key 是 Anthropic 返回的 tool_use block id。
            early_executions: dict[str, asyncio.Task] = {}


            def _on_tool_block(block:dict):
                # 流式响应中一旦完整收到 tool_use block，如果工具是并发安全且权限允许，
                # 就可以提前开始执行，减少等待完整模型响应后的空档时间。
                if block["name"] in CONCURRENCY_SAFE_TOOLS:
                    perm = check_permission(block["name"], block["input"], self.permission_mode, self._plan_file_path)
                    if perm["action"]=="allow":
                        task =asyncio.create_task(self._execute_tool_call(block["name"], block["input"]))
                        early_executions[block["id"]] = task


            # 调用 Anthropic 流式接口；流式过程中完成 tool block 时会触发 _on_tool_block。
            response = await self._call_anthropic_stream(on_tool_block_complete=_on_tool_block)
            if not self.is_sub_agent:
                stop_spinner()

            # 记录本次模型调用的耗时点和 token 消耗，用于成本展示与预算控制。
            self.last_api_call_time = time.time()
            self.total_input_tokens += response.usage.input_tokens
            self.total_output_tokens += response.usage.output_tokens
            self.last_input_token_count = response.usage.input_tokens

            # Anthropic 的响应内容里可能混有 text block 和 tool_use block，这里只挑出工具调用。
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            # 把模型返回的所有 content block 写入消息历史，后续 tool_result 要与这些 tool_use 对应。
            self._anthropic_messages.append({
                "role": "assistant",
                "content": [self._block_to_dict(b) for b in response.content],
            })

            # 没有工具调用，说明模型已经给出最终回复，本轮对话结束。
            if not tool_uses:
                if not self.is_sub_agent:
                    print_cost(self.total_input_tokens, self.total_output_tokens)
                break

            # 有工具调用时，进入下一轮工具执行。这里同时检查 turn/budget 限制。
            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                # 预算强制收段落点（改造二评审 #7）：chat() 收尾 _finalize_staged_chat 执行确定性收段。
                if self._loop_stages_active:
                    self._stage_close_reason = f"budget:{budget['reason']}"
                self._anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": f"Tool execution skipped: {budget['reason']}",
                        }
                        for tu in tool_uses
                    ],
                })
                break


            # 收集本轮所有工具结果，之后作为 tool_result 消息回传给模型。
            tool_results: list[dict] = []
            context_break = False

            cancelled = False
            try:
                for tu in tool_uses:
                    # context_break 表示某个工具执行期间清理了上下文，需要停止继续处理本轮剩余工具。
                    if context_break or self._aborted:
                        break

                    # 将工具入参转为普通 dict，便于权限检查、打印和实际执行。
                    inp = dict(tu.input) if hasattr(tu, "items") else tu.input
                    # 与 OpenAI 路径对称：input 非 dict（数组/字符串/null）时归一为空 dict，
                    # 否则 print_tool_call → _get_tool_summary 调 inp.get 抛 AttributeError；
                    # 该处位于收尾 try/finally 之外（finally 仅在取消时补占位）→ 整轮崩溃 + 悬空。
                    if not isinstance(inp, dict):
                        inp = {}
                    print_tool_call(tu.name, inp)

                    # 如果这个工具已经在流式阶段提前开始执行，这里只需要等待它完成并收集结果。
                    early_task = early_executions.get(tu.id)
                    if early_task:
                        try:
                            raw = await early_task
                        except Exception as e:
                            raw = f"Error executing tool: {e}"
                        raw = utf8_tool_result(raw)
                        res = self._persist_large_result(tu.name, raw)
                        print_tool_result(tu.name, res)
                        self._record_tool_outcome(tu.name, not self._looks_like_tool_failure(tu.name, raw, res))
                        tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})
                        continue

                    # 如果不是提前执行的工具，就在真正执行前做权限检查。

                    perm = check_permission(tu.name, inp, self.permission_mode, self._plan_file_path)
                    if perm["action"] == "deny":
                        # 权限拒绝时，也要返回一个 tool_result，让模型知道该工具调用失败的原因。
                        print_info(f"Denied: {perm.get('message', '')}")
                        self._record_tool_outcome(tu.name, False)
                        tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                             "content": denied_tool_result("deny", perm.get("message", ""))})
                        continue

                    if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in self._confirmed_paths:
                        # 高风险操作需要用户确认；同一个 message 确认过后会缓存，避免重复询问。
                        confirmed = await self._confirm_dangerous(perm["message"])
                        if not confirmed:
                            self._record_tool_outcome(tu.name, False)
                            tool_results.append(
                                {"type": "tool_result", "tool_use_id": tu.id,
                                 "content": denied_tool_result("user-denied")})
                            continue
                        self._confirmed_paths.add(perm["message"])

                    # 权限通过后执行工具，并把大输出持久化为可回传的摘要或引用。
                    try:
                        raw = await self._execute_tool_call(tu.name, inp)
                    except Exception as e:
                        raw = f"Error executing tool: {e}"
                    raw = utf8_tool_result(raw)
                    res = self._persist_large_result(tu.name, raw)
                    print_tool_result(tu.name, res)
                    self._record_tool_outcome(tu.name, not self._looks_like_tool_failure(tu.name, raw, res))

                    if self._context_cleared:
                        # 工具执行过程中如果清理了上下文，就把结果作为新的用户消息写入，
                        # 并停止继续处理本轮剩余工具，避免旧上下文和新上下文混在一起。
                        self._context_cleared = False
                        self._anthropic_messages.append({"role": "user", "content": res})
                        context_break = True
                        break

                    # Anthropic 要求 tool_result 使用 tool_use_id 对应到前面的 tool_use block。
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})
            except asyncio.CancelledError:
                # Ctrl+C 取消会在工具执行中抛 CancelledError（继承 BaseException，不被
                # except Exception 兜住），绕过循环后的收尾直接冒泡出 chat()。
                cancelled = True
                raise
            finally:
                # 取消/中断（Ctrl+C）时循环会提前 break（见上方 self._aborted 判据）或异常退出，
                # 但 assistant(tool_use) 消息已入 history；未执行的 tool_use 若无配对 tool_result
                # 会留下悬空块 → 持久化后 --resume 下条请求 400。与预算超限路径对称：为所有未覆盖
                # 的 tool_use 补占位 tool_result（消息列表是持久对象，配对在落盘前完成）。
                if (self._aborted or cancelled) and not context_break:
                    _covered = {tr.get("tool_use_id") for tr in tool_results}
                    for tu in tool_uses:
                        if tu.id not in _covered:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": "Tool execution cancelled (interrupted).",
                            })
                if not context_break and tool_results:
                    # Anthropic 要求 assistant/tool_use 后面紧跟一条 user/tool_result 消息，
                    # 且这条消息必须包含本轮所有 tool_use 的对应结果。
                    self._anthropic_messages.append({"role": "user", "content": tool_results})

            self._context_cleared = False

            # 工具结果可能很长，每轮工具执行后检查是否需要压缩上下文。
            self._refresh_runtime_system_prompt()
            await self._check_and_compact()

    @staticmethod
    def _block_to_dict(block) -> dict:
        if block.type == "text":
            return {"type": "text", "text": _safe_utf8_text(block.text)}
        if block.type == "tool_use":
            raw_input = dict(block.input) if hasattr(block.input, 'items') else block.input
            return {"type": "tool_use", "id": _safe_utf8_text(block.id), "name": _safe_utf8_text(block.name), "input": _sanitize_for_utf8(raw_input)}
        # Fallback
        return {"type": _safe_utf8_text(block.type)}

    async def _call_anthropic_stream(self, on_tool_block_complete=None):

        async def _do():
            max_output =  _get_max_output_tokens(self.model)

            create_params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_output if self._thinking_mode != "disabled" else 16384,
                "system": _safe_utf8_text(self._system_prompt),
                "tools": _sanitize_for_utf8(get_active_tool_definitions(self.tools)),
                "messages": _sanitize_for_utf8(self._anthropic_messages),
            }
            #如果开启了思考模式，就给 Anthropic 请求加上 thinking 参数。
            if self._thinking_mode  in ("adaptive", "enabled"):
                create_params["thinking"]={"type": "enabled", "budget_tokens": max_output - 1}

            first_text = True

            tool_blocks_by_index: dict[int, dict] = {}

            async with self._anthropic_client.messages.stream(**create_params)as stream:
                async for event in stream:
                    if not hasattr(event, 'type'):
                        continue
                    # 当事件是工具调用开始：
                    if event.type == "content_block_start":
                        cb = getattr(event, 'content_block', None)
                        #如果 block 类型是 tool_use，就记录这个工具调用：
                        if cb and getattr(cb, 'type', None) == "tool_use":
                            #因为工具参数 JSON 是流式分片返回的，所以先准备一个空字符串 input_json。
                            tool_blocks_by_index[event.index]= {
                                "id": cb.id, "name": cb.name, "input_json": "",
                            }
                    #当事件是内容增量，分三种情况。
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        # 第一种，普通文本：模型输出正文时，
                        # 调用 _emit_text()。如果是普通交互，就打印；
                        # 如果是 run_once()，就写入 _output_buffer。
                        if hasattr(delta, "text"):
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n")
                                first_text = False
                            self._emit_text(delta.text)
                        #第二种，thinking 内容：
                        #如果模型返回思考内容，也输出出来，并在开头加：[thinking]
                        elif hasattr(delta, 'thinking'):
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n  [thinking] ")
                                first_text = False
                            self._emit_text(delta.thinking)
                        #第三种，工具参数 JSON 片段：工具调用的参数不是一次性返回，
                        # 而是一段一段返回，所以这里不断拼接到 input_json。
                        elif hasattr(delta, 'partial_json'):
                            tb = tool_blocks_by_index.get(event.index)
                            if tb:
                                tb["input_json"] += _safe_utf8_text(delta.partial_json)
                    #当一个 content block 结束：
                    #如果结束的是之前记录的工具调用，就把拼好的 JSON 解析出来：
                    elif event.type == "content_block_stop":
                        tb = tool_blocks_by_index.pop(event.index, None)
                        if tb and on_tool_block_complete:
                            import json as _json
                            try:
                                parsed = _json.loads(tb["input_json"] or "{}")
                            except Exception:
                                parsed = {}
                            #然后调用回调：
                            #这个回调的作用通常是：工具调用一完整，
                            # 就可以提前开始执行工具，不必等整条 assistant 消息全部结束。
                            on_tool_block_complete({
                                "type": "tool_use", "id": _safe_utf8_text(tb["id"]),
                                "name": _safe_utf8_text(tb["name"]), "input": _sanitize_for_utf8(parsed),
                            })
                final_message = await stream.get_final_message()

            #过滤思考的message（因为 thinking 内容一般不应该进入历史消息，否则后续上下文会变大，也可能不符合 API 消息格式要求。）
            final_message.content = [b for b in final_message.content if b.type != "thinking"]
            return final_message
#调用 _do()，如果遇到可重试错误，就由 _with_retry() 负责重试。
        return await _with_retry(_do)

    #openAI后端

    async def _chat_openai(self, user_message:str) -> None:
        user_message = _safe_utf8_text(user_message)
        self._openai_messages.append({"role": "user", "content": user_message})

        #预取句柄 MemoryPrefetch
        memory_prefetch: MemoryPrefetch | None = None
        if not self.is_sub_agent and not _memory_prefetch_disabled():
            sq = self._build_side_query(label="memory_recall")
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    self._already_surfaced_memories, self._session_memory_bytes,
                )

        while True:
            if self._aborted:
                break

            self._run_compression_pipeline()

            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        injection_text = _safe_utf8_text(injection_text)
                        last = self._openai_messages[-1] if self._openai_messages else None

                        if last and last.get("role") == "user":
                            last["content"] = (last.get("content") or "") + "\n\n" + injection_text
                        else:
                            self._openai_messages.append({"role": "user", "content": injection_text})

                        for m in memories:
                            self._already_surfaced_memories.add(m.path)
                            self._session_memory_bytes += len(m.content.encode())
                except Exception:
                    pass

            if not self.is_sub_agent:
                start_spinner()

            response = await self._call_openai_stream()

            if not self.is_sub_agent:
                stop_spinner()

            self.last_api_call_time = time.time()

            if response.get("usage"):
                self.total_input_tokens += response["usage"]["prompt_tokens"]
                self.total_output_tokens += response["usage"]["completion_tokens"]
                self.last_input_token_count = response["usage"]["prompt_tokens"]

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})

            self._openai_messages.append(message)
            assistant_idx = len(self._openai_messages) - 1

            tool_calls = message.get("tool_calls")

            if not tool_calls:
                if not self.is_sub_agent:
                    print_cost(self.total_input_tokens, self.total_output_tokens)
                break

            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                # 预算强制收段落点（改造二评审 #7，与 Anthropic 路径对称）：
                # chat() 收尾 _finalize_staged_chat 执行确定性收段。
                if self._loop_stages_active:
                    self._stage_close_reason = f"budget:{budget['reason']}"
                # 与 Anthropic 路径对称：为每个未执行的 tool_call 补占位 tool 消息，
                # 保持 assistant(tool_calls) 与 role=tool 的配对，避免悬空 tool_calls。
                # 与主循环一致：缺 id 者就地补稳定占位 id（否则原样存回、悬空持久化）。
                for _idx, tc in enumerate(tool_calls):
                    if not tc.get("id"):
                        tc["id"] = f"call_autogen_{self.current_turns}_{_idx}"
                    self._openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"Tool execution skipped: {budget['reason']}",
                    })
                break

            oai_checked: list[dict] = []
            for _idx, tc in enumerate(tool_calls):
                if self._aborted:
                    break

                # 缺 id 的 tool_call（type/function.name 均合法）会穿过畸形防御被正常
                # 执行，随后 ct["tc"]["id"] 下标取 id 触发 KeyError 崩溃整轮。就地补
                # 稳定占位 id（tc 与已存档的 assistant 消息为同一对象，无需另写回），
                # 保证不崩溃、且 assistant(tool_calls) 与 role=tool 始终成对。
                if not tc.get("id"):
                    tc["id"] = f"call_autogen_{self.current_turns}_{_idx}"

                # 畸形 tool_call 防御：非 function / 缺 function / 缺 name 时不得
                # continue 后留下悬空 tool_call_id（也避免 KeyError 使整轮崩溃），
                # 统一补一条 role=tool 占位消息，保持 assistant(tool_calls) 配对。
                _fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
                if tc.get("type") != "function" or not _fn or not _fn.get("name"):
                    if tc.get("id"):
                        self._openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": "Tool call skipped: malformed tool_call "
                                       "(missing function/name).",
                        })
                    continue

                fn_name = _fn["name"]
                try:
                    inp = json.loads(_fn.get("arguments") or "{}")
                except Exception:
                    inp = {}
                # arguments 解析结果非 dict（数组/字符串/null）时归一为空 dict：否则
                # print_tool_call → _get_tool_summary 调 inp.get 抛 AttributeError。该处位于
                # 本轮收尾 try/finally 之外、无兜底 → 整轮崩溃，且已入 history 的
                # assistant(tool_calls) 留下悬空（0 条 role=tool，--resume 下条请求 400）。
                if not isinstance(inp, dict):
                    inp = {}

                # 展示/权限检查同样位于收尾 try/finally 之外，异常冒泡会使整轮崩溃并悬空。
                # 兜底：异常时为该 tool_call 补占位 tool 消息后跳过，保持配对、不悬空。
                try:
                    print_tool_call(fn_name, inp)
                    perm = check_permission(fn_name, inp, self.permission_mode, self._plan_file_path)
                except Exception:
                    self._openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "Tool call skipped: tool call display / permission "
                                   "check failed.",
                    })
                    continue

                if perm["action"] == "deny":
                    print_info(f"Denied: {perm.get('message', '')}")
                    self._record_tool_outcome(fn_name, False)
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                                        "result": f"Action denied: {perm.get('message', '')}"})
                    continue
                if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in self._confirmed_paths:
                    confirmed = await self._confirm_dangerous(perm["message"])
                    if not confirmed:
                        self._record_tool_outcome(fn_name, False)
                        oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                                            "result": "User denied this action."})
                        continue
                    self._confirmed_paths.add(perm["message"])
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

            # 权限检查循环结束后（本轮全部 tool_call 已收集），再统一构建并发批次并执行一次。
            # 原实现把该块缩进在 for tc 内部，导致同一轮并发安全工具被重复执行、并写入重复
            # tool_call_id 的 role=tool 消息（与 Anthropic 路径对称：仅在 for 之外执行一次）。
            oai_batches: list[dict] = []
            for ct in oai_checked:
                safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
                if safe and oai_batches and oai_batches[-1]["concurrent"]:
                    oai_batches[-1]["items"].append(ct)
                else:
                    oai_batches.append({"concurrent": safe, "items": [ct]})

            oai_context_break = False
            cancelled = False
            try:
                for batch in oai_batches:
                    if oai_context_break or self._aborted:
                        break

                    if batch["concurrent"]:
                        async def _run_oai_safe(ct_item: dict) -> tuple[dict, str, str]:
                            # 与 Anthropic 路径对称：工具抛异常不得中断整轮，否则已入 history 的
                            # assistant(tool_calls) 缺配对的 role=tool → 悬空持久化（下次 400），
                            # 且异常冒泡出 chat() 使 _turn_output_buffer 残留（main.py 误判仍在处理）。
                            try:
                                raw = await self._execute_tool_call(ct_item["fn"], ct_item["inp"])
                            except Exception as e:
                                raw = f"Error executing tool: {e}"
                            raw = utf8_tool_result(raw)
                            res = self._persist_large_result(ct_item["fn"], raw)
                            print_tool_result(ct_item["fn"], res)
                            return ct_item, raw, res

                        results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]])
                        for ct_item, raw, res in results:
                            # 与下方非并发批对称：传原始 raw，避免仅凭截断预览误判失败。
                            self._record_tool_outcome(
                                ct_item["fn"],
                                not self._looks_like_tool_failure(ct_item["fn"], raw, res),
                            )
                            self._openai_messages.append(
                                {"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res})
                    else:
                        for ct in batch["items"]:
                            if not ct["allowed"]:
                                self._openai_messages.append(
                                    {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": ct["result"]})
                                continue

                            # 与 Anthropic 路径对称：工具抛异常时写占位 tool 消息（保持
                            # assistant(tool_calls)/role=tool 配对，避免悬空持久化下次 400），
                            # 不让异常冒泡出 chat()（否则 _turn_output_buffer 残留，
                            # main.py 的 _agent_is_processing 误判仍在处理）。
                            try:
                                raw = await self._execute_tool_call(ct["fn"], ct["inp"])
                            except Exception as e:
                                raw = f"Error executing tool: {e}"
                            raw = utf8_tool_result(raw)
                            res = self._persist_large_result(ct["fn"], raw)
                            print_tool_result(ct["fn"], res)
                            self._record_tool_outcome(
                                ct["fn"],
                                not self._looks_like_tool_failure(ct["fn"], raw, res),
                            )

                            if self._context_cleared:
                                self._context_cleared = False
                                self._openai_messages.append({"role": "user", "content": res})
                                oai_context_break = True
                                break

                            self._openai_messages.append(
                                {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res})
            except asyncio.CancelledError:
                # Ctrl+C 取消会在工具执行中抛 CancelledError（继承 BaseException，不被
                # except Exception 兜住），绕过收尾直接冒泡出 chat()。
                cancelled = True
                raise
            finally:
                # 取消/中断（Ctrl+C）时批量执行循环提前 break（见上方 self._aborted 判据）或异常
                # 退出，但 assistant(tool_calls) 消息已入 history；未执行的 tool_call 若缺 role=tool
                # 会悬空持久化（--resume 下条请求 400）。与预算超限路径对称：为所有未配对
                # tool_call 补占位 tool 消息（列表是持久对象，配对在落盘前完成）。
                if (self._aborted or cancelled) and not oai_context_break:
                    _covered_ids = {
                        m.get("tool_call_id")
                        for m in self._openai_messages[assistant_idx + 1:]
                        if m.get("role") == "tool"
                    }
                    for _idx, tc in enumerate(tool_calls):
                        if not tc.get("id"):
                            tc["id"] = f"call_autogen_{self.current_turns}_{_idx}"
                        if tc["id"] not in _covered_ids:
                            self._openai_messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": "Tool execution cancelled (interrupted).",
                            })

            self._context_cleared = False
            self._refresh_runtime_system_prompt()
            await self._check_and_compact()

    async def _call_openai_stream(self) -> dict:
        # 部分第三方 OpenAI-compatible 网关的流式实现有缺陷（content 双发、
        # reasoning 泄漏进 content、usage chunk 序列化报错）。设置 NO_STREAM=1
        # 可回退为非流式请求：一次拿到完整回复，输出体验从打字机变为整段显示。
        if os.environ.get("NO_STREAM") == "1":
            return await self._call_openai_nostream()

        async def _do():
            stream = await self._openai_client.chat.completions.create(
                model=self.model,
                tools=_sanitize_for_utf8(_to_openai_tools(get_active_tool_definitions(self.tools))),
                messages=_sanitize_for_utf8(self._openai_messages),
                stream=True,
                stream_options={"include_usage": True},
            )

            content = ""
            first_text = True
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None

            async for chunk in stream:
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta and delta.content:
                    text = _safe_utf8_text(delta.content)
                    if first_text:
                        stop_spinner()
                        self._emit_text("\n")
                        first_text = False
                    self._emit_text(text)
                    content += text

                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        existing = tool_calls.get(tc.index)
                        if existing:
                            if tc.function and tc.function.arguments:
                                existing["arguments"] += _safe_utf8_text(tc.function.arguments)
                        else:
                            tool_calls[tc.index] = {
                                "id": _safe_utf8_text(tc.id or ""),
                                "name": _safe_utf8_text((tc.function.name if tc.function else "") or ""),
                                "arguments": _safe_utf8_text((tc.function.arguments if tc.function else "") or ""),
                            }

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items())
                ]

            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": assembled,
                    },
                    "finish_reason": finish_reason or "stop",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await _with_retry(_do)

    async def _call_openai_nostream(self) -> dict:
        """非流式请求。返回与流式路径相同的 dict 结构。"""
        async def _do():
            response = await self._openai_client.chat.completions.create(
                model=self.model,
                tools=_sanitize_for_utf8(_to_openai_tools(get_active_tool_definitions(self.tools))),
                messages=_sanitize_for_utf8(self._openai_messages),
            )
            usage = getattr(response, "usage", None)
            message = response.choices[0].message if response.choices else None
            if message is None:
                return {"choices": [{"message": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
            content = _safe_utf8_text(message.content or "")
            if content:
                stop_spinner()
                self._emit_text("\n")
                self._emit_text(content)
            tool_calls = None
            if message.tool_calls:
                tool_calls = [
                    {
                        "id": _safe_utf8_text(tc.id or ""),
                        "type": "function",
                        "function": {
                            "name": _safe_utf8_text(tc.function.name or ""),
                            "arguments": _safe_utf8_text(tc.function.arguments or ""),
                        },
                    }
                    for tc in message.tool_calls
                ]
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": response.choices[0].finish_reason or "stop",
                }],
                "usage": {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                },
            }
        return await _with_retry(_do)

    async def _confirm_dangerous(self, command: str) -> bool:
        print_confirmation(command)
        if self.confirm_fn:
            return await self.confirm_fn(command)
        # Fallback: blocking input
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False
