#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from tqdm.utils import IS_WIN

from agents.memory import get_memory_dir, read_text_migrating

ToolDef = dict  # Anthropic tool schema dict
#权限模式
PermissionMode = str  # "default" | "plan" | "acceptEdits" | "bypassPermissions" | "dontAsk"


#---------结构化工具结果（W03）------------------
# 成功/失败必须由机器状态决定，不能扫描正文里有没有 "error"/"timeout"：
#   读一份记录错误码的文档 = 成功；`Command failed (exit code 3)` = 失败。
# 协议边缘（Anthropic tool_result / OpenAI role=tool）仍消费字符串，由 render() 提供。
TOOL_OK = "ok"
TOOL_FAILED = "failed"
TOOL_DENIED = "denied"
TOOL_CONFLICT = "conflict"
TOOL_TIMEOUT = "timeout"
TOOL_UNKNOWN = "error"

FAILURE_STATUSES = frozenset({TOOL_FAILED, TOOL_DENIED, TOOL_CONFLICT, TOOL_TIMEOUT, TOOL_UNKNOWN})


class ToolResult(str):
    """工具执行结果：本身是 str（协议边缘零改动），同时携带机器可判定状态。

    继承 str 是为了让既有调用方（打印、`startswith("Error")`、日志、测试、持久化）零改动，
    但**判定成功/失败只能读 .status / .ok**，不能再扫描正文里的 "error"/"timeout"：
    读一份记录错误码的文档是成功，`Command failed (exit code 3)` 才是失败。
    artifact_paths 只在真正成功写入后登记；失败路径绝不登记，杜绝"假产物"。
    """

    __slots__ = ("status", "error_code", "exit_code", "duration_ms", "artifact_paths", "metadata")

    def __new__(
        cls,
        content: str = "",
        *,
        status: str = TOOL_OK,
        error_code: str = "",
        exit_code: int | None = None,
        duration_ms: int = 0,
        artifact_paths: tuple[str, ...] = (),
        metadata: dict | None = None,
    ) -> "ToolResult":
        obj = super().__new__(cls, "" if content is None else str(content))
        obj.status = status
        obj.error_code = error_code
        obj.exit_code = exit_code
        obj.duration_ms = duration_ms
        obj.artifact_paths = tuple(artifact_paths)
        obj.metadata = dict(metadata or {})
        return obj

    @property
    def ok(self) -> bool:
        return self.status not in FAILURE_STATUSES

    def render(self) -> str:
        return str(self)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "errorCode": self.error_code,
            "exitCode": self.exit_code,
            "durationMs": self.duration_ms,
            "artifactPaths": list(self.artifact_paths),
            "metadata": dict(self.metadata),
        }


def coerce_tool_result(value) -> ToolResult:
    """把历史裸字符串归一为 ToolResult；遗留返回值按成功处理（兼容语义）。"""
    if isinstance(value, ToolResult):
        return value
    return ToolResult("" if value is None else str(value))


def tool_result_ok(value) -> bool:
    return coerce_tool_result(value).ok


def tool_result_status(value) -> str:
    return coerce_tool_result(value).status


def tool_result_artifacts(value) -> tuple[str, ...]:
    return coerce_tool_result(value).artifact_paths


READ_TOOLS = {"read_file", "list_files", "grep_search", "compact_context"}
EDIT_TOOLS = {"write_file", "edit_file", "skill_evolve", "skill_create"}
#会话/流程控制类工具：非编辑、非 shell、非 MCP，允许在 check_permission 尾部自动放行。
#未登记的工具名一律降级 confirm/deny（fail-closed），避免未来新增工具因未被任何分支
#处理而默认放行。
SESSION_CONTROL_TOOLS = {"skill", "advance_stage", "agent", "tool_search"}


#并发安全的工具可以并行运行（只读，无副作用）
CONCURRENCY_SAFE_TOOLS = {"read_file", "list_files", "grep_search"}







#工具定义
tool_definitions: list[ToolDef] = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Returns the file content with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to read"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to write"},
                "content": {"type": "string", "description": "The content to write to the file"},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Edit a file by replacing an exact string match with new content. The old_string must match exactly (including whitespace and indentation).",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to edit"},
                "old_string": {"type": "string", "description": "The exact string to find and replace"},
                "new_string": {"type": "string", "description": "The string to replace it with"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_files",
        "description": "List files matching a glob pattern. Returns matching file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": 'Glob pattern to match files (e.g., "**/*.ts", "src/**/*")'},
                "path": {"type": "string", "description": "Base directory to search from. Defaults to current directory."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_search",
        "description": "Search for a pattern in files. Returns matching lines with file paths and line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The regex pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search in. Defaults to current directory."},
                "include": {"type": "string", "description": 'File glob pattern to include (e.g., "*.ts", "*.py")'},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_shell",
        "description": "Execute a shell command and return its output. Use this for running tests, installing packages, git operations, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "timeout": {"type": "number", "description": "Timeout in milliseconds (default: 30000)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "skill",
        "description": "Invoke a registered skill by name. Skills are prompt templates loaded from .evoharness/skills/. Returns the skill's resolved prompt to follow.",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The name of the skill to invoke"},
                "args": {"type": "string", "description": "Optional arguments to pass to the skill"},
            },
            "required": ["skill_name"],
        },
    },
    {
        "name": "compact_context",
        "description": "Compact the current conversation context into structured session memory when the context is long, tool results are noisy, or a strategy reset is useful. This preserves task progress, current next steps, and tool-use experience, then continues from the folded memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief reason for compacting now, such as long context, many tool calls, repeated failures, or strategy change.",
                },
            },
        },
    },
    {
        # Loop 阶段边界工具（改造二）：走 _context_cleared 通道（同 compact_context），
        # 刻意不入 READ_TOOLS / CONCURRENCY_SAFE_TOOLS——不提前执行、不并发分批。
        "name": "advance_stage",
        "description": "Close the current loop stage (execute -> verify -> deliver) and start the next one with a summary handover: your conclusions, key artifact paths, user constraints, and the next action are carried over while the raw stage conversation is replaced. Only available when loop stages are active (staged --plan sessions).",
        "input_schema": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": ["verify", "deliver"],
                    "description": "The next stage to enter. Stage 'plan' is left via exit_plan_mode, not this tool.",
                },
                "summary": {
                    "type": "string",
                    "description": "One-paragraph conclusion of the finished stage (what is done, what was refuted).",
                },
                "artifacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path of a key artifact"},
                            "why": {"type": "string", "description": "One line: why it matters next"},
                        },
                        "required": ["path"],
                    },
                    "description": "Key artifacts of the finished stage (files written, reports, test outputs).",
                },
            },
            "required": ["stage", "summary"],
        },
    },
    {
        "name": "skill_evolve",
        "description": "Persist an explicit reusable user correction or workflow preference into an existing skill. Creates a version snapshot before editing the skill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The registered skill name to evolve"},
                "lesson": {"type": "string", "description": "Durable reusable rule to add to the skill"},
                "rationale": {"type": "string", "description": "Why this lesson should affect future similar tasks"},
                "target": {
                    "type": "string",
                    "enum": ["active", "project", "user"],
                    "description": "Which skill file to update. Defaults to active.",
                },
            },
            "required": ["skill_name", "lesson"],
        },
    },
    {
        "name": "skill_create",
        "description": "Create a new reusable skill from explicit durable workflow guidance when no suitable existing skill exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Concise reusable skill name"},
                "description": {"type": "string", "description": "One-sentence description of what the skill does and when to use it"},
                "instructions": {"type": "string", "description": "Reusable SKILL.md body. Focus on durable method, constraints, and workflow, not one-off task content."},
                "when_to_use": {"type": "string", "description": "Trigger condition for auto-invocation"},
                "target": {
                    "type": "string",
                    "enum": ["project", "user"],
                    "description": "Where to create the skill. Defaults to project.",
                },
                "context": {
                    "type": "string",
                    "enum": ["inline", "fork"],
                    "description": "Skill execution mode. Defaults to inline.",
                },
                "user_invocable": {"type": "boolean", "description": "Whether users can invoke it manually with /<skill>. Defaults to false."},
                "allowed_tools": {"type": "string", "description": "Optional comma-separated allowed tools for fork mode"},
                "evidence": {"type": "string", "description": "Short user-provided evidence showing why this is reusable"},
            },
            "required": ["name", "description", "instructions"],
        },
    },
    {
        "name": "enter_plan_mode",
        "description": "Enter plan mode to switch to a read-only planning phase. In plan mode, you can only read files and write to the plan file.",
        "input_schema": {"type": "object", "properties": {}},
        "deferred": True,
    },
    {
        "name": "exit_plan_mode",
        "description": "Exit plan mode after you have finished writing your plan to the plan file.",
        "input_schema": {"type": "object", "properties": {}},
        "deferred": True,
    },
    {
        "name": "agent",
        "description": "Launch a sub-agent to handle a task autonomously. Sub-agents have isolated context and return their result. Types: 'explore' (read-only), 'plan' (read-only, structured planning), 'general' (full tools).",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Short (3-5 word) description of the sub-agent's task"},
                "prompt": {"type": "string", "description": "Detailed task instructions for the sub-agent"},
                "type": {"type": "string", "enum": ["explore", "plan", "general"], "description": "Agent type. Default: general"},
            },
            "required": ["description", "prompt"],
        },
    },
    # ─── Tool search (deferred tool loader) ─────────────────────
    {
        "name": "tool_search",
        "description": "Search for available tools by name or keyword. Returns full schema definitions for matching deferred tools so you can use them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Tool name or search keywords"},
            },
            "required": ["query"],
        },
    },
]

