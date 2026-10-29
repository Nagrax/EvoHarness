"""E-05 回归：折叠尾部保留（只折旧消息段、保留最近完整 N 轮，保护工具调用/结果配对）。

历史消融（GAIA 165 配对，McNemar p=0.011）定位折叠「保活有效、保质为负」。旧实现在折叠时
把整段历史（含最近轮）替换成一则摘要，造成推理链断裂与返工。本文件锁定可选尾部保留：

- `split_keep_recent_tail` 的切分正确性（含轮数不足、keep<=0 兼容）；
- 边界不得产生悬空工具结果（recent 不得以 result 消息开头）；
- `_compact_*` 集成：keep>0 时保留最近轮、keep<=0 时保持旧行为（整段折叠）。

纯函数 + Agent 实例（monkeypatch 掉 side query 与落盘），零模型、零网络。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.agent as agent_mod  # noqa: E402
from agents.agent import Agent  # noqa: E402
from agents.session_memory import (  # noqa: E402
    fallback_folded_memory,
    split_keep_recent_tail,
)


def _plain_user(text):
    return {"role": "user", "content": text}


def _assistant(text):
    return {"role": "assistant", "content": text}


def _result_msg(anthropic, call_id="t1"):
    if anthropic:
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "r"}]}
    return {"role": "tool", "tool_call_id": call_id, "content": "r"}


def _call_msg(anthropic, call_id="t1"):
    if anthropic:
        return {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": "read_file", "input": {}}]}
    return {"role": "assistant", "tool_calls": [{"id": call_id, "function": {"name": "read_file", "arguments": "{}"}}]}


# ── 切分纯函数 ───────────────────────────────────────────────────────────────

def test_keep_zero_is_backward_compatible():
    msgs = [_plain_user("u1"), _assistant("a1"), _plain_user("u2"), _assistant("a2")]
    older, recent = split_keep_recent_tail(msgs, 0, anthropic=False)
    assert older == msgs and recent == []


def test_keep_recent_turns_openai():
    body = [_plain_user("u1"), _assistant("a1"), _plain_user("u2"), _assistant("a2")]
    older, recent = split_keep_recent_tail(body, 1, anthropic=False)
    assert older == body[:2]
    assert recent == body[2:]


def test_tool_result_user_is_not_a_turn_anthropic():
    body = [
        _plain_user("u1"), _call_msg(True), _result_msg(True), _assistant("a1"),
        _plain_user("u2"), _assistant("a2"),
    ]
    # 轮起点只有 u1 与 u2 → keep 1 时 recent 从 u2 开始（tool_result 不被当作轮）。
    older, recent = split_keep_recent_tail(body, 1, anthropic=True)
    assert recent == body[4:]
    assert older == body[:4]


def test_insufficient_turns_returns_no_older():
    body = [_plain_user("u1"), _assistant("a1")]
    older, recent = split_keep_recent_tail(body, 3, anthropic=False)
    assert older == [] and recent == body


def test_boundary_never_starts_with_tool_result():
    histories = [
        [_plain_user("u1"), _call_msg(True), _result_msg(True), _assistant("a1"), _plain_user("u2"), _assistant("a2")],
        [_plain_user("u1"), _assistant("a1"), _plain_user("u2"), _call_msg(False), _result_msg(False), _assistant("a2"), _plain_user("u3")],
    ]
    for anthropic in (True, False):
        for h in histories:
            for keep in (1, 2):
                older, recent = split_keep_recent_tail(h, keep, anthropic=anthropic)
                if recent:
                    first = recent[0]
                    if anthropic:
                        assert not (isinstance(first.get("content"), list) and first["content"] and first["content"][0].get("type") == "tool_result")
                    else:
                        assert first.get("role") != "tool"


def _has_tool_result(msg) -> bool:
    content = msg.get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def test_mixed_tool_result_user_is_never_a_turn_start():
    """混合消息（tool_result + text，由记忆注入真实产生）不得被当作普通用户轮起点，
    否则 recent 会以一条悬空 tool_result 开头（Anthropic 400）。"""
    mixed = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "r"},
        {"type": "text", "text": "note"},
    ]}
    history = [
        _plain_user("u1"), _call_msg(True), mixed, _assistant("a1"),
        _plain_user("u2"), _assistant("a2"),
        _plain_user("u3"), _assistant("a3"),
    ]
    for keep in (1, 2, 3, 4):
        older, recent = split_keep_recent_tail(history, keep, anthropic=True)
        if recent:
            assert not _has_tool_result(recent[0]), f"recent 以悬空工具结果开头（keep={keep}）"


def test_anthropic_compaction_mixed_message_yields_legal_sequence(monkeypatch):
    _patch_fold(monkeypatch)
    monkeypatch.setenv("EVOHARNESS_FOLD_KEEP_RECENT_TURNS", "1")
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)
    agent.use_openai = False
    agent._anthropic_messages = [
        _plain_user("u1"), _call_msg(True),
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "r"},
            {"type": "text", "text": "note"},
        ]},
        _assistant("a1"), _plain_user("u2"), _assistant("a2"),
    ]
    ok = asyncio.run(agent._compact_anthropic(trigger="test"))
    assert ok is True
    msgs = agent._anthropic_messages
    assert msgs[0]["role"] == "user" and "u2" in msgs[0]["content"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]  # 无连续 user（Anthropic 合法）
    assert not any(
        msgs[i]["role"] == "user" and msgs[i + 1]["role"] == "user" for i in range(len(msgs) - 1)
    )


# ── _compact_* 集成（monkeypatch 掉 side query 与落盘） ──────────────────────

def _patch_fold(monkeypatch):
    async def fake_gen(self, transcript):
        return fallback_folded_memory("hist")

    monkeypatch.setattr(Agent, "_generate_folded_session_memory", fake_gen)
    monkeypatch.setattr(Agent, "_refresh_runtime_system_prompt", lambda self: None)
    monkeypatch.setattr(agent_mod, "save_folded_session_memory", lambda *a, **k: None)


def test_openai_compaction_keeps_recent_turns(monkeypatch):
    _patch_fold(monkeypatch)
    monkeypatch.setenv("EVOHARNESS_FOLD_KEEP_RECENT_TURNS", "1")
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)
    agent.use_openai = True
    agent._openai_messages = [
        {"role": "system", "content": "sys"},
        _plain_user("u1"), _assistant("a1"),
        _plain_user("u2"), _assistant("a2"),
        _plain_user("u3"), _assistant("a3"),
    ]
    ok = asyncio.run(agent._compact_openai(trigger="test"))
    assert ok is True
    msgs = agent._openai_messages
    assert msgs[0] == {"role": "system", "content": "sys"}      # system 保留
    assert msgs[1]["role"] == "user" and "u3" in msgs[1]["content"]  # 摘要并入最近轮用户
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert agent._fold_count == 1


def test_anthropic_compaction_keeps_recent_turns(monkeypatch):
    _patch_fold(monkeypatch)
    monkeypatch.setenv("EVOHARNESS_FOLD_KEEP_RECENT_TURNS", "1")
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)
    agent.use_openai = False
    agent._anthropic_messages = [
        _plain_user("u1"), _assistant("a1"),
        _plain_user("u2"), _assistant("a2"),
    ]
    ok = asyncio.run(agent._compact_anthropic(trigger="test"))
    assert ok is True
    msgs = agent._anthropic_messages
    assert msgs[0]["role"] == "user" and "u2" in msgs[0]["content"]
    assert msgs[1]["content"].startswith("a2")
    assert agent._fold_count == 1


def test_keep_zero_compaction_replaces_whole_history(monkeypatch):
    _patch_fold(monkeypatch)
    monkeypatch.delenv("EVOHARNESS_FOLD_KEEP_RECENT_TURNS", raising=False)  # 默认 0 → 旧行为
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)
    agent.use_openai = True
    agent._openai_messages = [
        {"role": "system", "content": "sys"},
        _plain_user("u1"), _assistant("a1"),
        _plain_user("u2"), _assistant("a2"),
    ]
    ok = asyncio.run(agent._compact_openai(trigger="test"))
    assert ok is True
    assert [m["role"] for m in agent._openai_messages] == ["system", "user"]  # 整段折叠（兼容）
