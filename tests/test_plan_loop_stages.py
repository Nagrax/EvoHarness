"""Loop 阶段状态机（改造二）回归测试。

覆盖设计 testPlan ①-⑧：
① exit_plan_mode 审批断裂 P0 回归（缺 await / permission_mode 不迁移）+ 边界①双分支
② 状态机纯函数：迁移合法性、manifest 读写与写坏降级、plan_id 目录名安全
③ 交接生成：四字段 schema、坏 JSON fallback 保留确定性产物、边界①豁免 side_query
④ 70% 改道强制收段 + 复位断言（防分叉风暴）+ 非 staged 对照（现状路径不变）
⑤ advance_stage 同批判对：替换后无悬空 tool_use（Anthropic）/ tool_call（OpenAI）
⑥ 预算强制收段落点：break 前置标志 + chat() 收尾确定性收段（不调 side_query）
⑦ resume：边界定位 / 目录缺失降级 / 首段崩溃降级 / 权限不自动恢复（评审 #6）
⑧ 双协议结构化对照（剔除时间戳与会话 id 的投影，评审 #13 更正：不做逐字节断言）

零网络：side_query 全部用桩；阶段目录与全局自动存档重定向到 tmp_path。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.agent as agent_mod
import agents.session as session_mod
from agents.agent import Agent
from agents.main import _resume_staged_session
from agents.stages import (
    MANIFEST_FILENAME,
    is_valid_transition,
    load_manifest,
    new_manifest,
    write_manifest,
)

VALID_HANDOVER_JSON = json.dumps({
    "conclusions": [
        "auth module refactored and verified by tests/test_auth.py",
        "old endpoint /v1/login is refuted (removed), do not retry it",
    ],
    "artifacts": [{"path": "src/auth.py", "why": "rewritten module"}],
    "constraints": ["user requires Python 3.11"],
    "next_actions": [{"type": "tool_call", "description": "run pytest tests/test_auth.py"}],
})

# 大写字母避开 tmp_path 随机 hex 字符集，防路径撞子串。
ORPHAN_TOOL_ID = "TOOLUSEID_ABC123"


def _redirect_io(monkeypatch, tmp_path) -> Path:
    """阶段目录与全局自动存档全部重定向到 tmp_path，测试不留仓库/用户目录痕迹。"""
    stage_root = tmp_path / "proj" / ".evoharness" / "sessions"
    monkeypatch.setattr(session_mod, "get_project_session_dir", lambda: stage_root)
    monkeypatch.setattr(agent_mod, "save_session", lambda *a, **k: None)
    monkeypatch.setattr(agent_mod, "save_folded_session_memory", lambda *a, **k: None)
    return stage_root


def make_agent(monkeypatch, tmp_path, *, staged=True, use_openai=False, **kw) -> Agent:
    _redirect_io(monkeypatch, tmp_path)
    kwargs = dict(loop_stages=staged)
    if use_openai:
        kwargs.update(api_base="http://127.0.0.1:9/v1", api_key="test-key")
    kwargs.update(kw)
    return Agent(**kwargs)


def stub_side_query(agent: Agent, payload: str | None, calls: list, *, expect_max_tokens: int | None = None) -> None:
    """payload=None 表示该路径不得发起交接 side_query（max_tokens=4000 调用即测试失败）；
    无参调用（记忆预取等合法用途）放行并返回空串桩。"""
    if payload is None:
        def _forbidden(*, max_tokens: int = 256, label: str = ""):
            if max_tokens >= 4000:
                raise AssertionError("handover side_query must not be called in this path")

            async def _sq(system: str, user_message: str) -> str:
                calls.append({"system": system, "user": user_message, "non_handover": True})
                return ""

            return _sq

        agent._build_side_query = _forbidden
        return

    async def _sq(system: str, user_message: str) -> str:
        calls.append({"system": system, "user": user_message})
        return payload

    def _combined(*, max_tokens: int = 256, label: str = ""):
        calls.append({"max_tokens": max_tokens})
        if expect_max_tokens is not None:
            assert max_tokens == expect_max_tokens, f"handover side_query max_tokens={max_tokens}"
        return _sq

    agent._build_side_query = _combined


def project_manifest(manifest: dict) -> list[dict]:
    """评审 #13：剔除时间戳/session_id/token 数等不稳定字段的结构化投影。"""
    return [
        {
            "name": r.get("name"),
            "status": r.get("status"),
            "close_reason": r.get("close_reason"),
            "artifacts": r.get("artifacts"),
            "handover_keys": sorted((r.get("handover") or {}).keys()) if r.get("handover") else None,
        }
        for r in manifest.get("stages", [])
    ]


