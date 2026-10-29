"""主循环 / 历史归一化 / 子代理凭据的针对性回归（零网络）。

覆盖 finding：
1. OpenAI 同一轮多个并发安全工具各执行一次，且不写重复 tool_call_id。
2. OpenAI 预算超限收尾为未执行的 tool_call 补占位 tool 消息（配对，无悬空）。
3. _normalize_anthropic_messages：tool_result 部分覆盖时保持 assistant/tool_result 成对。
4. 子代理继承父代理的 api_key / anthropic_base_url（两条协议）。
5. 子代理继承父代理 permission_mode（agent 工具 / skill fork），不再默认 bypassPermissions。
6. OpenAI 畸形 tool_call（非 function / 缺 function / 缺 name）补占位、不崩溃、不悬空。
7. _normalize_anthropic_messages 清理孤儿 tool_result（含跨协议迁移）。
8. OpenAI 并发批也将原始 raw 传给失败判定（与非并发批对称）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.agent as agent_mod
import agents.skill_tiers as skill_tiers_mod
import agents.skills as skills_mod
from agents.agent import Agent
from agents.main import _agent_is_processing
from agents.tools import check_permission


# ─── ① OpenAI：并发安全工具本轮只执行一次 ────────────────────────


def _resp_tool_calls(pairs) -> dict:
    return {"choices": [{"message": {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps(inp)}}
            for cid, name, inp in pairs
        ],
    }}]}


def _resp_final(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _make_openai_agent(**kw) -> Agent:
    kwargs = dict(
        api_base="http://127.0.0.1:9/v1",
        api_key="test-key",
        custom_system_prompt="sys",
        is_sub_agent=True,
        permission_mode="bypassPermissions",
    )
    kwargs.update(kw)
    return Agent(**kwargs)


def test_openai_concurrent_safe_tools_executed_once_per_round():
    agent = _make_openai_agent()
    executed: list[str] = []
    responses = [
        _resp_tool_calls([
            ("call_a", "read_file", {"file_path": "f1"}),
            ("call_b", "read_file", {"file_path": "f2"}),
        ]),
        _resp_final("done"),
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        executed.append(inp["file_path"])
        return "content"

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_openai("go"))

    assert sorted(executed) == ["f1", "f2"]  # 每个工具恰好执行一次，无重复执行
    tool_ids = [m["tool_call_id"] for m in agent._openai_messages if m.get("role") == "tool"]
    assert sorted(tool_ids) == ["call_a", "call_b"]  # 无重复 tool_call_id


# ─── ② OpenAI：预算超限补占位 tool 消息（无悬空 tool_calls）──────


def test_openai_budget_exceeded_pairs_tool_calls_with_placeholder():
    agent = _make_openai_agent(max_turns=1)
    executed: list[str] = []
    responses = [
        _resp_tool_calls([("call_a", "read_file", {"file_path": "f1"})]),
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        executed.append(inp["file_path"])
        return "content"

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_openai("go"))

    assert executed == []  # 预算超限，未执行任何工具
    call_ids = {tc["id"] for m in agent._openai_messages for tc in (m.get("tool_calls") or [])}
    tool_ids = {m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"}
    assert call_ids == {"call_a"}
    assert tool_ids == call_ids  # 每个 tool_call 都有占位 tool 回复，保持配对


def test_openai_budget_exceeded_pairs_tool_call_missing_id_with_placeholder():
    """回归：预算超限分支曾只对 tc.get('id') 为真者补占位，缺 id 者原样存回、悬空持久化。"""
    agent = _make_openai_agent(max_turns=1)
    responses = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"type": "function",
             "function": {"name": "read_file", "arguments": "{\"file_path\": \"f1\"}"}},
        ]}}]},
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        raise AssertionError("budget exceeded: 不应执行任何工具")

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_openai("go"))

    call_ids = [tc["id"] for m in agent._openai_messages for tc in (m.get("tool_calls") or [])]
    tool_ids = [m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"]
    assert len(call_ids) == 1 and call_ids[0]  # 缺 id 者已补稳定占位 id（旧实现原样存回 → 空 id）
    assert tool_ids == call_ids                # 逐对配对，无悬空


# ─── ③ _normalize_anthropic_messages：部分 tool_result 保持成对 ──


def _assert_anthropic_pairing(msgs: list[dict]) -> None:
    for idx, msg in enumerate(msgs):
        content = msg.get("content")
        if msg.get("role") == "assistant" and isinstance(content, list):
            uses = {b.get("id") for b in content if isinstance(b, dict) and b.get("type") == "tool_use"}
            if uses:
                nxt = msgs[idx + 1] if idx + 1 < len(msgs) else None
                results = (
                    {b.get("tool_use_id") for b in nxt["content"]
                     if isinstance(b, dict) and b.get("type") == "tool_result"}
                    if nxt and nxt.get("role") == "user" and isinstance(nxt.get("content"), list)
                    else set()
                )
                assert uses == results, f"assistant tool_use 未被完整回答: {uses} != {results}"
        if msg.get("role") == "user" and isinstance(content, list):
            results = {b.get("tool_use_id") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"}
            if results:
                prev = msgs[idx - 1] if idx > 0 else None
                uses = (
                    {b.get("id") for b in prev["content"]
                     if isinstance(b, dict) and b.get("type") == "tool_use"}
                    if prev and prev.get("role") == "assistant" and isinstance(prev.get("content"), list)
                    else set()
                )
                assert results <= uses, f"出现只有 tool_result、没有 tool_use 的非法序列: {results}"


def test_normalize_anthropic_partial_tool_results_keeps_pairing():
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)  # anthropic 路径
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "u1", "name": "read_file", "input": {"file_path": "a"}},
            {"type": "tool_use", "id": "u2", "name": "read_file", "input": {"file_path": "b"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "r1"},
        ]},
    ]
    out = agent._normalize_anthropic_messages(msgs)
    _assert_anthropic_pairing(out)
    # assistant 保留，两个 tool_use 都有对应 tool_result（缺失的已补齐）
    answered = {
        b.get("tool_use_id")
        for m in out if m.get("role") == "user" and isinstance(m.get("content"), list)
        for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
    }
    assert {"u1", "u2"} <= answered


def test_normalize_anthropic_orphan_tool_use_still_dropped():
    """无任何 tool_result 的孤儿 assistant tool_use 仍按原语义丢弃。"""
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "u1", "name": "read_file", "input": {}},
        ]},
        {"role": "user", "content": "next turn"},
    ]
    out = agent._normalize_anthropic_messages(msgs)
    _assert_anthropic_pairing(out)
    assert all(
        not (m.get("role") == "assistant" and isinstance(m.get("content"), list)
             and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m["content"]))
        for m in out
    )


# ─── ④ 子代理继承父代理凭据 / base url ───────────────────────────


def _install_fake_subagent(monkeypatch, captured: dict) -> None:
    class _FakeSub:
        def __init__(self, **kwargs):
            captured.clear()
            captured.update(kwargs)

        async def run_once(self, prompt):
            return {"text": "ok", "tokens": {"input": 0, "output": 0}}

    monkeypatch.setattr(agent_mod, "Agent", _FakeSub)


def _parent(use_openai: bool) -> Agent:
    kwargs = dict(custom_system_prompt="sys", is_sub_agent=True)
    if use_openai:
        kwargs.update(api_base="http://127.0.0.1:9/v1", api_key="parent-key")
    else:
        kwargs.update(api_key="parent-key", anthropic_base_url="http://127.0.0.1:9")
    return Agent(**kwargs)


def test_agent_tool_subagent_inherits_parent_credentials(monkeypatch):
    monkeypatch.setattr(agent_mod, "get_sub_agent_config",
                        lambda t: {"system_prompt": "s", "tools": []})
    for use_openai in (False, True):
        agent = _parent(use_openai)
        captured: dict = {}
        _install_fake_subagent(monkeypatch, captured)
        asyncio.run(agent._execute_agent_tool(
            {"type": "general", "description": "d", "prompt": "p"}))
        assert captured["api_key"] == "parent-key"
        if use_openai:
            assert captured["api_base"] == str(agent._openai_client.base_url)
        else:
            assert captured["api_base"] is None
            assert captured["anthropic_base_url"] == "http://127.0.0.1:9"


def test_skill_fork_subagent_inherits_parent_credentials(monkeypatch):
    monkeypatch.setattr(skills_mod, "execute_skill",
                        lambda name, args: {"context": "fork", "prompt": "p", "allowed_tools": None})
    monkeypatch.setattr(skill_tiers_mod, "fork_blocked_reason", lambda name: "")
    for use_openai in (False, True):
        agent = _parent(use_openai)
        captured: dict = {}
        _install_fake_subagent(monkeypatch, captured)
        asyncio.run(agent._execute_skill_tool({"skill_name": "x", "args": "a"}))
        assert captured["api_key"] == "parent-key"
        if use_openai:
            assert captured["api_base"] == str(agent._openai_client.base_url)
        else:
            assert captured["anthropic_base_url"] == "http://127.0.0.1:9"


# ─── ⑤ 内置代理类型不可被自定义文件覆盖 / 自定义白名单剔除 agent ──────


def _fake_custom(_agents: dict):
    import agents.subagent as subagent_mod
    return lambda: _agents


def test_builtin_explore_not_overridden_by_custom(monkeypatch):
    import agents.subagent as subagent_mod
    custom = {"explore": {"name": "explore", "description": "evil",
                          "allowed_tools": ["read_file", "write_file", "run_shell"],
                          "system_prompt": "evil prompt"}}
    monkeypatch.setattr(subagent_mod, "_discover_custom_agents", _fake_custom(custom))
    cfg = subagent_mod.get_sub_agent_config("explore")
    names = {t["name"] for t in cfg["tools"]}
    assert names <= subagent_mod.READ_ONLY_TOOLS
    assert "write_file" not in names and "run_shell" not in names
    assert cfg["system_prompt"] == subagent_mod.EXPLORE_PROMPT


def test_builtin_plan_not_overridden_by_custom(monkeypatch):
    import agents.subagent as subagent_mod
    custom = {"plan": {"name": "plan", "description": "d",
                       "allowed_tools": ["run_shell"], "system_prompt": "evil"}}
    monkeypatch.setattr(subagent_mod, "_discover_custom_agents", _fake_custom(custom))
    cfg = subagent_mod.get_sub_agent_config("plan")
    names = {t["name"] for t in cfg["tools"]}
    assert names <= subagent_mod.READ_ONLY_TOOLS
    assert cfg["system_prompt"] == subagent_mod.PLAN_PROMPT


def test_custom_allowed_tools_never_include_agent(monkeypatch):
    import agents.subagent as subagent_mod
    custom = {"helper": {"name": "helper", "description": "d",
                         "allowed_tools": ["agent", "run_shell"], "system_prompt": "p"}}
    monkeypatch.setattr(subagent_mod, "_discover_custom_agents", _fake_custom(custom))
    cfg = subagent_mod.get_sub_agent_config("helper")
    names = {t["name"] for t in cfg["tools"]}
    assert "agent" not in names
    assert "run_shell" in names


def test_custom_without_whitelist_excludes_agent(monkeypatch):
    import agents.subagent as subagent_mod
    custom = {"helper2": {"name": "helper2", "description": "d",
                          "allowed_tools": None, "system_prompt": "p"}}
    monkeypatch.setattr(subagent_mod, "_discover_custom_agents", _fake_custom(custom))
    cfg = subagent_mod.get_sub_agent_config("helper2")
    names = {t["name"] for t in cfg["tools"]}
    assert "agent" not in names
    assert names == {t["name"] for t in subagent_mod.tool_definitions if t["name"] != "agent"}


def test_available_types_excludes_builtin_collision(monkeypatch):
    import agents.subagent as subagent_mod
    custom = {"explore": {"name": "explore", "description": "evil",
                          "allowed_tools": None, "system_prompt": "p"},
              "myhelper": {"name": "myhelper", "description": "ok",
                           "allowed_tools": None, "system_prompt": "p"}}
    monkeypatch.setattr(subagent_mod, "_discover_custom_agents", _fake_custom(custom))
    names = [t["name"] for t in subagent_mod.get_available_agent_types()]
    assert names.count("explore") == 1
    assert "myhelper" in names


# ─── ⑥ 子代理继承父代理 permission_mode（不再默认 bypassPermissions）──


def test_agent_tool_subagent_inherits_permission_mode(monkeypatch):
    monkeypatch.setattr(agent_mod, "get_sub_agent_config",
                        lambda t: {"system_prompt": "s", "tools": []})
    for mode in ("default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"):
        agent = _parent(False)
        agent.permission_mode = mode
        captured: dict = {}
        _install_fake_subagent(monkeypatch, captured)
        asyncio.run(agent._execute_agent_tool(
            {"type": "general", "description": "d", "prompt": "p"}))
        assert captured["permission_mode"] == mode

    # 复现 finding：dontAsk 主代理父/子对同一危险命令均应 deny（旧实现子代理 bypass → allow）
    assert check_permission("run_shell", {"command": "rm -rf build"}, "dontAsk")["action"] == "deny"
    # default 档 run_shell 应 confirm
    assert check_permission("run_shell", {"command": "ls"}, "default")["action"] == "confirm"


def test_skill_fork_subagent_inherits_permission_mode(monkeypatch):
    monkeypatch.setattr(skills_mod, "execute_skill",
                        lambda name, args: {"context": "fork", "prompt": "p", "allowed_tools": None})
    monkeypatch.setattr(skill_tiers_mod, "fork_blocked_reason", lambda name: "")
    for mode in ("default", "dontAsk", "plan", "bypassPermissions"):
        agent = _parent(False)
        agent.permission_mode = mode
        captured: dict = {}
        _install_fake_subagent(monkeypatch, captured)
        asyncio.run(agent._execute_skill_tool({"skill_name": "x", "args": "a"}))
        assert captured["permission_mode"] == mode


# ─── ⑥b 子代理继承父代理 confirm_fn（confirm 走父通道，不退化 input()）──


def test_agent_tool_subagent_inherits_parent_confirm_fn(monkeypatch):
    monkeypatch.setattr(agent_mod, "get_sub_agent_config",
                        lambda t: {"system_prompt": "s", "tools": []})

    async def parent_confirm(message: str) -> bool:
        return True

    for use_openai in (False, True):
        agent = _parent(use_openai)
        agent.set_confirm_fn(parent_confirm)
        captured: dict = {}
        _install_fake_subagent(monkeypatch, captured)
        asyncio.run(agent._execute_agent_tool(
            {"type": "general", "description": "d", "prompt": "p"}))
        assert captured["confirm_fn"] is parent_confirm


def test_skill_fork_subagent_inherits_parent_confirm_fn(monkeypatch):
    monkeypatch.setattr(skills_mod, "execute_skill",
                        lambda name, args: {"context": "fork", "prompt": "p", "allowed_tools": None})
    monkeypatch.setattr(skill_tiers_mod, "fork_blocked_reason", lambda name: "")

    async def parent_confirm(message: str) -> bool:
        return True

    agent = _parent(False)
    agent.set_confirm_fn(parent_confirm)
    captured: dict = {}
    _install_fake_subagent(monkeypatch, captured)
    asyncio.run(agent._execute_skill_tool({"skill_name": "x", "args": "a"}))
    assert captured["confirm_fn"] is parent_confirm


def test_subagent_confirm_uses_inherited_parent_channel(monkeypatch):
    """子代理带父 confirm_fn 时 confirm 走父通道；input() EOF 不再静默拒绝。"""
    calls: list[str] = []

    async def parent_confirm(message: str) -> bool:
        calls.append(message)
        return True

    # 模拟继承后的子代理：permission_mode=acceptEdits 下 run_shell 一律 confirm。
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True,
                  permission_mode="acceptEdits", confirm_fn=parent_confirm)

    def _raise_eof(*args, **kwargs):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    assert asyncio.run(agent._confirm_dangerous("rm -rf build")) is True
    assert calls  # 命中父确认通道，未退化到 input()


# ─── ⑦ OpenAI：畸形 tool_call 补占位、不崩溃、不悬空 ──────────────


def test_openai_malformed_tool_call_paired_with_placeholder():
    agent = _make_openai_agent()
    responses = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "bad1", "type": "not_function"},                 # 非 function
            {"id": "bad2", "type": "function", "function": {}},     # 缺 name
        ]}}]},
        _resp_final("done"),
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        raise AssertionError("畸形 tool_call 不应被执行")

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_openai("go"))  # 旧实现：KeyError 崩溃 / continue 悬空

    call_ids = {tc["id"] for m in agent._openai_messages for tc in (m.get("tool_calls") or [])}
    tool_ids = {m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"}
    assert call_ids == {"bad1", "bad2"}
    assert tool_ids == call_ids  # 每个畸形 tool_call 都有占位 tool 回复，保持配对


# ─── ⑧ _normalize_anthropic_messages 清理孤儿 tool_result ─────────


def _has_tool_result_block(msgs: list[dict]) -> bool:
    return any(
        isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
        for m in msgs
    )


def test_normalize_anthropic_drops_orphan_tool_result():
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ghost", "content": "stale"},
        ]},
        {"role": "assistant", "content": "next"},
    ]
    out = agent._normalize_anthropic_messages(msgs)
    _assert_anthropic_pairing(out)
    assert not _has_tool_result_block(out)


def test_normalize_anthropic_keeps_text_sibling_of_orphan():
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": [
            {"type": "text", "text": "note"},
            {"type": "tool_result", "tool_use_id": "ghost", "content": "stale"},
        ]},
    ]
    out = agent._normalize_anthropic_messages(msgs)
    _assert_anthropic_pairing(out)
    assert out[-1]["content"] == [{"type": "text", "text": "note"}]


def test_openai_to_anthropic_migration_drops_orphan_tool_result():
    """跨协议迁移不应产出"只有 tool_result"的非法历史。"""
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)  # anthropic 路径
    openai_msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "ghost", "content": "stale"},
        {"role": "assistant", "content": "done"},
    ]
    out = agent._openai_messages_to_anthropic(openai_msgs)
    _assert_anthropic_pairing(out)
    assert not _has_tool_result_block(out)


# ─── ⑨ OpenAI 并发批传原始 raw 给失败判定 ────────────────────────


def test_openai_concurrent_batch_passes_raw_to_failure_check():
    agent = _make_openai_agent()
    captured: dict = {}
    orig = agent._looks_like_tool_failure

    def spy(name, raw, result):
        captured[name] = raw
        return orig(name, raw, result)

    agent._looks_like_tool_failure = spy
    responses = [
        _resp_tool_calls([
            ("call_a", "read_file", {"file_path": "f1"}),
            ("call_b", "read_file", {"file_path": "f2"}),
        ]),
        _resp_final("done"),
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        return f"RAW-{inp['file_path']}"

    def fake_persist(name, res):
        return "TRUNCATED-PREVIEW"

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    agent._persist_large_result = fake_persist
    asyncio.run(agent._chat_openai("go"))

    assert captured["read_file"] in {"RAW-f1", "RAW-f2"}  # 旧实现传 "" → 判定相反


# ─── ⑩ OpenAI：缺 id 的 tool_call 补稳定占位、不崩溃、不悬空 ──────


def test_openai_tool_call_missing_id_is_paired_not_crashed():
    agent = _make_openai_agent()
    executed: list[str] = []
    responses = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"type": "function",
             "function": {"name": "read_file", "arguments": "{\"file_path\": \"f1\"}"}},
            {"type": "function",
             "function": {"name": "run_shell", "arguments": "{\"command\": \"echo hi\"}"}},
        ]}}]},
        _resp_final("done"),
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        executed.append(name)
        return "RAW"

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_openai("go"))  # 旧实现：ct["tc"]["id"] 下标 → KeyError 崩溃整轮

    assert sorted(executed) == ["read_file", "run_shell"]  # 并发批 + 顺序批各一处，均不崩
    call_ids = [tc["id"] for m in agent._openai_messages for tc in (m.get("tool_calls") or [])]
    tool_ids = [m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"]
    assert len(call_ids) == 2 and all(call_ids)  # 缺 id 者已补稳定占位 id
    assert set(tool_ids) == set(call_ids)        # 逐对配对，无悬空


# ─── ⑪ 跨协议迁移保留全部真实 tool_result（不丢、不被占位覆盖）───


def _tool_result_contents(msgs: list[dict]) -> dict:
    return {
        b.get("tool_use_id"): b.get("content")
        for m in msgs if m.get("role") == "user" and isinstance(m.get("content"), list)
        for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
    }


def test_normalize_anthropic_keeps_all_real_tool_results():
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)  # anthropic 路径
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "id1", "name": "read_file", "input": {}},
            {"type": "tool_use", "id": "id2", "name": "read_file", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "id1", "content": "REAL-1"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "id2", "content": "REAL-2"},
        ]},
    ]
    out = agent._normalize_anthropic_messages(msgs)
    _assert_anthropic_pairing(out)
    # 旧实现：第 2 条真实结果被当孤儿删除，只剩合成占位 → 真实输出丢失
    assert _tool_result_contents(out) == {"id1": "REAL-1", "id2": "REAL-2"}


def test_openai_to_anthropic_migration_preserves_all_tool_results():
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)  # anthropic 路径
    openai_msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "id1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "id2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "id1", "content": "REAL-1"},
        {"role": "tool", "tool_call_id": "id2", "content": "REAL-2"},
    ]
    out = agent._openai_messages_to_anthropic(openai_msgs)
    _assert_anthropic_pairing(out)
    assert _tool_result_contents(out) == {"id1": "REAL-1", "id2": "REAL-2"}


# ─── ⑫ 未配对 assistant 只剔 tool_use、保留 text 块 ──────────────


def test_normalize_anthropic_unpaired_assistant_keeps_text_block():
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "reasoning正文"},
            {"type": "tool_use", "id": "u1", "name": "read_file", "input": {}},
        ]},
        {"role": "user", "content": "next turn"},
    ]
    out = agent._normalize_anthropic_messages(msgs)
    _assert_anthropic_pairing(out)
    # 旧实现：整条 assistant 丢弃，连带丢失正文/推理 text 块
    assert any(
        m.get("role") == "assistant" and isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "text" and b.get("text") == "reasoning正文"
                for b in m["content"])
        for m in out
    )
    assert all(
        not (m.get("role") == "assistant" and isinstance(m.get("content"), list)
             and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m["content"]))
        for m in out
    )


# ─── ⑬ 合并真实结果时剔除夹带的孤儿 tool_result ─────────────────


def test_normalize_anthropic_drops_orphan_tool_result_merged_with_real_results():
    """回归（R2 修复引入）：孤儿 tool_result 与其后合法结果被并进同一 user 消息保留 →
    产出非法 Anthropic 序列（API 400）。复现 msgs=[user hi, assistant[tool_use u1],
    user[tool_result u1], user[tool_result ghost], assistant next]。"""
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True)  # anthropic 路径
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "u1", "name": "read_file", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "REAL-1"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ghost", "content": "stale"},
        ]},
        {"role": "assistant", "content": "next"},
    ]
    out = agent._normalize_anthropic_messages(msgs)
    _assert_anthropic_pairing(out)
    # 真实结果保留，夹带的 ghost 被剔除（旧实现两者并进同一 user 消息保留）
    assert _tool_result_contents(out) == {"u1": "REAL-1"}


# ─── ⑭ REPL Ctrl+C 中止判据用运行期缓冲（main.py run_repl 回归）──


def test_chat_running_state_is_detected_by_turn_output_buffer_not_output_buffer():
    """回归：main.py run_repl 的 handle_sigint 判据曾用 agent._output_buffer（仅 run_once()
    置非 None）；REPL 走 chat() 只设 _turn_output_buffer → 判据恒假、Ctrl+C 无法中止。
    此处直接测 main.py 抽取出的运行期判据 _agent_is_processing。"""
    agent = _make_openai_agent()  # is_sub_agent=True：chat 尾部的打印/存档分支全部跳过
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_stream():
        started.set()
        await release.wait()
        return _resp_final("done")

    agent._call_openai_stream = fake_stream

    async def scenario():
        task = asyncio.create_task(agent.chat("go"))
        await started.wait()
        assert _agent_is_processing(agent) is True   # 运行期：新判据正确识别 → Ctrl+C 可中止
        assert agent._output_buffer is None          # 旧判据字段确为 None（修复前恒不中止）
        agent.abort()
        release.set()
        await task

    asyncio.run(scenario())
    assert _agent_is_processing(agent) is False      # 空闲态：不应误判为处理中


# ─── ⑮ OpenAI：工具抛异常不中断整轮（配对占位 + 复位运行期缓冲）───


def test_openai_tool_execution_error_is_paired_and_turn_completes():
    """回归：OpenAI 路径曾未像 Anthropic 给 _execute_tool_call 加 try 兜底。工具抛异常会
    冒泡出 chat()，导致 assistant(tool_calls) 无配对 role=tool（悬空 → 下次 400），且
    _turn_output_buffer 残留使 _agent_is_processing 误判仍在处理。"""
    agent = _make_openai_agent()
    responses = [
        # run_shell 非并发安全工具 → 走顺序批（非 gather 分支）。
        _resp_tool_calls([("call_run", "run_shell", {"command": "echo hi"})]),
        _resp_final("done"),
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        raise RuntimeError("boom")

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent.chat("go"))  # 旧实现：RuntimeError 冒泡，chat() 中断

    call_ids = {tc["id"] for m in agent._openai_messages for tc in (m.get("tool_calls") or [])}
    tool_ids = {m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"}
    assert call_ids == {"call_run"}
    assert tool_ids == call_ids                   # 有配对占位 tool 消息，无悬空
    assert _agent_is_processing(agent) is False   # buffer 已复位，不误判仍在处理


def test_openai_concurrent_batch_tool_error_does_not_abort_round():
    """并发批（asyncio.gather）中单个工具抛异常也不得整轮中断/悬空。"""
    agent = _make_openai_agent()
    executed: list[str] = []
    responses = [
        _resp_tool_calls([
            ("call_a", "read_file", {"file_path": "f1"}),
            ("call_b", "read_file", {"file_path": "f2"}),
        ]),
        _resp_final("done"),
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        executed.append(inp["file_path"])
        if inp["file_path"] == "f1":
            raise RuntimeError("concurrent boom")
        return "content"

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_openai("go"))  # 旧实现：gather 抛错，整轮中断

    assert sorted(executed) == ["f1", "f2"]        # 一个工具抛错不影响另一个
    tool_ids = {m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"}
    assert tool_ids == {"call_a", "call_b"}        # 每个 tool_call 都有配对消息



# ─── ⑯ Ctrl+C 取消：未执行的 tool_call / tool_use 也要补配对占位 ──


def _anthropic_tool_response(pairs):
    import types
    blocks = [
        types.SimpleNamespace(type="tool_use", id=uid, name=name, input=inp)
        for uid, name, inp in pairs
    ]
    return types.SimpleNamespace(
        content=blocks,
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def test_openai_abort_pairs_unexecuted_tool_calls():
    """回归：abort（Ctrl+C）置位后批量循环提前 break，已入 history 的 assistant(tool_calls)
    里后续未执行的 tool_call 缺 role=tool → 悬空持久化（--resume 下条请求 400）。"""
    agent = _make_openai_agent()
    executed: list[str] = []
    responses = [
        _resp_tool_calls([
            ("call_r", "read_file", {"file_path": "f1"}),
            ("call_s", "run_shell", {"command": "echo hi"}),
        ]),
        _resp_final("done"),
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        executed.append(name)
        agent._aborted = True  # 模拟 Ctrl+C 在并发安全工具执行期间到达
        return "content"

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_openai("go"))

    assert executed == ["read_file"]                     # abort 后不再执行 run_shell
    call_ids = {tc["id"] for m in agent._openai_messages for tc in (m.get("tool_calls") or [])}
    tool_ids = {m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"}
    assert tool_ids == call_ids == {"call_r", "call_s"}  # 未执行的 call_s 也有配对占位


def test_openai_abort_cancellation_pairs_unexecuted_tool_calls():
    """取消以 CancelledError（BaseException，不被 except Exception 兜住）从工具执行中抛出时，
    也要为未配对 tool_call 补占位，再让取消继续向上冒泡。"""
    agent = _make_openai_agent()
    executed: list[str] = []
    responses = [
        _resp_tool_calls([
            ("call_a", "run_shell", {"command": "a"}),
            ("call_b", "run_shell", {"command": "b"}),
        ]),
        _resp_final("done"),
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        executed.append(inp["command"])
        agent._aborted = True
        raise asyncio.CancelledError()

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    raised = False
    try:
        asyncio.run(agent._chat_openai("go"))
    except asyncio.CancelledError:
        raised = True
    assert raised

    assert executed == ["a"]                             # 第二个工具未执行
    call_ids = {tc["id"] for m in agent._openai_messages for tc in (m.get("tool_calls") or [])}
    tool_ids = {m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"}
    assert tool_ids == call_ids == {"call_a", "call_b"}


def test_anthropic_abort_pairs_unexecuted_tool_uses():
    """Anthropic 路径同理：abort 后未执行的 tool_use 必须补配对 tool_result。"""
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True,
                  permission_mode="bypassPermissions")
    executed: list[str] = []
    response = _anthropic_tool_response([
        ("u1", "read_file", {"file_path": "a"}),
        ("u2", "read_file", {"file_path": "b"}),
    ])

    async def fake_stream(on_tool_block_complete=None):
        return response

    async def fake_exec(name, inp):
        executed.append(inp["file_path"])
        agent._aborted = True
        return "content"

    agent._call_anthropic_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_anthropic("go"))

    assert executed == ["a"]
    _assert_anthropic_pairing(agent._anthropic_messages)
    assert {"u1", "u2"} <= set(_tool_result_contents(agent._anthropic_messages))


# ─── ⑰ arguments/input 解析为非 dict：不崩溃、不悬空 ──────────────


def test_openai_non_dict_arguments_no_crash_no_dangle():
    """回归：arguments='[1, 2]'（json.loads 得 list）→ print_tool_call→
    _get_tool_summary 调 inp.get 抛 AttributeError；该处在收尾 try/finally 之外，
    旧实现整轮崩溃且已入 history 的 assistant(tool_calls) 悬空（0 条 role=tool）。
    修复后归一为空 dict 并正常执行。"""
    agent = _make_openai_agent()
    responses = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "nd1", "type": "function",
             "function": {"name": "read_file", "arguments": "[1, 2]"}},
        ]}}]},
        _resp_final("done"),
    ]
    executed: list = []

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        executed.append((name, inp))
        return "content"

    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_openai("go"))  # 修复前：AttributeError 崩溃整轮

    call_ids = {tc["id"] for m in agent._openai_messages for tc in (m.get("tool_calls") or [])}
    tool_ids = {m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"}
    assert call_ids == {"nd1"}
    assert tool_ids == call_ids             # 配对，无悬空
    assert executed == [("read_file", {})]  # 归一为空 dict 后正常执行


def test_openai_display_path_failure_falls_back_to_placeholder(monkeypatch):
    """兜底：展示/权限路径抛异常时补占位 tool 消息，不崩溃、不悬空。"""
    agent = _make_openai_agent()
    responses = [
        _resp_tool_calls([("call_a", "read_file", {"file_path": "f1"})]),
        _resp_final("done"),
    ]

    async def fake_stream():
        return responses.pop(0)

    async def fake_exec(name, inp):
        raise AssertionError("展示路径异常时不应执行该工具")

    def boom(name, inp):
        raise AttributeError("boom")

    monkeypatch.setattr(agent_mod, "print_tool_call", boom)
    agent._call_openai_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_openai("go"))  # 修复前：AttributeError 冒泡出 chat()

    call_ids = {tc["id"] for m in agent._openai_messages for tc in (m.get("tool_calls") or [])}
    tool_ids = {m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"}
    assert call_ids == {"call_a"}
    assert tool_ids == call_ids


def test_anthropic_non_dict_input_no_crash_no_dangle():
    """Anthropic 路径对称：tool_use.input 非 dict（list）时归一为空 dict，
    不崩溃、不悬空。"""
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True,
                  permission_mode="bypassPermissions")
    executed: list = []
    responses = [
        _anthropic_tool_response([("u1", "read_file", [1, 2])]),
        _anthropic_tool_response([]),
    ]

    async def fake_stream(on_tool_block_complete=None):
        return responses.pop(0)

    async def fake_exec(name, inp):
        executed.append((name, inp))
        return "content"

    agent._call_anthropic_stream = fake_stream
    agent._execute_tool_call = fake_exec
    asyncio.run(agent._chat_anthropic("go"))  # 修复前：AttributeError 崩溃整轮

    _assert_anthropic_pairing(agent._anthropic_messages)
    assert {"u1"} <= set(_tool_result_contents(agent._anthropic_messages))
    assert executed == [("read_file", {})]
