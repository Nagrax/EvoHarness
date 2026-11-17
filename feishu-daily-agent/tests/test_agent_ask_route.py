"""L9049：「问：」前缀路由到 EvoHarness Agent（桥接降级语义）。

回归点：
1. 桥开启且可达 → 命中问：前缀的消息产生 agent_ask 草稿（内容=桥答案），
   不走经验匹配（note 含 agent_ask，experience_id 为空）
2. 桥开启但不可达（fetch_answer 返回 None）→ 优雅回落经验库流程
3. 桥关闭 → 前缀消息走普通流程，绝不触网
4. 前缀解析边界：去前缀、无内容返回 None、非前缀不受影响
"""
from __future__ import annotations

from app.core.database import session_scope
from app.core.config import get_settings
from app.models import DraftStatus
from app.services import agent_bridge, reply_engine
from tests.helpers import incoming


def _seed_experience_if_any():
    """本文件不造经验：回落路径走 no-experience 草稿即可。"""


def test_bridge_disabled_never_touches_network(source, settings, monkeypatch):
    monkeypatch.setattr(settings, "evoharness_ask_enabled", False)
    called = {"n": 0}

    def fake_fetch(question):
        called["n"] += 1
        return "不应被调用"

    monkeypatch.setattr(agent_bridge, "fetch_answer", fake_fetch)
    with session_scope() as session:
        draft = reply_engine.process_incoming(
            session, incoming("问：这是什么", "om_ask_off"), source=source
        )
    assert called["n"] == 0
    assert draft is not None  # 普通 no-experience 草稿
    assert draft.draft_content != "不应被调用"


def test_bridge_answer_becomes_agent_ask_draft(source, settings, monkeypatch):
    monkeypatch.setattr(settings, "evoharness_ask_enabled", True)
    monkeypatch.setattr(agent_bridge, "fetch_answer", lambda q: "这是 EvoHarness 的回答")
    with session_scope() as session:
        draft = reply_engine.process_incoming(
            session, incoming("问：解释一下这个函数", "om_ask_on"), source=source
        )
        assert draft is not None
        assert draft.draft_content == "这是 EvoHarness 的回答"
        assert draft.status == DraftStatus.PENDING
        assert draft.mode == "draft"
        assert "agent_ask" in (draft.note or "")
        assert draft.experience_id is None


def test_bridge_unavailable_falls_back_to_experience_flow(source, settings, monkeypatch):
    monkeypatch.setattr(settings, "evoharness_ask_enabled", True)
    monkeypatch.setattr(agent_bridge, "fetch_answer", lambda q: None)
    with session_scope() as session:
        draft = reply_engine.process_incoming(
            session, incoming("问：年假怎么申请", "om_ask_fallback"), source=source
        )
        assert draft is not None
        assert "agent_ask" not in (draft.note or "")
        assert draft.draft_content != "这是 EvoHarness 的回答"


def test_strip_ask_prefix_edges():
    assert agent_bridge.strip_ask_prefix("问：你好") == "你好"
    assert agent_bridge.strip_ask_prefix("问:half width") == "half width"
    assert agent_bridge.strip_ask_prefix("问：   ") is None
    assert agent_bridge.strip_ask_prefix("普通消息 问：不在开头") is None
    assert agent_bridge.strip_ask_prefix("") is None


def test_enabled_defaults_off_in_offline(settings):
    # offline 模式且未显式开启时，桥必须默认关闭（测试环境零触网）
    assert agent_bridge.enabled() is False or settings.evoharness_ask_enabled is True