# ─── ① 边界①实驱动：P0 回归 + 双分支 ────────────────────────────


def test_exit_plan_mode_approval_is_awaited_and_drives_execute_segment(monkeypatch, tmp_path):
    """P0 回归（评审 #1）：修复前 async 审批桩未 await → AttributeError、审批体不执行、
    mode 停 plan；修复后审批体执行、permission_mode 真实迁移、execute 段开启。"""
    agent = make_agent(monkeypatch, tmp_path, permission_mode="plan")
    stub_side_query(agent, None, [])  # 边界①豁免：全程不得调 side_query
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# plan", encoding="utf-8")
    agent._plan_file_path = str(plan_file)
    calls: list = []

    async def approval(plan):
        calls.append(plan)
        return {"choice": "execute"}

    agent.set_plan_approval_fn(approval)
    asyncio.run(agent._execute_plan_mode_tool("exit_plan_mode"))
    assert calls == ["# plan"], "审批函数体必须真实执行（且收到计划正文而非文件路径）"
    assert agent.permission_mode == "acceptEdits", "choice=execute 后必须迁出 plan 档"
    manifest = load_manifest(agent._stage_plan_dir)
    assert [r["name"] for r in manifest["stages"]] == ["plan", "execute"]
    assert manifest["stages"][0]["status"] == "completed"
    assert manifest["stages"][0]["close_reason"] == "plan-approved:execute"
    assert manifest["stages"][1]["advisory_mode"] == "acceptEdits"  # 钩子在模式迁移后：记录真实执行档
    assert manifest["stages"][1]["status"] == "in_progress"
    assert manifest["stages"][1]["handover"] is not None
    # plan 段文件在清历史前已落盘（choice=execute 不清历史，此处验证快照机制本身）
    assert (Path(agent._stage_plan_dir) / manifest["stages"][0]["session_file"]).exists()


def test_exit_plan_mode_approval_receives_plan_body_not_path(monkeypatch, tmp_path):
    """评审 finding：审批面板/回显曾把 plan_content 赋为文件路径；修复后应为计划文件正文。"""
    agent = make_agent(monkeypatch, tmp_path, permission_mode="plan")
    stub_side_query(agent, None, [])
    body = "# Distinctive Plan Body\n\nstep 1: do the thing"
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(body, encoding="utf-8")
    agent._plan_file_path = str(plan_file)

    received: list = []

    async def approval(plan):
        received.append(plan)
        return {"choice": "execute"}

    agent.set_plan_approval_fn(approval)
    result = asyncio.run(agent._execute_plan_mode_tool("exit_plan_mode"))
    assert received == [body], "审批函数应收到计划正文，而非 str(plan_file)"
    assert body in result, "回显的 '## Approved Plan' 段落应含计划正文"