#已登记的内置工具名集合：check_permission 兜底前需对它们显式放行，避免已注册的内置工具
#（如 write_file/edit_file 写工作区内既有文件）落入下方 unrecognized 兜底。
BUILTIN_TOOLS = {t["name"] for t in tool_definitions}


#----------------------工具调用----------------------------

def _resolve_tool_path(raw_path: str, *, must_exist: bool = True) -> Path:
    path = Path(raw_path)
    if path.exists() or not path.is_absolute():
        return path

    # 绝对路径不得被逐级剥前缀重挂到 cwd：否则外部（尚不存在）的路径会被静默改写成
    # 工作区内同名文件，越界判定失效、写入落到错误文件却回执外部路径。保持原样让
    # 越界判定（_is_within_workspace）生效，外部写/读一律走 confirm/deny。
    return path


def _workspace_root() -> Path:
    try:
        return Path.cwd().resolve()
    except Exception:
        return Path.cwd()


def _is_within_workspace(raw_path: str) -> bool:
    """判断路径是否落在工作区（仓库根/cwd）内。空路径视为工作区内。"""
    if not raw_path:
        return True
    try:
        p = _resolve_tool_path(raw_path, must_exist=False).resolve()
    except Exception:
        return False
    root = _workspace_root()
    return p == root or root in p.parents


#模型可写、且会被后续会话当作可信配置加载的文件（持久化代码执行风险）
PROTECTED_CONFIG_NAME = ".mcp.json"
PROTECTED_CONFIG_DIR = ".evoharness"
PROTECTED_CONFIG_FILE = "settings.json"


def _normalize_protected_path(raw_path: str) -> str:
    """把路径规范化为可做“是否同一真实文件”比较的形式（OS 级解析）。

    不再靠字符串逐字比对（8 轮审计每轮都能找到新写法绕过），改用操作系统解析：
      - NTFS 备用数据流后缀（`.mcp.json::$DATA` / `path:stream`）：先剥离 `::`/`:name`
        （保留盘符的 `C:`），否则 realpath 会把 `::` 留在结果里。
      - `os.path.realpath`：展开符号链接、8.3 短名（`MCP~1.JSO` → `.mcp.json`）、
        尾随点/空白、`.`/`..`。
      - 逐分量 `rstrip(" .")`：Windows 视分量尾随点/空白为无意义，兜底 realpath 对
        不存在文件不归一尾随点的情况。
      - `os.path.normcase`：统一大小写与分隔符。
    """
    if not raw_path:
        return ""
    p = str(raw_path)
    drive, tail = os.path.splitdrive(p)
    colon = tail.find(":")
    if colon != -1:
        tail = tail[:colon]
    p = drive + tail
    try:
        p = os.path.realpath(p)
    except Exception:
        p = os.path.normpath(p)
    sep = os.sep
    p = p.replace("\\", sep).replace("/", sep)
    parts = [seg.rstrip(" .") for seg in p.split(sep)]
    p = sep.join(parts)
    return os.path.normcase(os.path.normpath(p))


def _protected_config_paths() -> tuple[Path, ...]:
    """受保护配置的三个合法落点：**工作区根**（cwd）与**用户主目录**。

    只有 `cwd/.mcp.json`、`cwd/.evoharness/settings.json`、`~/.evoharness/settings.json`
    被视为受保护；任意子目录/其它位置的同名文件不再误判（如 `docs/sub/.mcp.json`、
    `/other/.evoharness/settings.json`）。
    """
    return (
        Path.cwd() / PROTECTED_CONFIG_NAME,
        Path.cwd() / PROTECTED_CONFIG_DIR / PROTECTED_CONFIG_FILE,
        Path.home() / PROTECTED_CONFIG_DIR / PROTECTED_CONFIG_FILE,
    )


def _same_file_as_protected_config(raw_path: str) -> bool:
    """目标存在于文件系统时，用文件身份（os.stat 的 st_dev/st_ino）判定是否与受保护配置
    为同一实体。

    覆盖硬链接别名：硬链接没有“目标指针”，os.path.realpath 不会解析到另一端名字，因此
    仅靠基名/目录比对会漏判；同一 inode 即视为受保护（symlink 由 os.stat 默认跟随目标，
    同样命中）。目标不存在时无法取身份，返回 False（尾随点/空白/8.3 短名/数据流/符号链接
    等别名已由 _normalize_protected_path 的 realpath 归一覆盖）。
    """
    try:
        target = os.stat(raw_path)
    except (OSError, ValueError, TypeError):
        return False
    for cand in _protected_config_paths():
        try:
            st = os.stat(cand)
        except OSError:
            continue
        if st.st_dev == target.st_dev and st.st_ino == target.st_ino:
            return True
    return False


def _is_protected_config_edit(raw_path: str) -> bool:
    """判断写入目标是否是不可自动放行的敏感配置。

    `.mcp.json` 与 `.evoharness/settings.json` 会被后续会话读取：前者会自动 spawn
    任意 command（持久化代码执行），后者的 permissions.allow 会放行任意工具。
    这两类文件的写入必须绕过 settings 的 allow 规则，至少要求用户显式确认。

    受保护范围精确限定到**工作区根**与**用户主目录**的三个落点（见
    _protected_config_paths）——不再按基名/目录对任意子目录文件误判。
    判定分两层：先把目标与三个落点都走 OS 级 realpath 归一（见
    _normalize_protected_path）后逐字比对，覆盖尾随点/空白/NTFS 数据流/8.3 短名/符号
    链接；再对存在的目标做文件身份（st_dev/st_ino）比较，覆盖 realpath 无法解析的硬链接
    别名（见 _same_file_as_protected_config）。
    """
    norm = _normalize_protected_path(raw_path)
    if not norm:
        return False
    for cand in _protected_config_paths():
        if norm == _normalize_protected_path(str(cand)):
            return True
    return _same_file_as_protected_config(raw_path)


