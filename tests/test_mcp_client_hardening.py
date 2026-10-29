"""MCP 客户端加固回归（纯本地，零网络 / 零模型）。

覆盖 2 条 finding：
1. `McpConnection.close()` 关闭 stdin/stdout/stderr 管道，消除 Windows
   ProactorEventLoop 下的 `ResourceWarning: unclosed transport` 与
   `ValueError: I/O operation on closed pipe`，且调用幂等。
2. `check_permission` 对 `.mcp.json` / `.evoharness/settings.json` 的写入不得被
   settings 的 allow 规则自动放行（持久化代码执行 / 放行任意工具）。
"""

from __future__ import annotations

import asyncio
import gc
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.tools as tools_mod
from agents.mcp_client import McpConnection
from agents.tools import check_permission


# ─── finding 1：close() 关闭 stdio 管道 ────────────────────────────

def _run_close_scenario() -> tuple[list, dict]:
    """在当前进程内跑一次 connect→close，返回 (捕获的 warning, 关闭后的状态)。"""
    state: dict = {}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        async def scenario() -> None:
            conn = McpConnection(
                "regression",
                sys.executable,
                ["-c", "import time; time.sleep(30)"],
            )
            await conn.connect()
            assert conn._process is not None
            proc = conn._process
            # close 前抓取管道引用，close 后用于断言其传输层已关闭。
            state["writer"] = proc.stdin
            state["stdout_transport"] = proc.stdout._transport
            state["stderr_transport"] = proc.stderr._transport
            conn.close()
            # 幂等：重复 close 不应抛异常。
            conn.close()
            state["proc"] = proc
            state["process_null"] = conn._process
            state["reader_null"] = conn._reader_task
            # 等待子进程被回收，让 master 传输层完成自清理。
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

        asyncio.run(scenario())
        # 触发 transport.__del__，暴露未关闭的传输层。
        gc.collect()

    return caught, state


def test_close_releases_stdio_pipes():
    caught, state = _run_close_scenario()
    proc = state["proc"]

    # 幂等且引用已置空。
    assert state["process_null"] is None
    assert state["reader_null"] is None
    # stdin/stdout/stderr 管道对象已显式关闭并置空（修复前 kill 后仍保留未关闭管道）。
    assert proc.stdin is None and proc.stdout is None and proc.stderr is None
    assert state["writer"].is_closing()
    assert state["stdout_transport"].is_closing()
    assert state["stderr_transport"].is_closing()

    resource_warnings = [
        w for w in caught if issubclass(w.category, ResourceWarning)
    ]
    assert not resource_warnings, [str(w.message) for w in resource_warnings]


# ─── finding 2：敏感配置写入守卫 ──────────────────────────────────

def _prepare_workspace(monkeypatch, tmp_path: Path) -> None:
    """搭建工作区：存在 allow 规则的 settings + 已存在的配置文件。"""
    monkeypatch.chdir(tmp_path)
    # 权限规则是进程级缓存，测试前必须重置，避免受其它用例影响。
    monkeypatch.setattr(tools_mod, "_cached_rules", None)

    (tmp_path / ".mcp.json").write_text("{}", encoding="utf-8")
    (tmp_path / "normal.txt").write_text("hi", encoding="utf-8")
    settings_dir = tmp_path / ".evoharness"
    settings_dir.mkdir(exist_ok=True)
    (settings_dir / "settings.json").write_text(
        '{"permissions": {"allow": ["write_file", "edit_file"]}}',
        encoding="utf-8",
    )


def test_protected_config_write_not_auto_allowed_by_rules(monkeypatch, tmp_path):
    _prepare_workspace(monkeypatch, tmp_path)

    # allow 规则存在，但目标是非敏感文件时仍按既有逻辑放行（未被误伤）。
    normal = check_permission("write_file", {"file_path": "normal.txt", "content": "x"}, "acceptEdits")
    assert normal["action"] == "allow"

    # `.mcp.json` 即使命中 allow 规则，也必须降级为 confirm（不得自动放行）。
    mcp_json = check_permission("write_file", {"file_path": ".mcp.json", "content": "{}"}, "default")
    assert mcp_json["action"] == "confirm"

    # `.evoharness/settings.json` 同样不得被 allow 规则自动放行。
    proj_settings = check_permission(
        "edit_file",
        {"file_path": ".evoharness/settings.json", "old_string": "{}", "new_string": "{}"},
        "acceptEdits",
    )
    assert proj_settings["action"] == "confirm"

    # Windows 反斜杠变体也要命中守卫。
    backslash = check_permission(
        "write_file", {"file_path": ".evoharness\\settings.json", "content": "{}"}, "default"
    )
    assert backslash["action"] == "confirm"


def test_protected_config_write_denied_in_hard_gate_modes(monkeypatch, tmp_path):
    _prepare_workspace(monkeypatch, tmp_path)

    dont_ask = check_permission("write_file", {"file_path": ".mcp.json", "content": "{}"}, "dontAsk")
    assert dont_ask["action"] == "deny"

    plan = check_permission("write_file", {"file_path": ".mcp.json", "content": "{}"}, "plan")
    assert plan["action"] == "deny"