def test_exit_plan_mode_reads_non_utf8_plan_file_without_crashing(monkeypatch, tmp_path):
    """low 回归：非 UTF-8 计划文件读取抛错曾中断审批、permission_mode 滞留 plan 档；
    现读取加异常保护、退化为安全兜底文本，审批体仍执行、模式仍真实迁移。"""
    agent = make_agent(monkeypatch, tmp_path, permission_mode="plan")
    stub_side_query(agent, None, [])
    plan_file = tmp_path / "plan.md"
    plan_file.write_bytes(b"# plan\n\xff\xfe not utf-8")  # 非法 UTF-8 字节
    agent._plan_file_path = str(plan_file)

    received: list = []

    async def approval(plan):
        received.append(plan)
        return {"choice": "execute"}

    agent.set_plan_approval_fn(approval)
    result = asyncio.run(agent._execute_plan_mode_tool("exit_plan_mode"))
    assert len(received) == 1, "非 UTF-8 读取失败不得中断审批流程"
    assert "could not be read as UTF-8" in received[0], "应回退为安全兜底文本"
    assert agent.permission_mode == "acceptEdits", "审批通过后必须迁出 plan 档"
    assert "could not be read as UTF-8" in result


def test_approval_choice_clear_and_execute_also_transitions_and_opens_segment(monkeypatch, tmp_path):
    """choice=1：permission_mode 迁移（探针实证的原 bug）+ 历史清空 + execute 段开启。"""
    agent = make_agent(monkeypatch, tmp_path, permission_mode="plan")
    stub_side_query(agent, None, [])
    agent._plan_file_path = str(tmp_path / "p.md")
    agent._anthropic_messages = [{"role": "user", "content": "plan phase talk"}]

    async def approval(plan):
        return {"choice": "clear-and-execute"}

    agent.set_plan_approval_fn(approval)
    asyncio.run(agent._execute_plan_mode_tool("exit_plan_mode"))
    assert agent.permission_mode == "acceptEdits"
    assert agent._anthropic_messages == []  # 既有 clear-and-execute 行为不变
    manifest = load_manifest(agent._stage_plan_dir)
    assert [r["close_reason"] for r in manifest["stages"]] == ["plan-approved:clear-and-execute", "plan-approved:clear-and-execute"]
    assert manifest["stages"][1]["handover"] is not None


def test_exit_plan_mode_without_approval_fn_opens_execute_segment(monkeypatch, tmp_path):
    """评审 #2：一次性 --plan 走无审批回退分支，同样开 execute 段（豁免 side_query）。"""
    agent = make_agent(monkeypatch, tmp_path, permission_mode="plan")
    stub_side_query(agent, None, [])
    agent._plan_file_path = str(tmp_path / "p.md")
    asyncio.run(agent._execute_plan_mode_tool("exit_plan_mode"))
    assert agent.permission_mode == "default"  # 回退分支恢复原模式
    manifest = load_manifest(agent._stage_plan_dir)
    assert [r["name"] for r in manifest["stages"]] == ["plan", "execute"]
    assert manifest["stages"][0]["close_reason"] == "plan-approved:auto-exit"
    assert manifest["stages"][1]["handover"] is not None


def test_plan_mode_toggle_does_not_arm_staging(monkeypatch, tmp_path):
    """评审 #5：REPL /plan toggle 与 enter_plan_mode 只切权限档，不激活阶段机。"""
    agent = make_agent(monkeypatch, tmp_path, staged=False)
    agent.toggle_plan_mode()
    assert agent.permission_mode == "plan"
    assert agent._loop_stages_active is False
    assert agent._stage_manifest is None
    agent.toggle_plan_mode()
    assert agent._loop_stages_active is False


# ─── ② 状态机纯函数 ──────────────────────────────────────────────


def test_stage_transition_rules():
    assert is_valid_transition("plan", "execute")
    assert is_valid_transition("execute", "verify")
    assert is_valid_transition("verify", "deliver")
    assert not is_valid_transition("plan", "verify")      # 跳段
    assert not is_valid_transition("execute", "execute")  # 同段
    assert not is_valid_transition("deliver", "plan")     # 回退
    assert not is_valid_transition("verify", "bogus")     # 非法段名