# run_shell 的“写入目标”枚举（重定向目标 / 复制·移动 / -OutFile / curl -o / certutil /
# tar -C / 内联解释器落盘 API …）已整体删除——这是有意的安全取舍：
#   从 shell 命令文本里可靠推断“它究竟写哪个文件”在对抗面下是无界的（变量展开、运行时
#   拼接路径、命令别名、8.3 短名、编码、语法糖…），8 轮审计每轮都会出现新的绕过写法。
# 因此 run_shell 不再枚举写入目标，改由「封闭元字符集 force-confirm + 默认确认 + allow
# 白名单」兜底：含元字符（含 `>` / `>>` 重定向）的命令一律要求用户确认；只有不含任何
# 元字符的简单字面命令才可能被 allow 规则放行。write_file / edit_file 侧仍精确守敏感配置
# （见 _is_protected_config_edit：realpath 归一 + st_dev/st_ino 文件身份比较同一真实文件）。
# 有意取舍（留档）：因 run_shell 已不枚举写目标，用户显式配置的宽泛 allow 规则（如
# allow(run_shell(Set-Content *))）命中时仍可能写入受保护配置——这里靠“宽泛 allow 必须由
# 用户显式配置”兜底，不再对 shell 命令文本做不可靠的写目标推断。


#读取文件并且在读取文件的基础上添加行号
def _read_file(inp:dict) -> "ToolResult":
    try:
        path = _resolve_tool_path(inp["file_path"])
        content = read_text_migrating(path)
        lines = content.split("\n")
        numbered = "\n".join(f"{i + 1:4d} | {line}" for i, line in enumerate(lines))
        # 正文里出现 "error"/"timeout" 只是文件内容，不构成失败。
        return ToolResult(numbered, status=TOOL_OK, metadata={"path": str(path), "lines": len(lines)})
    except FileNotFoundError as e:
        return ToolResult(f"Error reading file: {e}", status=TOOL_FAILED, error_code="file-missing")
    except IsADirectoryError as e:
        return ToolResult(f"Error reading file: {e}", status=TOOL_FAILED, error_code="is-directory")
    except PermissionError as e:
        return ToolResult(f"Error reading file: {e}", status=TOOL_FAILED, error_code="permission-denied")
    except Exception as e:  # noqa: BLE001
        return ToolResult(f"Error reading file: {e}", status=TOOL_FAILED, error_code=type(e).__name__)

def _write_file(inp:dict) -> "ToolResult":
    try:
        path = _resolve_tool_path(inp["file_path"], must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp["content"], encoding="utf-8")
        # 索引重建失败不应污染"文件是否写好"这个结论：文件已落盘就是成功，
        # 索引是派生数据。单独 try 住，避免外层 except 把它报成写入失败。
        try:
            index_result = _auto_update_memory_index(str(path))
        except Exception as error:  # noqa: BLE001
            index_result = ToolResult(
                f"memory index rebuild failed: {type(error).__name__}: {error}",
                status=TOOL_FAILED, error_code="memory-index-write-failed",
            )
        lines = inp["content"].split("\n")
        line_count = len(lines)
        preview = "\n".join(f"{i + 1:4d} | {l}" for i, l in enumerate(lines[:30]))
        trunc = f"\n  ... ({line_count} lines total)" if line_count > 30 else ""
        # 文件本身写成功了，索引失败属于降级：结果仍为 ok，但把原因带在正文里，
        # 避免用户以为"记忆已入库、索引也更新了"。
        index_note = ""
        if not index_result.ok:
            index_note = f"\n[warning] memory index not updated: {index_result.error_code}"
        return ToolResult(
            f"Successfully wrote to {inp['file_path']} ({line_count} lines){index_note}\n\n{preview}{trunc}",
            status=TOOL_OK,
            artifact_paths=(str(path),),
            metadata={
                "bytes": len(str(inp.get("content") or "").encode("utf-8")),
                "memoryIndexOk": bool(index_result.ok),
                "memoryIndexError": index_result.error_code,
            },
        )
    except PermissionError as e:
        return ToolResult(f"Error writing file: {e}", status=TOOL_FAILED, error_code="permission-denied")
    except Exception as e:  # noqa: BLE001
        return ToolResult(f"Error writing file: {e}", status=TOOL_FAILED, error_code=type(e).__name__)

def _memory_fallback_description(raw: str, name: str) -> str:
    # memory 缺 description 时的兜底摘要：取 frontmatter 之后首行正文，再退回 name。
    lines = raw.split("\n")
    i = 0
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1
    for line in lines[i:]:
        text = line.strip().lstrip("#").strip()
        if text:
            return text
    return name


def _strip_matching_quotes(value: str) -> str:
    # 去掉 value 两侧成对的引号（同 memory.parse_frontmatter 的行为），未配对时保留原样。
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _memory_health_hint(mem_path: Path) -> dict:
    """写记忆后的 L1/L2 健康提示；失败不影响索引（返回空 dict）。

    开关 EVOHARNESS_MEMORY_LIFECYCLE=0 关闭（对照实验用）。
    """
    try:
        if os.environ.get("EVOHARNESS_MEMORY_LIFECYCLE", "1").strip().lower() in {
                "0", "false", "no", "off"}:
            return {}
        from agents.memory_lifecycle import scan_memory_health

        health = scan_memory_health(mem_path)
        summary = health.get("summary") or {}
        if not any(summary.get(k) for k in ("conflicts", "duplicates", "stale", "unparsed")):
            return {}
        return {
            "memoryHealth": health,
            "memoryHealthHint": (
                f"记忆健康：{summary.get('conflicts', 0)} 处覆盖、"
                f"{summary.get('duplicates', 0)} 对疑似重复、"
                f"{summary.get('stale', 0)} 条陈旧、{summary.get('unparsed', 0)} 条解析失败"
                "（只提示不改文件；重复/冲突的语义判断留给模型或人）"),
        }
    except Exception as error:  # noqa: BLE001
        # 健康扫描是提示层，不能让它拖垮索引重建——但要记录原因，不静默。
        return {"memoryHealthError": f"{type(error).__name__}: {error}"[:160]}


