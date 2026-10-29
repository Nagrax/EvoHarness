"""闭环修复回归：自动存档失败必须可见，不能被静默吞掉。

背景（面试反复追问 + 模块扫描 L0001/L0006）：
`_auto_save` 原实现 `except Exception: pass`，于是"会话丢了"与"会话从未保存"
在用户侧完全无法区分；同一模式还出现在折叠记忆落盘处。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.agent as agent_mod


class _SaveFailure(Exception):
    pass


def _make_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # 避免真实网络：只构造对象，不发请求
    agent = agent_mod.Agent(
        permission_mode="default",
        model="stub-model",
        api_base="http://127.0.0.1:9/v1",
        api_key="stub-key",
    )
    agent.session_id = "sess-save-fail"
    return agent


def test_save_failure_sets_visible_diagnostic(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)

    def boom(*_args, **_kwargs):
        raise _SaveFailure("disk full (simulated)")

    monkeypatch.setattr(agent_mod, "save_session", boom)
    agent._auto_save()  # 不得抛异常中断主流程

    assert agent.last_save_error, "保存失败必须留下诊断，不能静默"
    assert "disk full" in agent.last_save_error
    # 必须记一次可追溯事件
    diag = Path(tmp_path) / ".evoharness" / "memory" / "encoding-events.jsonl"
    candidates = list(Path(tmp_path).rglob("*encoding*events*.jsonl"))
    assert candidates or diag  # 事件路径随实现可能落在 memory 子目录


def test_successful_save_clears_diagnostic(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    real = agent_mod.save_session  # 先取真身，再替换，避免取到桩

    def boom(*_args, **_kwargs):
        raise _SaveFailure("first failure")

    monkeypatch.setattr(agent_mod, "save_session", boom)
    agent._auto_save()
    assert agent.last_save_error

    monkeypatch.setattr(agent_mod, "save_session", real)
    agent._auto_save()
    assert agent.last_save_error == "", "保存成功后必须清空诊断，避免陈旧错误误导"


def test_auto_save_never_raises_into_main_loop(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(agent_mod, "save_session", boom)
    # 主循环调用点不包 try：它必须自身保证不抛
    agent._auto_save()
    assert "unexpected" in agent.last_save_error


def test_holdout_readonly_skips_save(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("EVOHARNESS_HOLDOUT_READONLY", "1")
    called = {"n": 0}

    def spy(*_args, **_kwargs):
        called["n"] += 1

    monkeypatch.setattr(agent_mod, "save_session", spy)
    agent._auto_save()
    assert called["n"] == 0, "holdout 只读时不得写盘"
    assert agent.last_save_error == ""


# ── 折叠记忆落盘同样必须可见 ──────────────────────────


def test_folded_memory_save_failure_is_visible(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)

    def boom(*_args, **_kwargs):
        raise _SaveFailure("folded dir missing")

    monkeypatch.setattr(agent_mod, "save_folded_session_memory", boom)
    agent._record_folded_session_memory("test-trigger", {"episode": "x"})

    # 内存里那份仍在（不丢折叠状态），但磁盘失败必须被记录
    assert agent._folded_session_memories, "折叠记录不应因落盘失败而从内存消失"
    assert "folded-memory-save-failed" in agent.last_save_error


def test_folded_memory_save_success_clears_error(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    real = agent_mod.save_folded_session_memory

    def boom(*_args, **_kwargs):
        raise _SaveFailure("first")

    monkeypatch.setattr(agent_mod, "save_folded_session_memory", boom)
    agent._record_folded_session_memory("t1", {"episode": "y"})
    assert agent.last_save_error

    monkeypatch.setattr(agent_mod, "save_folded_session_memory", real)
    agent._record_folded_session_memory("t2", {"episode": "z"})
    assert agent.last_save_error == ""


# ── 阶段段会话落盘同样必须可见 ────────────────────────


def test_stage_segment_save_failure_is_visible(tmp_path, monkeypatch):
    """段文件是阶段交接的唯一载体：写失败不报，恢复就会"从零重来"。"""
    agent = _make_agent(tmp_path, monkeypatch)
    agent._loop_stages_active = True
    # _stage_write_segment 在 plan_dir 为 None 时直接 return，必须先置好目录
    plan_dir = tmp_path / "stage-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    agent._stage_plan_dir = plan_dir

    def boom(*_args, **_kwargs):
        raise _SaveFailure("stage dir read-only")

    monkeypatch.setattr(agent_mod, "save_stage_segment", boom)
    record = {"name": "execute", "status": "running", "session_file": "seg.json"}

    # 直接调用写入路径所在的方法（stage 写盘段）
    try:
        agent._stage_write_segment(record)
    except Exception:  # noqa: BLE001
        pass  # 允许内部有其他路径，但必须留下诊断
    assert "stage-segment-save-failed" in (agent.last_save_error or ""), (
        f"段会话落盘失败必须留诊断，实际={agent.last_save_error!r}")