def test_manifest_roundtrip_and_corruption_fallback(tmp_path):
    manifest = new_manifest("ab12cd34", str(tmp_path))
    write_manifest(tmp_path, manifest)
    loaded = load_manifest(tmp_path)
    assert loaded["plan_id"] == "ab12cd34"
    assert [r["name"] for r in loaded["stages"]] == ["plan"]
    # 写坏（截断 JSON）→ 降级 None，不抛异常（README 已知边界教训）
    (tmp_path / MANIFEST_FILENAME).write_text("{ truncated", encoding="utf-8")
    assert load_manifest(tmp_path) is None
    (tmp_path / MANIFEST_FILENAME).unlink()
    assert load_manifest(tmp_path) is None


def test_plan_id_is_windows_safe_hex(monkeypatch, tmp_path):
    """评审 #10：plan_id = session_id（uuid hex），目录名不含用户文本/非法字符。"""
    agent = make_agent(monkeypatch, tmp_path)
    assert agent.session_id and all(c in "0123456789abcdef" for c in agent.session_id)
    manifest = load_manifest(agent._stage_plan_dir)
    assert manifest["plan_id"] == agent.session_id
    assert Path(agent._stage_plan_dir).name == agent.session_id


# ─── ③ 交接生成 ──────────────────────────────────────────────────


def test_handover_fallback_keeps_deterministic_artifacts(monkeypatch, tmp_path):
    agent = make_agent(monkeypatch, tmp_path)  # default 档 → 起始段 execute
    calls: list = []
    stub_side_query(agent, "this is not json", calls)
    agent._note_artifact("reports/scan.txt", "dump")
    result = asyncio.run(agent._execute_advance_stage_tool({"stage": "verify", "summary": "done"}))
    assert "now in stage 'verify'" in result
    manifest = load_manifest(agent._stage_plan_dir)
    handover = manifest["stages"][1]["handover"]
    # 模型 summary 保留在首位，fallback 标记在后（交接失败不丢模型结论）。
    assert handover["conclusions"][0] == "done"
    assert any("failed" in str(c) for c in handover["conclusions"])
    paths = [a["path"] for a in manifest["stages"][0]["artifacts"]]
    assert "reports/scan.txt" in paths  # 确定性产物不因 LLM 失败而丢
    assert calls and calls[0]["max_tokens"] == 4000  # 设计定值（折叠路径为 6000）


# ─── ④ 70% 改道收段 + 复位 + 非 staged 对照 ─────────────────────


def test_threshold_forces_split_and_resets_counters(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOHARNESS_CONTEXT_WINDOW", "40000")  # effective_window = 20000
    agent = make_agent(monkeypatch, tmp_path)
    calls: list = []
    stub_side_query(agent, VALID_HANDOVER_JSON, calls)
    agent.last_input_token_count = int(agent.effective_window * 0.7) + 10
    agent._tool_error_streak = 5
    asyncio.run(agent._check_and_compact())
    manifest = load_manifest(agent._stage_plan_dir)
    assert [r["name"] for r in manifest["stages"]] == ["execute", "execute"]
    assert [r["status"] for r in manifest["stages"]] == ["split", "in_progress"]
    assert manifest["stages"][0]["close_reason"] == "threshold:70%"
    assert manifest["stages"][1]["handover"] is not None
    # 复位断言（评审 #3）：不复位则段内剩余每轮再触发收段（分叉风暴）。
    assert agent.last_input_token_count == 0
    assert agent._tool_error_streak == 0
    assert agent._same_tool_repeat_count == 0
    # 第二次调用为 no-op：分叉风暴回归证明。
    asyncio.run(agent._check_and_compact())
    assert len(load_manifest(agent._stage_plan_dir)["stages"]) == 2


def test_non_staged_auto_compact_keeps_existing_fold_path(monkeypatch, tmp_path):
    """对照：非 staged 走既有 trigger=auto 裸折叠路径，结构不变。"""
    agent = make_agent(monkeypatch, tmp_path, staged=False)
    triggers: list = []

    async def fake_compact(*, trigger):
        triggers.append(trigger)
        return True

    agent._compact_conversation = fake_compact
    agent.last_input_token_count = int(agent.effective_window * 0.7) + 10
    asyncio.run(agent._check_and_compact())
    assert triggers == ["auto"]
    assert agent._stage_manifest is None


# ─── ⑤ advance_stage 同批判对 + 双协议 ──────────────────────────


def _seed_history(agent: Agent) -> None:
    if agent.use_openai:
        agent._openai_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "refactor auth"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": ORPHAN_TOOL_ID, "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"file_path\": \"a.py\"}"}},
            ]},
            {"role": "tool", "tool_call_id": ORPHAN_TOOL_ID, "content": "contents"},
        ]
    else:
        agent._anthropic_messages = [
            {"role": "user", "content": "refactor auth"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": ORPHAN_TOOL_ID, "name": "read_file", "input": {"file_path": "a.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": ORPHAN_TOOL_ID, "content": "contents"},
            ]},
        ]


