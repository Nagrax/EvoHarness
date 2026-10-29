"""W03 回归：工具结果的机器状态不得由正文内容决定。

历史缺陷（已修）：
- `_looks_like_tool_failure` 扫描正文里的 "error"/"denied"/"timeout"，于是
  读一份记录错误码的文档被误判为失败；
- 同一扫描对 `Command failed (exit code 3)` 命中不了任何关键词 → 真失败记为成功；
- MCP `isError=true` 被丢弃，只拼 content 文本；
- 失败写入仍会登记 artifact（假产物）。

本文件只测状态判定，不联网、不调模型。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.tools as tools
from agents.agent import utf8_tool_result
from agents.tools import (
    FAILURE_STATUSES,
    TOOL_CONFLICT,
    TOOL_DENIED,
    TOOL_FAILED,
    TOOL_OK,
    TOOL_TIMEOUT,
    ToolResult,
    coerce_tool_result,
    execute_tool,
)


def _agent_stub():
    from agents.agent import Agent

    return Agent.__new__(Agent)


# ─── ① 成功正文里含 error / denied / timeout 不得判失败 ─────────────


def test_read_success_with_error_words_is_not_failure(tmp_path):
    doc = tmp_path / "errors.md"
    doc.write_text("ERROR_CODE_TABLE: 500 error handled; request denied; timeout=30s\n", encoding="utf-8")
    result = asyncio.run(execute_tool("read_file", {"file_path": str(doc)}))
    assert isinstance(result, ToolResult)
    assert result.status == TOOL_OK and result.ok is True
    assert "ERROR_CODE_TABLE" in result
    # 主循环判定必须与结构化状态一致，不能退回正文扫描
    assert _agent_stub()._looks_like_tool_failure("read_file", "", result) is False


def test_read_result_remains_str_for_protocol_edges(tmp_path):
    doc = tmp_path / "plain.md"
    doc.write_text("hello\n", encoding="utf-8")
    result = asyncio.run(execute_tool("read_file", {"file_path": str(doc)}))
    # 协议边缘仍消费字符串：isinstance / startswith / len 必须照旧可用
    assert isinstance(result, str)
    assert len(result) > 0
    assert not result.startswith("Error")
    assert str(result) == result.render()


# ─── ② 真实失败必须判失败（旧实现漏判） ──────────────────────────


def test_shell_nonzero_exit_is_failure():
    result = asyncio.run(execute_tool("run_shell", {"command": "cmd /c exit 3"}))
    assert result.status == TOOL_FAILED
    assert result.ok is False
    assert result.exit_code == 3
    assert "exit code 3" in result
    assert _agent_stub()._looks_like_tool_failure("run_shell", "", result) is True


def test_shell_success_is_not_failure():
    result = asyncio.run(execute_tool("run_shell", {"command": "cmd /c echo evo_w03_ok"}))
    assert result.status == TOOL_OK and result.ok is True
    assert result.exit_code == 0
    assert "evo_w03_ok" in result


def test_shell_timeout_has_own_status():
    result = asyncio.run(execute_tool("run_shell", {"command": "cmd /c ping -n 6 127.0.0.1 > nul", "timeout": 300}))
    assert result.status == TOOL_TIMEOUT
    assert result.ok is False
    assert result.error_code == "timeout"


def test_missing_file_is_failure_with_code(tmp_path):
    result = asyncio.run(execute_tool("read_file", {"file_path": str(tmp_path / "absent.md")}))
    assert result.status == TOOL_FAILED
    assert result.error_code == "file-missing"


# ─── ③ 读写前置条件与冲突使用专门状态 ────────────────────────────


def test_write_without_prior_read_is_failure(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("orig\n", encoding="utf-8")
    state: dict[str, float] = {}
    result = asyncio.run(execute_tool("write_file", {"file_path": str(target), "content": "new\n"}, state))
    assert result.status == TOOL_FAILED
    assert result.error_code == "read-required"


def test_external_mtime_change_is_conflict(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("orig\n", encoding="utf-8")
    state: dict[str, float] = {}
    asyncio.run(execute_tool("read_file", {"file_path": str(target)}, state))
    import os as _os
    _os.utime(target, (0, 0))
    result = asyncio.run(execute_tool("write_file", {"file_path": str(target), "content": "new\n"}, state))
    assert result.status == TOOL_CONFLICT
    assert result.ok is False
    assert result.error_code == "mtime-conflict"


# ─── ④ 假 artifact 必须为 0 ────────────────────────────────────


def test_failed_write_registers_no_artifact(tmp_path):
    missing_parent = tmp_path / "blocked"  # 用文件占住父路径，制造真实写入失败
    missing_parent.write_text("not a dir\n", encoding="utf-8")
    target = missing_parent / "child.txt"
    result = asyncio.run(execute_tool("write_file", {"file_path": str(target), "content": "x"}))
    assert result.ok is False
    assert result.artifact_paths == ()


def test_successful_write_registers_artifact(tmp_path):
    target = tmp_path / "ok.txt"
    result = asyncio.run(execute_tool("write_file", {"file_path": str(target), "content": "x"}))
    assert result.ok is True
    assert result.artifact_paths == (str(target.resolve()),)


def test_edit_conflict_registers_no_artifact(tmp_path):
    target = tmp_path / "e.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    asyncio.run(execute_tool("read_file", {"file_path": str(target)}))
    result = asyncio.run(execute_tool("edit_file", {"file_path": str(target),
                                                     "old_string": "alpha", "new_string": "gamma"}))
    # 成功编辑登记产物；这里重点验证"未命中"路径不登记
    assert result.ok is True
    miss = asyncio.run(execute_tool("edit_file", {"file_path": str(target),
                                                   "old_string": "zzz", "new_string": "yyy"}))
    assert miss.status == TOOL_FAILED and miss.artifact_paths == ()


# ─── ⑤ 拒绝与未知工具不再靠正文猜 ────────────────────────────


def test_denied_result_carries_denied_status():
    from agents.agent import denied_tool_result

    perm_denied = denied_tool_result("deny", "risky command")
    assert isinstance(perm_denied, str)
    assert coerce_tool_result(perm_denied).status == TOOL_DENIED
    assert _agent_stub()._looks_like_tool_failure("run_shell", "", perm_denied) is True

    user_denied = denied_tool_result("user-denied")
    assert coerce_tool_result(user_denied).status == TOOL_DENIED
    assert str(user_denied) == "User denied this action."


def test_unknown_tool_is_failure():
    result = asyncio.run(execute_tool("no_such_tool", {}))
    assert result.status == TOOL_FAILED
    assert result.error_code == "unknown-tool"


# ─── ⑥ 状态在 UTF-8 归一化后不丢失（主循环真实路径） ──────────────


def test_utf8_normalization_preserves_status():
    original = ToolResult("Command failed (exit code 7)", status=TOOL_FAILED,
                          error_code="nonzero-exit", exit_code=7)
    normalized = utf8_tool_result(original)
    assert isinstance(normalized, ToolResult)
    assert normalized.status == TOOL_FAILED
    assert normalized.exit_code == 7
    assert str(normalized) == "Command failed (exit code 7)"


def test_tool_exception_path_marked_failed():
    normalized = utf8_tool_result("Error executing tool: boom")
    assert coerce_tool_result(normalized).status in FAILURE_STATUSES
    assert coerce_tool_result(normalized).error_code == "tool-exception"


def test_plain_text_defaults_to_ok():
    normalized = utf8_tool_result("nothing suspicious here")
    assert coerce_tool_result(normalized).status == TOOL_OK


# ─── ⑦ MCP isError 成为机器状态 ──────────────────────────────


def _mcp_server_with_result(payload):
    from agents.mcp_client import McpConnection

    server = McpConnection.__new__(McpConnection)

    async def fake_send(_method, _params):
        return payload

    server._send_request = fake_send
    return server


def test_mcp_iserror_true_is_failure():
    server = _mcp_server_with_result({"isError": True, "content": [{"type": "text", "text": "tool blew up"}]})
    result = asyncio.run(server.call_tool("t", {}))
    assert isinstance(result, ToolResult)
    assert result.status == TOOL_FAILED
    assert result.error_code == "mcp-is-error"
    assert result.metadata.get("isError") is True


def test_mcp_normal_payload_with_error_word_is_success():
    server = _mcp_server_with_result(
        {"isError": False, "content": [{"type": "text", "text": "no error found in 0 files"}]}
    )
    result = asyncio.run(server.call_tool("t", {}))
    assert result.status == TOOL_OK
    assert "no error found" in result
    assert _agent_stub()._looks_like_tool_failure("mcp__s__t", "", result) is False


# ─── ⑧ 兼容性：历史裸字符串仍按成功处理 ────────────────────────


def test_legacy_plain_string_is_ok():
    assert coerce_tool_result("Success").status == TOOL_OK
    assert tools.tool_result_ok("Success") is True
    assert tools.tool_result_artifacts("Success") == ()


def test_truncation_keeps_status():
    long_text = "x" * (tools.MAX_RESULT_CHARS + 500)
    result = asyncio.run(execute_tool("run_shell", {"command": "cmd /c echo placeholder"}))
    truncated = tools._truncate_tool_result(ToolResult(long_text, status=TOOL_FAILED,
                                                       error_code="nonzero-exit", exit_code=9))
    assert len(str(truncated)) < len(long_text)
    assert truncated.status == TOOL_FAILED
    assert truncated.exit_code == 9
    assert truncated.metadata.get("truncated") is True