def _auto_update_memory_index(file_path:str) -> "ToolResult":
    """重建 MEMORY.md 索引；失败必须让调用方知道，而不是静默留一份旧索引。

    旧实现两层 `except: pass`：内层让某个 memory 解析失败就整条从索引消失，
    外层让索引写盘失败完全无感——用户看到的是"记忆存了但索引里没有"。
    这里内层降级为占位条目（保留可追溯），外层把失败状态回传给调用方。
    """
    skipped: list[dict[str, str]] = []
    try:
        mem_dir = str(get_memory_dir())
        if file_path.startswith(mem_dir) and file_path.endswith(".md") and not file_path.endswith("MEMORY.md"):
            mem_path = Path(mem_dir)
            lines = ["# Memory Index", ""]
            for f in sorted(mem_path.glob("*.md")):
                if f.name == "MEMORY.md":
                    continue
                try:
                    raw = read_text_migrating(f)
                    name_match = re.search(r"^name:\s*(.+)$", raw, re.MULTILINE)
                    type_match = re.search(r"^type:\s*(.+)$", raw, re.MULTILINE)
                    desc_match = re.search(r"^description:\s*(.+)$", raw, re.MULTILINE)
                    if name_match and type_match:
                        # 与 memory.parse_frontmatter 保持一致：去掉 value 两侧的配对引号，
                        # 否则同一 memory 会在此记录出 "Name"、在 memory 侧记录出 Name。
                        n = _strip_matching_quotes(name_match.group(1).strip())
                        t = _strip_matching_quotes(type_match.group(1).strip())
                        d = _strip_matching_quotes(desc_match.group(1).strip()) if desc_match else ""
                        if not d:
                            d = _memory_fallback_description(raw, n)
                        lines.append(f"- **[{n}]({f.name})** ({t}) — {d}")
                except Exception as error:  # noqa: BLE001
                    # 单条解析失败不能让它在索引里凭空消失：保留条目并标注原因。
                    skipped.append({"file": f.name, "reason": f"{type(error).__name__}: {error}"[:160]})
                    lines.append(f"- **[{f.name}]({f.name})** (unknown) — 索引解析失败：{type(error).__name__}")
            (mem_path / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")
            # L1/L2 生命周健康扫描（memory_lifecycle 移植）：写入后发现冲突/重复要
            # 让调用方看见，但不能静默改文件——语义判断留给人或模型。
            health = _memory_health_hint(mem_path)
            if health:
                metadata_extra = health
            else:
                metadata_extra = {}
            if skipped:
                return ToolResult(
                    f"memory index rebuilt with {len(skipped)} skipped entries",
                    status=TOOL_OK,  # 索引已写出；跳过项是降级，不是失败
                    metadata={"skipped": skipped, "entries": len(lines) - 2, **metadata_extra},
                )
            return ToolResult("memory index rebuilt", status=TOOL_OK,
                              metadata={"skipped": [], "entries": len(lines) - 2, **metadata_extra})
        return ToolResult("not a memory file; index untouched", status=TOOL_OK)
    except Exception as error:  # noqa: BLE001
        return ToolResult(
            f"memory index rebuild failed: {type(error).__name__}: {error}",
            status=TOOL_FAILED,
            error_code="memory-index-write-failed",
            metadata={"skipped": skipped},
        )

#-------------------------编辑助手，符号规范化+差异化------------------------

#将各种Unicode引号字符统一转换为标准的ASCII直引号。
def _normalize_quotes(s: str) -> str:
    s = re.sub("[\u2018\u2019\u2032]", "'", s)
    s = re.sub('[\u201c\u201d\u2033]', '"', s)
    return s

#查询匹配到的字符串
def _find_all_actual_strings(file_content: str, search_string: str) -> list[str]:
    """返回文件中所有与 search_string 匹配的实际片段（含引号归一变体）。

    旧实现 `_find_actual_string` 只返回第一处，唯一性校验又只数原文出现次数：
    文件里同一句话有两处弯引号变体时，count(old_string)==0/1，
    结果直接 replace 掉第一处——静默改错位置。
    这里把所有命中位置都找出来，交由调用方判定唯一性。
    """
    if search_string in file_content:
        # 原文精确匹配：按出现次数展开
        positions: list[int] = []
        start = 0
        while True:
            idx = file_content.find(search_string, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + len(search_string)
        return [file_content[i:i + len(search_string)] for i in positions]

    norm_search = _normalize_quotes(search_string)
    norm_file = _normalize_quotes(file_content)
    if not norm_search:
        return []
    matches: list[str] = []
    start = 0
    while True:
        idx = norm_file.find(norm_search, start)
        if idx == -1:
            break
        matches.append(file_content[idx:idx + len(search_string)])
        start = idx + len(norm_search)
    return matches


def _find_actual_string(file_content: str, search_string: str) -> str | None:
    """向后兼容：只取第一处匹配（仅用于展示/探测，不用于唯一性判定）。"""
    matches = _find_all_actual_strings(file_content, search_string)
    return matches[0] if matches else None

#生成一个简单的文本差异格式
def _generate_diff(old_content: str, old_string: str, new_string: str) -> str:
    before_change = old_content.split(old_string)[0]
    line_num = before_change.count("\n") + 1
    old_lines = old_string.split("\n")
    new_lines = new_string.split("\n")

    parts = [f"@@ -{line_num},{len(old_lines)} +{line_num},{len(new_lines)} @@"]
    for l in old_lines:
        parts.append(f"- {l}")
    for l in new_lines:
        parts.append(f"+ {l}")
    return "\n".join(parts)

#编辑文件
def _edit_file(inp: dict) -> "ToolResult":
    try:
        path = _resolve_tool_path(inp["file_path"])
        content = read_text_migrating(path)

        matches = _find_all_actual_strings(content, inp["old_string"])
        if not matches:
            return ToolResult(f"Error: old_string not found in {inp['file_path']}",
                              status=TOOL_FAILED, error_code="match-not-found")
        if len(matches) > 1:
            # 唯一性必须按**实际命中数**判定（含引号归一变体），不能只数原文出现次数。
            return ToolResult(
                f"Error: old_string matched {len(matches)} locations in {inp['file_path']}. Must be unique.",
                status=TOOL_CONFLICT, error_code="match-not-unique")

        actual = matches[0]
        new_content = content.replace(actual, inp["new_string"], 1)

        # 原子写 + 写前重核（对齐 session.py 的原子写与 W09 验收的"锁内重核"）：
        # 1) content 是读进来的副本，写盘前磁盘可能已被外部改掉——必须用 mtime_ns/size
        #    复核磁盘现状与读取时一致，否则外部修改会被静默覆盖；
        # 2) os.replace 保证崩溃不留半截文件。
        try:
            current_stat = path.stat()
            disk_now = read_text_migrating(path)
        except OSError as error:
            return ToolResult(f"Error editing file: {error}", status=TOOL_FAILED,
                              error_code="stat-failed")
        if disk_now != content:
            return ToolResult(
                f"Warning: {inp['file_path']} changed on disk since it was read. "
                "Please read_file again before editing.",
                status=TOOL_CONFLICT, error_code="disk-changed-since-read")

        tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            tmp.write_text(new_content, encoding="utf-8")
            os.replace(str(tmp), str(path))
        except OSError:
            # 中断/替换失败不得残留 tmp（实测：失败路径会留下 .tmp 垃圾文件）。
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

        diff = _generate_diff(content, actual, inp["new_string"])

        quote_note = " (matched via quote normalization)" if actual != inp["old_string"] else ""

        return ToolResult(f"Successfully edited {inp['file_path']}{quote_note}\n\n{diff}",
                          status=TOOL_OK, artifact_paths=(str(path),))
    except Exception as e:  # noqa: BLE001
        return ToolResult(f"Error editing file: {e}", status=TOOL_FAILED, error_code=type(e).__name__)


def _list_files(inp: dict) -> "ToolResult":
    try:
        base = _resolve_tool_path(inp.get("path") or ".")
        pattern = inp["pattern"]
        files = []
        for p in base.glob(pattern):
            if p.is_file():
                rel = str(p.relative_to(base) if base != Path(".") else p)

                if "node_modules" in rel or ".git" in rel.split(os.sep):
                    continue
                files.append(rel)
                if len(files) >= 200:
                    break
        if not files:
            # 空结果不是失败：模式没命中属于正常查询结果。
            return ToolResult("No files found matching the pattern.", status=TOOL_OK,
                              metadata={"matches": 0})
        result = "\n".join(files[:200])

        if len(files) > 200:
            result += f"\n...and {len(files) - 200} more files are found ..."
        return ToolResult(result, status=TOOL_OK, metadata={"matches": len(files)})
    except FileNotFoundError as e:
        return ToolResult(f"Error listing files: {e}", status=TOOL_FAILED, error_code="path-missing")
    except Exception as e:  # noqa: BLE001
        return ToolResult(f"Error listing files: {e}", status=TOOL_FAILED, error_code=type(e).__name__)


def _grep_search(inp: dict) -> "ToolResult":
    pattern = inp["pattern"]
    try:
        path = str(_resolve_tool_path(inp.get("path") or "."))
    except Exception as error:  # noqa: BLE001
        return ToolResult(f"Error searching files: {error}", status=TOOL_FAILED,
                          error_code="path-invalid")
    include = inp.get("include")

    if not IS_WIN:
        try:
            args = ["grep", "--line-number", "--color=never", "-r"]
            if include:
                args.append(f"--include={include}")
            args.extend(["--", pattern, path])

            result = subprocess.run(
                args, capture_output=True, text=True, timeout=10
            )
            # grep 退出码：0=命中，1=无命中（正常结果），2=错误。只有 2 才是失败。
            if result.returncode == 1:
                return ToolResult("No matches found.", status=TOOL_OK, metadata={"matches": 0})
            if result.returncode == 0:
                lines = [l for l in result.stdout.split("\n") if l]
                output = "\n".join(lines[:100])
                if len(lines) >100:
                    output += f"\n... and {len(lines) - 100} more matches"
                return ToolResult(output, status=TOOL_OK, metadata={"matches": len(lines)})
            if result.returncode == 2:
                return ToolResult(
                    f"Error searching files: {(result.stderr or '').strip()[:300]}",
                    status=TOOL_FAILED, error_code="grep-error", exit_code=2,
                )
        except Exception:
            pass

    try:
        return ToolResult(_grep_python(pattern, path, include), status=TOOL_OK)
    except re.error as error:
        return ToolResult(f"Error searching files: invalid pattern: {error}",
                          status=TOOL_FAILED, error_code="invalid-pattern")
    except Exception as error:  # noqa: BLE001
        return ToolResult(f"Error searching files: {error}", status=TOOL_FAILED,
                          error_code=type(error).__name__)


def _grep_python(pattern: str, directory: str, include: str | None) -> str:
    regex = re.compile(pattern)
    include_pattern = include
    matches: list[str] = []

    def walk(d: str) -> None:
        if len(matches) >= 200:
            return
        try:
            entries = os.listdir(d)
        except Exception:
            return
        for name in entries:
            if name.startswith(".") or name == "node_modules":
                continue
            full = os.path.join(d, name)
            if os.path.isdir(full):
                walk(full)
                continue
            if include_pattern and not fnmatch.fnmatch(name, include_pattern):
                continue
            try:
                text = Path(full).read_text(errors="replace")
                for i, line in enumerate(text.split("\n")):
                    if regex.search(line):
                        matches.append(f"{full}:{i+1}:{line}")
                        if len(matches) >= 200:
                            return
            except Exception:
                pass

    walk(directory)
    if not matches:
        return "No matches found."
    output = "\n".join(matches[:100])
    if len(matches) > 100:
        output += f"\n... and {len(matches) - 100} more matches"
    return output



#--------截断过长的工具调用结果

def _env_int(name: str, default: int) -> int:
    """环境变量覆盖（非法值回退默认）：让截断上限成为 Harness² 可优化的真实轴。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


MAX_RESULT_CHARS = _env_int("EVOHARNESS_MAX_RESULT_CHARS", 50000)


def _truncate_result(result:str) -> str:
    if len(result) <= MAX_RESULT_CHARS:
        return result
    keep_each = (MAX_RESULT_CHARS - 60) // 2
    return (
        result[:keep_each]
        +f"\n\n[... truncated {len(result) - keep_each * 2} chars ...]\n\n"
        +result[-keep_each:]
    )






#----------延迟工具激活---------------------------
_activated_tools: set[str] = set()



def reset_activated_tools() -> None:
    _activated_tools.clear()

# MCP tool hints（readOnlyHint/destructiveHint/idempotentHint/openWorldHint）：
# 声明每个工具的行为特征，宿主（Claude/OpenAI 等 MCP 客户端）据此在调用前给
# 用户风险提示。MCP 生态的信任索引与 OpenAI 工具目录均要求四个 hint 为显式布尔。
# 清单定义在 tool_hints.py（静态文件，供仓库扫描类检查直接读取）。
from .tool_hints import TOOL_HINTS  # noqa: E402

def _with_hints(t: ToolDef) -> ToolDef:
    """给工具定义补 MCP hints（定义里已声明的优先，不覆盖）。"""
    hints = dict(t.get("hints") or TOOL_HINTS.get(t["name"]) or {})
    if hints:
        out = dict(t)
        out["hints"] = hints
        return out
    return t

def _without_hints(t: ToolDef) -> ToolDef:
    """剥掉 hints 字段：Anthropic/OpenAI 的 tool schema 不认识 MCP hints，
    直接发送会被 API 拒绝；hints 只用于 MCP 暴露与静态检查。"""
    if "hints" in t:
        return {k: v for k, v in t.items() if k != "hints"}
    return t

def get_active_tool_definitions(all_tools: list[ToolDef] | None = None, *, with_hints: bool = False) -> list[ToolDef]:
    """当前可用的工具定义。

    with_hints=True 时附带 MCP tool hints（供 MCP 客户端/静态检查消费）；
    默认 False，返回干净的协议 schema（Anthropic/OpenAI 调用路径用）。
    """
    tools = all_tools if all_tools is not None else tool_definitions
    decorate = _with_hints if with_hints else _without_hints
    return [
        {k: v for k, v in decorate(t).items() if k != "deferred"}
        for t in tools
        if not t.get("deferred") or t["name"] in _activated_tools
    ]

def get_deferred_tool_names(all_tools: list[ToolDef] | None = None) -> list[str]:
    tools = all_tools if all_tools is not None else tool_definitions
    return [t["name"] for t in tools if t.get("deferred") and t["name"] not in _activated_tools]

#执行shell命令
def _decode_console_output(raw: bytes | None) -> str:
    """按 UTF-8 → GBK 顺序严格解码子进程输出，都失败才降级占位符。

    中文 Windows 控制台命令（dir/ipconfig 等）输出 GBK，Python 等工具输出 UTF-8。
    GBK 解码容忍度高，必须排在 UTF-8 之后，否则会把真 UTF-8 解成乱中文。
    """
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _coerce_timeout_ms(value) -> float:
    # timeout 为空/非法/非正数时回退到默认 30000ms，避免 None 参与除法。
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return 30000.0
    if ms != ms or ms in (float("inf"), float("-inf")) or ms <= 0:
        return 30000.0
    return ms


def _shell_result(stdout: str, stderr: str, exit_code: int, duration_ms: int, timeout_ms: float) -> "ToolResult":
    """把 shell 执行事实翻译成结构化结果；非零退出是失败，与正文里有什么词无关。"""
    output = stdout or ""
    if exit_code == 0:
        return ToolResult(
            output or "(no output)",
            status=TOOL_OK,
            exit_code=0,
            duration_ms=duration_ms,
            metadata={"timeout_ms": timeout_ms, "stderrBytes": len(stderr or "")},
        )
    stderr_text = f"\nStderr: {stderr}" if stderr else ""
    stdout_text = f"\nStdout: {output}" if output else ""
    return ToolResult(
        f"Command failed (exit code {exit_code}){stdout_text}{stderr_text}",
        status=TOOL_FAILED,
        error_code="nonzero-exit",
        exit_code=exit_code,
        duration_ms=duration_ms,
        metadata={"timeout_ms": timeout_ms},
    )


def _kill_process_tree(pid: int) -> None:
    """逐级终止进程树：Windows 用 taskkill /T，POSIX 用 killpg；失败不抛。"""
    if not pid:
        return
    try:
        if IS_WIN:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass


async def _run_shell_async(inp: dict) -> "ToolResult":
    """非阻塞 shell：create_subprocess_exec + 有界输出 + 超时/取消时终止整棵进程树。

    旧实现用 `subprocess.run` 放在 async 函数里：4 秒命令期间事件循环被完全占满，
    /api/health 等协同工作无法响应（实测 4 秒内只有 3 次采样机会）。这里改成真正
    异步的子进程，并在超时或取消时 `taskkill /T`（POSIX killpg）回收子孙进程。
    """
    timeout_ms = _coerce_timeout_ms(inp.get("timeout"))
    timeout_s = timeout_ms / 1000
    started = time.time()
    command = inp.get("command")
    if IS_WIN:
        args = ["cmd", "/c", command]
    else:
        args = ["/bin/sh", "-c", command]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=not IS_WIN,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            _kill_process_tree(proc.pid)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
            duration_ms = int((time.time() - started) * 1000)
            return ToolResult(
                f"Command timed out after {timeout_ms}ms",
                status=TOOL_TIMEOUT,
                error_code="timeout",
                duration_ms=duration_ms,
                metadata={"timeout_ms": timeout_ms, "pid": proc.pid, "treeKilled": True},
            )
        duration_ms = int((time.time() - started) * 1000)
        return _shell_result(
            _decode_console_output(stdout),
            _decode_console_output(stderr),
            int(proc.returncode or 0),
            duration_ms,
            timeout_ms,
        )
    except asyncio.CancelledError:
        # 取消必须回收整棵进程树，否则子孙进程残留。
        if proc is not None and proc.returncode is None:
            _kill_process_tree(proc.pid)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
        raise
    except Exception as error:  # noqa: BLE001
        if proc is not None and proc.returncode is None:
            _kill_process_tree(proc.pid)
        return ToolResult(
            f"Error: {error}",
            status=TOOL_UNKNOWN,
            error_code=type(error).__name__,
            duration_ms=int((time.time() - started) * 1000),
            metadata={"timeout_ms": timeout_ms},
        )


def _run_shell(inp: dict) -> "ToolResult":
    timeout_ms = _coerce_timeout_ms(inp.get("timeout"))
    started = time.time()
    try:
        timeout_s = timeout_ms / 1000
        result = subprocess.run(
            inp["command"],
            shell=True,
            capture_output=True,
            timeout=timeout_s,
        )
        duration_ms = int((time.time() - started) * 1000)
        return _shell_result(
            _decode_console_output(result.stdout),
            _decode_console_output(result.stderr),
            int(result.returncode),
            duration_ms,
            timeout_ms,
        )
    except subprocess.TimeoutExpired as expired:
        duration_ms = int((time.time() - started) * 1000)
        partial_out = _decode_console_output(getattr(expired, "stdout", None))
        partial_err = _decode_console_output(getattr(expired, "stderr", None))
        detail = ""
        if partial_out:
            detail += f"\nStdout: {partial_out}"
        if partial_err:
            detail += f"\nStderr: {partial_err}"
        return ToolResult(
            f"Command timed out after {timeout_ms}ms{detail}",
            status=TOOL_TIMEOUT,
            error_code="timeout",
            duration_ms=duration_ms,
            metadata={"timeout_ms": timeout_ms},
        )
    except Exception as error:  # noqa: BLE001
        return ToolResult(
            f"Error: {error}",
            status=TOOL_UNKNOWN,
            error_code=type(error).__name__,
            duration_ms=int((time.time() - started) * 1000),
            metadata={"timeout_ms": timeout_ms},
        )


# 危险命令列表：收敛为一个极小的“灾难级”、形态稳定的封闭集合。
# 不再枚举“所有可能危险的命令 / 包装器 / 别名”（那是对抗面无界的根因：8 轮审计每轮都能
# 找到新写法）。这里只保留真正不可逆、且写法稳定的少数灾难级形态；其余风险一律交给
# 「封闭元字符集 force-confirm + 默认确认」覆盖（见 _shell_requires_confirm）。
_CATASTROPHIC_COMMAND_RE = re.compile(
    r"""\brm\s+-[A-Za-z]*r[A-Za-z]*f[A-Za-z]*\s+/(?:\s|\*|$|["';)&|])"""  # rm -rf /（清根；容忍尾随引号/分隔符）
    r"|\bmkfs(?:\.\w+)?\b"                                     # 格式化文件系统
    r"|\bdd\b[^\n]*\bof=/dev/"                                 # 直写块设备
    r"|:\s*\(\s*\)\s*\{.*\}\s*;\s*:",                          # fork bomb :(){ :|:& };:
    re.IGNORECASE,
)


def is_dangerous(command) -> bool:
    """是否命中极小的封闭“灾难级”命令列表（真正不可逆、形态稳定）。

    非 str（list/dict/None）无法按字面判定，返回 False 且绝不抛错；真正的“需确认”兜底
    由 _shell_requires_confirm 给出（见 check_permission 的短路求值顺序）。
    """
    return isinstance(command, str) and bool(command) and bool(_CATASTROPHIC_COMMAND_RE.search(command))

#权限规则
def _parse_rule(rule: str) -> dict:
    m = re.match(r"^([a-z_]+)\((.+)\)$", rule)
    if m:
        return {"tool": m.group(1), "pattern": m.group(2)}
    return {"tool": rule, "pattern": None}

def _load_settings(file_path: Path) -> dict | None:
    if not file_path.exists():
        return None
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return None

_cached_rules: dict | None = None



def load_permission_rules() -> dict:
    global _cached_rules
    if _cached_rules is not None:
        return _cached_rules

    allow: list[dict] = []
    deny: list[dict] = []

    user_settings = _load_settings(Path.home() / ".evoharness" / "settings.json")
    project_settings = _load_settings(Path.cwd() / ".evoharness" / "settings.json")

    for settings in [user_settings, project_settings]:
        if not settings or "permissions" not in settings:
            continue
        perms = settings["permissions"]
        for r in perms.get("allow", []):
            allow.append(_parse_rule(r))
        for r in perms.get("deny", []):
            deny.append(_parse_rule(r))

    _cached_rules = {"allow": allow, "deny": deny}
    return _cached_rules


def _canon_path_for_rule(p: str) -> str:
    # 把文件路径规范化为可比较形式：统一分隔符、折叠 ./ 与 ..、统一大小写（Windows）。
    if not isinstance(p, str):
        return ""
    q = p.replace("\\", "/")
    try:
        q = os.path.normpath(q)
    except Exception:
        pass
    return os.path.normcase(q).replace("\\", "/")


def _is_absolute_path_like(p: str) -> bool:
    return p.startswith("/") or bool(re.match(r"^[a-z]:/", p))


def _matches_rule(rule: dict, tool_name: str, inp: dict) -> bool:
    if rule["tool"] != tool_name:
        return False
    if rule["pattern"] is None:
        return True

    pattern = rule["pattern"]

    if tool_name == "run_shell":
        value = inp.get("command", "")
        if not isinstance(value, str):
            # 非 str 无字面可比，规则不命中（绝不抛 TypeError）；需确认兜底见
            # _shell_requires_confirm / _allow_rule_matches。
            return False
        # fnmatch 语义：支持两端/中部通配符（*rm -rf* / git * status 等）。
        if "*" in pattern:
            return fnmatch.fnmatchcase(value, pattern)
        return value == pattern

    if "file_path" not in inp:
        return True

    value = _canon_path_for_rule(inp["file_path"])
    if "*" in pattern:
        # fnmatch 语义：支持两端/中部通配符（**/*.pem / *.env 等）。
        canon_pattern = _canon_path_for_rule(pattern)
        if fnmatch.fnmatchcase(value, canon_pattern):
            return True
        # 相对 pattern 按任意深度后缀匹配绝对路径等变体。
        if not _is_absolute_path_like(canon_pattern):
            return fnmatch.fnmatchcase(value, "*/" + canon_pattern)
        return False

    canon_pattern = _canon_path_for_rule(pattern)
    if value == canon_pattern:
        return True
    # 相对 pattern（secret.txt / sub/../secret.txt）按路径后缀命中绝对路径等变体。
    if not _is_absolute_path_like(canon_pattern) and value.endswith("/" + canon_pattern):
        return True
    return False


# run_shell 的 force-confirm 判定：一个“小而封闭”的元字符集。
# 借鉴主流 agent（Claude Code / Cursor / Codex / Gemini CLI）的共识做法——不解析 shell
# 语义、不枚举危险命令/包装器/别名（对抗面无界），只判“命令里是否出现任意元字符”：
#   ; | &   → 命令串联 / 后台执行 / 子命令
#   < >     → 输入/输出重定向（含 >> 追加、落盘覆盖）
#   \n \r   → 换行拼接出的第二条命令
#   ` $ %   → 命令替换 / 变量展开（bash `...`·$var，cmd %VAR%）
# 命中任一即 force-confirm（在 allow 规则之前评估，任何前缀 allow 都不得覆盖）；只有不含
# 任何元字符的“简单字面命令”才可能被 allow 规则放行。刻意不解析引号——引号内的元字符同样
# 可能被 shell 展开（echo "$(rm -rf /)"），宁可多确认也不放行复合形态。
#
# 有意保守取舍：字面/无害文本里出现的 `% $ ; < > & |`（如 `git log --pretty=format:%H`、
# `echo 50%`）也会被一并升为确认——真实 shell 里这些本就是元字符。不为“精确判定”而重新
# 引入引号剔除（那会重开命令替换/注入回归）。
_SHELL_METACHARS_RE = re.compile(r"[;|&<>`$%\n\r]")


def _shell_requires_confirm(command) -> bool:
    """run_shell 是否必须 force-confirm（任何 allow 规则都不得覆盖）。

    仅依据封闭元字符集判定（见 _SHELL_METACHARS_RE）——不再枚举执行包装器 / 间接目标 /
    命令替换 / 复合分隔符等写法。这是有意的取舍：枚举法天生会被新写法绕过，而元字符集
    是封闭的、不可绕过的（含元字符即确认）。

    非 str（list/dict/None）一律视为需确认：无法按字面判定即保守处理，且绝不抛错——
    Anthropic 执行路径无兜底，抛 TypeError 会让整轮崩溃。
    """
    if not isinstance(command, str):
        return True
    if not command:
        return False
    return bool(_SHELL_METACHARS_RE.search(command))


def _allow_rule_matches(rule: dict, tool_name: str, inp: dict) -> bool:
    """allow 规则命中判定。run_shell 的通配前缀不得放行含 shell 分隔符/子命令的复合命令
    （`git status; curl … | sh` / `echo hi && shutdown` 等），此类需退化为 confirm。"""
    if not _matches_rule(rule, tool_name, inp):
        return False
    if tool_name == "run_shell" and "*" in (rule.get("pattern") or ""):
        return not _shell_requires_confirm(inp.get("command", ""))
    return True


def _check_permission_rules(tool_name: str, inp: dict) -> str | None:
    rules = load_permission_rules()
    for rule in rules["deny"]:
        if _matches_rule(rule, tool_name, inp):
            return "deny"
    for rule in rules["allow"]:
        if _allow_rule_matches(rule, tool_name, inp):
            return "allow"
    return None


VALID_PERMISSION_MODES = ("default", "plan", "acceptEdits", "bypassPermissions", "dontAsk")


def _normalize_permission_mode(mode) -> str:
    """规范化 mode（去空白、忽略大小写）并按白名单校验；未识别取值回退到最保守的
    default——绝不静默按“最小限制”处理，以免传入 'plan '/ 'Default' / 'foo' / '' 时
    plan 门禁或 default 强制 confirm 消失。"""
    m = str(mode).strip() if mode is not None else ""
    for valid in VALID_PERMISSION_MODES:
        if m.lower() == valid.lower():
            return valid
    return "default"


def check_permission(
    tool_name: str,
    inp: dict,
    mode: str = "default",
    plan_file_path: str | None = None,
) -> dict:
    """Returns {"action": "allow"|"deny"|"confirm", "message": ...}"""
    # 规范化 + 白名单校验 permission_mode：未识别取值按最保守的 default 处理，
    # 避免 'plan '/'Default'/'foo'/'' 之类的输入静默“失败开放”（plan 门禁 / default
    # 强制 confirm 消失）。
    mode = _normalize_permission_mode(mode)

    # deny 规则优先于一切（含 bypassPermissions）：用户显式拒绝不得被档位短路绕过。
    rule_result = _check_permission_rules(tool_name, inp)
    if rule_result == "deny":
        return {"action": "deny", "message": f"Denied by permission rule for {tool_name}"}

    if mode == "bypassPermissions":
        return {"action": "allow"}

    is_mcp = tool_name.startswith("mcp__")

    # plan / dontAsk 硬门禁优先于权限 allow 规则：不得被 settings 的 allow 规则放宽。
    # agent/skill 在 dontAsk 下同样自动拒绝（避免未受控的子代理/技能调用）。
    if mode == "plan":
        if tool_name in EDIT_TOOLS:
            file_path = inp.get("file_path") or inp.get("path")
            if plan_file_path and file_path == plan_file_path:
                return {"action": "allow"}
            return {"action": "deny", "message": f"Blocked in plan mode: {tool_name}"}
        if tool_name == "run_shell":
            return {"action": "deny", "message": "Shell commands blocked in plan mode"}
        if is_mcp:
            return {"action": "deny", "message": f"Blocked in plan mode: {tool_name}"}
    elif mode == "dontAsk" and (
        tool_name in EDIT_TOOLS
        or tool_name in ("run_shell", "agent", "skill")
        or is_mcp
    ):
        return {"action": "deny", "message": f"Auto-denied (dontAsk mode): {tool_name}"}

    # 敏感配置写入守卫：工作区根与用户主目录下的 .mcp.json / .evoharness/settings.json
    # 会被后续会话当作可信配置加载（自动 spawn 任意 command / 放行任意工具），不得被
    # settings 的 allow 规则自动放行。plan/dontAsk 已在上方硬门禁中 deny，这里对其余模式
    # 一律降级为 confirm。write_file / edit_file 侧目标可精确判定（OS 级 realpath 归一）。
    if tool_name in ("write_file", "edit_file"):
        raw_path = inp.get("file_path") or inp.get("path") or ""
        if _is_protected_config_edit(raw_path):
            return {"action": "confirm", "message": f"protected config write: {raw_path}"}
    elif tool_name == "run_shell" and (
        is_dangerous(inp.get("command", "")) or _shell_requires_confirm(inp.get("command", ""))
    ):
        # 策略级缓解（在 allow 规则之前评估，任何前缀 allow 都不得覆盖）：
        #   1) 灾难级命令（rm -rf / / mkfs / dd of=/dev/ / fork bomb）：极小封闭 denylist；
        #   2) 含 shell 元字符（; | & < > 换行 ` $ %）：封闭元字符集。
        # 有意取舍：run_shell 不再枚举“它写哪个文件”（无界、易被绕过），改由「默认确认 +
        # allow 白名单」兜底——含 `>` / `>>` 的重定向写配置也在此被元字符集一并拦下。
        return {"action": "confirm", "message": f"compound/catastrophic shell command: {inp.get('command', '')}"}

    if rule_result == "allow":
        return {"action": "allow"}

    if tool_name in READ_TOOLS:
        raw_path = inp.get("file_path") or inp.get("path")
        if raw_path and not _is_within_workspace(raw_path):
            if mode == "dontAsk":
                return {"action": "deny", "message": f"Auto-denied (dontAsk mode): read outside workspace: {raw_path}"}
            return {"action": "confirm", "message": f"read outside workspace: {raw_path}"}
        return {"action": "allow"}

    if is_mcp:
        # MCP 工具不在已知只读/编辑集合内，一律走确认，不再无条件放行。
        return {"action": "confirm", "message": f"confirm MCP tool: {tool_name}"}

    if tool_name in ("enter_plan_mode", "exit_plan_mode"):
        return {"action": "allow"}

    if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
        raw_path = inp.get("file_path") or inp.get("path")
        if raw_path and not _is_within_workspace(raw_path):
            return {"action": "confirm", "message": f"write outside workspace: {raw_path}"}
        return {"action": "allow"}

    needs_confirm = False
    confirm_message = ""

    raw_path = inp.get("file_path") or inp.get("path")
    outside_workspace = bool(raw_path) and not _is_within_workspace(raw_path)

    if tool_name == "run_shell" and mode in ("default", "acceptEdits"):
        # 灾难级 / 含元字符的命令已在上方 force-confirm；这里覆盖两档“shell 一律确认”。
        # acceptEdits 只应“自动接受编辑类工具”，run_shell 仍须确认（与 default 一致，对齐
        # Claude Code 语义）——否则无元字符的写敏感配置命令（powershell -com / certutil
        # -decode / curl -o / Set-Content）会在 acceptEdits 下免确认静默覆盖敏感配置。
        needs_confirm = True
        confirm_message = inp.get("command", "")
    elif tool_name in ("write_file", "edit_file") and outside_workspace:
        needs_confirm = True
        confirm_message = f"{tool_name} outside workspace: {raw_path}"
    elif tool_name == "write_file" and not _resolve_tool_path(inp.get("file_path", ""), must_exist=False).exists():
        needs_confirm = True
        confirm_message = f"write new file: {inp.get('file_path', '')}"
    elif tool_name == "edit_file" and not _resolve_tool_path(inp.get("file_path", "")).exists():
        needs_confirm = True
        confirm_message = f"edit non-existent file: {inp.get('file_path', '')}"
    elif tool_name == "skill_evolve":
        needs_confirm = True
        confirm_message = f"evolve skill: {inp.get('skill_name', '')}"
    elif tool_name == "skill_create":
        needs_confirm = True
        confirm_message = f"create skill: {inp.get('name', '')}"

    if needs_confirm:
        if mode == "dontAsk":
            return {"action": "deny", "message": f"Auto-denied (dontAsk mode): {confirm_message}"}
        return {"action": "confirm", "message": confirm_message}

    if tool_name in SESSION_CONTROL_TOOLS:
        return {"action": "allow"}

    # 已登记的内置工具：走到这里即未被任何确认分支命中（典型为 write_file/edit_file 写工作区
    # 内既有文件），恢复改动前的自动放行语义——它们绝不属于“未登记工具”，不得落入下方兜底。
    if tool_name in BUILTIN_TOOLS:
        return {"action": "allow"}

    # fail-closed：仅对真正未被任何分支识别/登记的工具名（可能是未来新增工具）默认不自动
    # 放行，一律要求用户确认；dontAsk 档直接拒绝。
    if mode == "dontAsk":
        return {"action": "deny", "message": f"Auto-denied (dontAsk mode): unrecognized tool {tool_name}"}
    return {"action": "confirm", "message": f"unrecognized tool requires confirmation: {tool_name}"}






#----------------执行工具调用-----------------------
# 'agent' 和 'skill' 这两个工具在 agent.py 中处理，以避免循环依赖。"

async def execute_tool(
    name: str, inp: dict, read_file_state: dict[str, float] | None = None
) -> "ToolResult":
    if name == "read_file":
        result = _read_file(inp)
        if read_file_state is not None and result.ok:
            abs_path = str(_resolve_tool_path(inp["file_path"]).resolve())
            try:
                read_file_state[abs_path] =  os.path.getmtime(abs_path)
            except OSError as error:  # noqa: BLE001
                # 登记失败 → 该文件不参与后续"写前重核"，并发覆盖保护降级。
                # 保持 fail-safe 降级（不登记错误值），但必须可见：正常路径不会触发。
                print(f"[read-state] cannot record mtime for {abs_path}: {error}")
        return _truncate_tool_result(result)

    if name in ("write_file", "edit_file") and read_file_state is not None:
        abs_path = str(_resolve_tool_path(inp["file_path"], must_exist=(name == "edit_file")).resolve())
        if os.path.exists(abs_path):
            verb = "writing" if name == "write_file" else "editing"
            if abs_path not in read_file_state:
                return ToolResult(
                    f"Error: You must read this file before {verb}. Use read_file first to see its current contents.",
                    status=TOOL_FAILED, error_code="read-required",
                )
            if os.path.getmtime(abs_path) != read_file_state[abs_path]:
                # 外部改动是冲突，不是普通失败：必须重读后重试。
                return ToolResult(
                    f"Warning: {inp['file_path']} was modified externally since your last read. Please read_file again before {verb}.",
                    status=TOOL_CONFLICT, error_code="mtime-conflict",
                )

    #搜索和激活延迟加载的工具。
    if name == "tool_search":
        query = (inp.get("query") or "").lower()
        deferred = [t for t in tool_definitions if t.get("deferred")]
        matches = [
            t for t in deferred
            if query in t["name"].lower() or query in (t.get("description") or "").lower()
        ]
        if not matches:
            return ToolResult("No matching deferred tools found.", status=TOOL_OK,
                              metadata={"matches": 0})

        for m in matches:
            _activated_tools.add(m["name"])

        return _truncate_tool_result(ToolResult(
            json.dumps(
                [{"name": t["name"], "description": t.get("description", ""), "input_schema": t["input_schema"]} for t in
                 matches],
                indent=2,
            ),
            status=TOOL_OK, metadata={"matches": len(matches)},
        ))
    if name == "skill_evolve":
        from .skills import evolve_skill

        result = evolve_skill(
            skill_name=inp.get("skill_name", ""),
            lesson=inp.get("lesson", ""),
            rationale=inp.get("rationale", ""),
            target=inp.get("target", "active"),
        )
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        return _truncate_tool_result(ToolResult(
            json.dumps(result, ensure_ascii=False, indent=2),
            status=TOOL_OK if ok else TOOL_FAILED,
            error_code="" if ok else str((result or {}).get("error") or "skill-evolve-failed")[:120],
        ))

    if name == "skill_create":
        from .skills import create_skill

        result = create_skill(
            name=inp.get("name", ""),
            description=inp.get("description", ""),
            instructions=inp.get("instructions", ""),
            when_to_use=inp.get("when_to_use", "") or inp.get("when-to-use", ""),
            target=inp.get("target", "project"),
            context=inp.get("context", "inline"),
            user_invocable=bool(inp.get("user_invocable", False)),
            allowed_tools=inp.get("allowed_tools"),
            evidence=inp.get("evidence", ""),
        )
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        return _truncate_tool_result(ToolResult(
            json.dumps(result, ensure_ascii=False, indent=2),
            status=TOOL_OK if ok else TOOL_FAILED,
            error_code="" if ok else str((result or {}).get("error") or "skill-create-failed")[:120],
        ))

    # run_shell 走真正的异步子进程：同步 subprocess.run 会阻塞整个事件循环。
    if name == "run_shell":
        return _truncate_tool_result(await _run_shell_async(inp))

    handlers: dict = {
        "write_file": _write_file,
        "edit_file": _edit_file,
        "list_files": _list_files,
        "grep_search": _grep_search,
    }
    handler = handlers.get(name)

    if not handler:
        return ToolResult(f"Unknown tool: {name}", status=TOOL_FAILED, error_code="unknown-tool")

    result = _truncate_tool_result(handler(inp))

    # 更新时间：只在该次写入/编辑真正成功后才登记读状态，失败不得冒充已同步。
    if name in ("write_file", "edit_file") and read_file_state is not None and result.ok:
        abs_path = str(_resolve_tool_path(inp["file_path"], must_exist=False).resolve())
        try:
            read_file_state[abs_path] = os.path.getmtime(abs_path)
        except OSError as error:  # noqa: BLE001
            # 同上：写入成功但登记失败，后续该文件的"写前重核"会跳过，必须可见。
            print(f"[read-state] cannot record mtime after write for {abs_path}: {error}")

    return result


def _truncate_tool_result(result: "ToolResult") -> "ToolResult":
    """截断正文但保留机器状态与产物；截断不是失败。"""
    text = _truncate_result(str(result))
    if len(text) == len(str(result)):
        return result
    return ToolResult(
        text,
        status=result.status,
        error_code=result.error_code,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        artifact_paths=result.artifact_paths,
        metadata={**result.metadata, "truncated": True, "originalChars": len(str(result))},
    )



def reset_permission_cache() -> None:
    global _cached_rules
    _cached_rules = None