def test_advance_stage_anthropic_replaces_history_without_orphans(monkeypatch, tmp_path):
    agent = make_agent(monkeypatch, tmp_path)  # anthropic 路径
    calls: list = []
    stub_side_query(agent, VALID_HANDOVER_JSON, calls, expect_max_tokens=4000)
    _seed_history(agent)
    agent._note_artifact("src/auth.py", "written")
    agent.last_input_token_count = 12345
    agent._tool_error_streak = 3
    result = asyncio.run(agent._execute_advance_stage_tool(
        {"stage": "verify", "summary": "execute done", "artifacts": [{"path": "src/other.py", "why": "touched"}]}
    ))
    assert agent._context_cleared is True  # 走既有通道，批内剩余工具会 break
    assert "now in stage 'verify'" in result
    assert len(agent._anthropic_messages) == 1
    assert agent._anthropic_messages[0]["role"] == "user"
    content = agent._anthropic_messages[0]["content"]
    assert "<stage-handover>" in content and "refuted" in content
    assert ORPHAN_TOOL_ID not in content  # 旧 tool_use id 不残留（无悬空）
    # 复位（评审 #3）
    assert agent.last_input_token_count == 0
    assert agent._tool_error_streak == 0
    # manifest：模型声明产物 + 确定性采集合并去重
    manifest = load_manifest(agent._stage_plan_dir)
    paths = [a["path"] for a in manifest["stages"][0]["artifacts"]]
    assert "src/auth.py" in paths and "src/other.py" in paths
    assert manifest["stages"][0]["folds"] == 0
    assert manifest["stages"][1]["name"] == "verify"
    assert (Path(agent._stage_plan_dir) / manifest["stages"][0]["session_file"]).exists()


def test_advance_stage_openai_keeps_system_and_drops_orphan_tool_calls(monkeypatch, tmp_path):
    agent = make_agent(monkeypatch, tmp_path, use_openai=True)
    calls: list = []
    stub_side_query(agent, VALID_HANDOVER_JSON, calls)
    _seed_history(agent)
    result = asyncio.run(agent._execute_advance_stage_tool({"stage": "verify", "summary": "done"}))
    assert agent._context_cleared is True
    assert "now in stage 'verify'" in result
    assert len(agent._openai_messages) == 2
    assert agent._openai_messages[0]["role"] == "system"  # system 首消息保留
    assert "<stage-handover>" in agent._openai_messages[1]["content"]
    call_ids = {tc["id"] for m in agent._openai_messages for tc in (m.get("tool_calls") or [])}
    tool_ids = {m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"}
    assert not call_ids and not tool_ids  # 无悬空 tool_call / tool 消息


def test_dual_protocol_structured_parity(monkeypatch, tmp_path):
    """⑧ 结构化对照：同一触发序列，两协议 manifest 投影一致（评审 #13：不做逐字节断言）。"""
    projections = []
    for use_openai in (False, True):
        agent = make_agent(monkeypatch, tmp_path, use_openai=use_openai)
        calls: list = []
        stub_side_query(agent, VALID_HANDOVER_JSON, calls)
        _seed_history(agent)
        agent._note_artifact("src/auth.py", "written")
        asyncio.run(agent._execute_advance_stage_tool({"stage": "verify", "summary": "done"}))
        projections.append(project_manifest(load_manifest(agent._stage_plan_dir)))
    assert projections[0] == projections[1]


def test_advance_stage_rejects_invalid_transitions(monkeypatch, tmp_path):
    agent = make_agent(monkeypatch, tmp_path, permission_mode="plan")  # 起始段 plan
    r = asyncio.run(agent._execute_advance_stage_tool({"stage": "execute", "summary": "x"}))
    assert "error" in r and "exit_plan_mode" in r

    agent2 = make_agent(monkeypatch, tmp_path)
    calls: list = []
    stub_side_query(agent2, VALID_HANDOVER_JSON, calls)
    asyncio.run(agent2._execute_advance_stage_tool({"stage": "verify", "summary": "ok"}))
    back = asyncio.run(agent2._execute_advance_stage_tool({"stage": "execute", "summary": "back"}))
    assert "error" in back and "expected 'deliver'" in back
    fwd = asyncio.run(agent2._execute_advance_stage_tool({"stage": "deliver", "summary": "done"}))
    assert "now in stage 'deliver'" in fwd
    terminal = asyncio.run(agent2._execute_advance_stage_tool({"stage": "deliver", "summary": "again"}))
    assert "error" in terminal and "final stage" in terminal

    agent3 = make_agent(monkeypatch, tmp_path, staged=False)
    inactive = asyncio.run(agent3._execute_advance_stage_tool({"stage": "verify", "summary": "x"}))
    assert "error" in inactive and "not active" in inactive


# ─── ⑥ 预算强制收段落点 ──────────────────────────────────────────


def test_budget_stop_closes_segment_at_chat_tail_without_side_query(monkeypatch, tmp_path):
    agent = make_agent(monkeypatch, tmp_path)
    stub_side_query(agent, None, [])  # 预算已是约束，收段不得再花：不调 side_query
    agent._stage_close_reason = "budget:Turn limit reached (1 >= 1)"
    agent._finalize_staged_chat()
    manifest = load_manifest(agent._stage_plan_dir)
    assert [r["status"] for r in manifest["stages"]] == ["split", "in_progress"]
    assert manifest["stages"][0]["close_reason"] == "budget:Turn limit reached (1 >= 1)"
    assert manifest["stages"][-1]["handover"] is None  # 续段无交接（resume 时回退最近边界）
    assert manifest["stages"][0]["close_reason"].startswith("budget:")
    assert agent._stage_close_reason == ""


def test_budget_stop_wired_from_main_loop_break(monkeypatch, tmp_path):
    """⑥ 端到端：max_turns=1 + 单工具响应 → 预算 break 置标志 → chat() 收尾收段。"""
    monkeypatch.setenv("EVOHARNESS_AUTO_SKILL_EVOLUTION", "0")
    monkeypatch.setattr(agent_mod, "start_memory_prefetch", lambda *a, **k: None)
    agent = make_agent(monkeypatch, tmp_path, max_turns=1)
    # 打桩 MCP 懒加载：chat() 首轮会连真实 MCP server（context7/playwright/github），测试不出网。
    async def _no_mcp():
        return None
    monkeypatch.setattr(agent._mcp_manager, "load_and_connect", _no_mcp)
    stub_side_query(agent, None, [])

    class _Usage:
        input_tokens = 10
        output_tokens = 5

    class _Block:
        type = "tool_use"
        id = ORPHAN_TOOL_ID
        name = "read_file"
        input = {"file_path": "x.py"}

    class _Resp:
        content = [_Block()]
        usage = _Usage()

    async def fake_stream(*, on_tool_block_complete=None):
        return _Resp()

    async def fake_exec(name, inp):
        return "ok"

    agent._call_anthropic_stream = fake_stream
    agent._execute_tool_call = fake_exec
    agent.permission_mode = "bypassPermissions"
    asyncio.run(agent.chat("fix the thing"))
    manifest = load_manifest(agent._stage_plan_dir)
    assert manifest["stages"][0]["status"] == "split"
    assert manifest["stages"][0]["close_reason"].startswith("budget:")
    assert manifest["stages"][-1]["name"] == "execute"


# ─── ⑦ resume ────────────────────────────────────────────────────


def test_resume_staged_session_restores_last_boundary(monkeypatch, tmp_path):
    agent = make_agent(monkeypatch, tmp_path)
    calls: list = []
    stub_side_query(agent, VALID_HANDOVER_JSON, calls)
    asyncio.run(agent._execute_advance_stage_tool({"stage": "verify", "summary": "done"}))
    metadata = {"loop_staged": True, "plan_dir_abs": str(agent._stage_plan_dir)}
    fresh = Agent()  # 非 staged、default 档
    assert _resume_staged_session(fresh, metadata) is True
    msgs = fresh._anthropic_messages
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    assert "<stage-handover>" in msgs[0]["content"]
    assert "verify" in msgs[0]["content"]
    assert fresh.permission_mode == "default"


def test_resume_does_not_restore_permission_mode(monkeypatch, tmp_path):
    """评审 #6：manifest 的档只是 advisory；resume 后有效权限一律 default。"""
    agent = make_agent(monkeypatch, tmp_path, permission_mode="plan")
    plan_file = tmp_path / "p.md"
    plan_file.write_text("# plan", encoding="utf-8")
    agent._plan_file_path = str(plan_file)

    async def approval(plan):
        return {"choice": "execute"}  # 用户当时明示审批 → acceptEdits

    agent.set_plan_approval_fn(approval)
    asyncio.run(agent._execute_plan_mode_tool("exit_plan_mode"))
    assert agent.permission_mode == "acceptEdits"
    metadata = {"loop_staged": True, "plan_dir_abs": str(agent._stage_plan_dir)}
    fresh = Agent()
    assert _resume_staged_session(fresh, metadata) is True
    assert fresh.permission_mode == "default"  # 不从文件恢复提权


def test_resume_falls_back_when_plan_dir_missing(monkeypatch, tmp_path):
    assert _resume_staged_session(Agent(), {"loop_staged": True, "plan_dir_abs": str(tmp_path / "nope")}) is False


def test_resume_falls_back_when_no_boundary_yet(monkeypatch, tmp_path):
    """评审 #9：首段（plan）内崩溃、无任何 completed 段 → 回退全局原始存档。"""
    agent = make_agent(monkeypatch, tmp_path, permission_mode="plan")
    assert _resume_staged_session(Agent(), {"loop_staged": True, "plan_dir_abs": str(agent._stage_plan_dir)}) is False


def test_resume_falls_back_when_only_budget_continuations_exist(monkeypatch, tmp_path):
    agent = make_agent(monkeypatch, tmp_path)
    agent._stage_close_reason = "budget:Turn limit reached (1 >= 1)"
    agent._finalize_staged_chat()  # 续段无 handover
    metadata = {"loop_staged": True, "plan_dir_abs": str(agent._stage_plan_dir)}
    # 无任何 handover → False（回退原始存档），而不是恢复到空状态。
    assert _resume_staged_session(Agent(), metadata) is False


# ─── 落盘与工具注册 ──────────────────────────────────────────────


def test_auto_save_metadata_marks_staged_session(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_save(session_id, data):
        captured.update(data)

    monkeypatch.setattr(agent_mod, "save_session", fake_save)
    stage_root = tmp_path / "proj" / ".evoharness" / "sessions"
    monkeypatch.setattr(session_mod, "get_project_session_dir", lambda: stage_root)
    agent = Agent(permission_mode="plan", loop_stages=True)
    agent._auto_save()
    meta = captured["metadata"]
    assert meta["loop_staged"] is True
    assert meta["plan_id"] == agent.session_id
    assert Path(meta["plan_dir_abs"]).is_absolute()

    # 非 staged 不带标记（普通会话语据不变）
    captured.clear()
    plain = Agent()
    plain._auto_save()
    assert "loop_staged" not in captured["metadata"]


def test_advance_stage_registered_but_not_safe_or_read():
    from agents.tools import CONCURRENCY_SAFE_TOOLS, READ_TOOLS, check_permission, tool_definitions

    names = {t["name"] for t in tool_definitions}
    assert "advance_stage" in names
    assert "advance_stage" not in CONCURRENCY_SAFE_TOOLS  # 不流式提前执行、不并发分批
    assert "advance_stage" not in READ_TOOLS
    for mode in ("default", "plan", "acceptEdits", "bypassPermissions", "dontAsk"):
        res = check_permission("advance_stage", {"stage": "verify", "summary": "s"}, mode, None)
        assert res["action"] == "allow", (mode, res)


def test_artifact_capture_from_write_tools(monkeypatch, tmp_path):
    agent = make_agent(monkeypatch, tmp_path)
    agent._note_artifact("a.py", "write_file")
    agent._note_artifact("a.py", "dup")  # 同路径去重
    arts = agent._deterministic_artifacts()
    assert [a["path"] for a in arts] == ["a.py"]
    assert arts[0]["why"] == "write_file"


# ─── 跨协议 resume：协议标记 → 迁移进当前协议列表（修复静默丢历史） ──


OPENAI_BASE = "http://127.0.0.1:9/v1"


def test_auto_save_metadata_records_protocol(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_save(session_id, data):
        captured.update(data)

    monkeypatch.setattr(agent_mod, "save_session", fake_save)
    Agent()._auto_save()
    assert captured["metadata"]["protocol"] == "anthropic"
    captured.clear()
    Agent(api_base=OPENAI_BASE, api_key="test-key")._auto_save()
    assert captured["metadata"]["protocol"] == "openai"


def test_restore_session_migrates_openai_history_into_anthropic(monkeypatch, tmp_path):
    _redirect_io(monkeypatch, tmp_path)
    payload = {
        "protocol": "openai",
        "openaiMessages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    }
    anth = Agent()
    anth.restore_session(payload)
    assert [m["role"] for m in anth._anthropic_messages] == ["user", "assistant"]
    assert [m["content"] for m in anth._anthropic_messages] == ["hi", "hello"]
    assert anth._openai_messages == []  # 不再误塞进当前不用的列表


def test_restore_session_migrates_anthropic_history_into_openai(monkeypatch, tmp_path):
    _redirect_io(monkeypatch, tmp_path)
    payload = {
        "protocol": "anthropic",
        "anthropicMessages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
        ],
    }
    oa = Agent(api_base=OPENAI_BASE, api_key="test-key")
    oa.restore_session(payload)
    msgs = oa._openai_messages
    assert msgs[0]["role"] == "system"  # 系统消息保留在首位
    assert msgs[1:] == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]


def test_restore_session_infers_protocol_without_marker(monkeypatch, tmp_path):
    _redirect_io(monkeypatch, tmp_path)
    # 无标记、只有 openaiMessages：按 payload 推断来源协议并迁移（旧会话兼容）
    anth = Agent()
    anth.restore_session({"openaiMessages": [{"role": "user", "content": "legacy"}]})
    assert anth._anthropic_messages == [{"role": "user", "content": "legacy"}]
    # 无标记、只有 anthropicMessages 且当前也是 anthropic：原路径不变
    anth2 = Agent()
    anth2.restore_session({"anthropicMessages": [{"role": "user", "content": "same"}]})
    assert anth2._anthropic_messages == [{"role": "user", "content": "same"}]
