"""owner 身份学习：只配 chat_id 时也能让卡片审批/指令校验通过。

背景（2026-11-10 实机接入）：p2p chat_id 足以投递日报，但一次性审批令牌与
owner 指令都按 open_id 绑定；只配 chat_id 时，owner 点按钮会因 operator
对不上被拒。首条来自该会话的消息足以确定身份，且因为该会话本身是部署者
显式声明的，不存在"随便谁都能认主"的风险。
"""
from __future__ import annotations

from app.core.database import session_scope
from app.services import owner_bootstrap
from tests.helpers import incoming

OWNER_CHAT = "oc_owner_learned"


def _configure(settings, monkeypatch, *, owner: str = "") -> None:
    monkeypatch.setattr(settings, "feishu_owner_open_id", owner)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", OWNER_CHAT)
    monkeypatch.setattr(settings, "feishu_owner_bootstrap", False)


def test_learns_owner_from_configured_chat(settings, monkeypatch):
    _configure(settings, monkeypatch)
    learned = owner_bootstrap.learn_owner_from_message(
        incoming("请问年假怎么申请", "om_learn_1", chat_id=OWNER_CHAT, sender="ou_owner_real")
    )
    assert learned == "ou_owner_real"
    assert settings.feishu_owner_open_id == "ou_owner_real"


def test_ignores_messages_from_other_chats(settings, monkeypatch):
    _configure(settings, monkeypatch)
    learned = owner_bootstrap.learn_owner_from_message(
        incoming("你好", "om_learn_2", chat_id="oc_someone_else", sender="ou_stranger")
    )
    assert learned == ""
    assert settings.feishu_owner_open_id == ""


def test_ignores_bot_messages(settings, monkeypatch):
    _configure(settings, monkeypatch)
    learned = owner_bootstrap.learn_owner_from_message(
        incoming("回声", "om_learn_3", chat_id=OWNER_CHAT, sender="ou_bot", sender_type="app")
    )
    assert learned == ""
    assert settings.feishu_owner_open_id == ""


def test_explicit_owner_wins(settings, monkeypatch):
    _configure(settings, monkeypatch, owner="ou_configured")
    learned = owner_bootstrap.learn_owner_from_message(
        incoming("你好", "om_learn_4", chat_id=OWNER_CHAT, sender="ou_other")
    )
    assert learned == "ou_configured"
    assert settings.feishu_owner_open_id == "ou_configured"


def test_persisted_identity_is_reused(settings, monkeypatch):
    _configure(settings, monkeypatch)
    owner_bootstrap.learn_owner_from_message(
        incoming("第一次", "om_learn_5", chat_id=OWNER_CHAT, sender="ou_owner_real")
    )
    # 模拟进程重启：内存配置被重置，但库里学到的身份仍应生效。
    object.__setattr__(settings, "feishu_owner_open_id", "")
    learned = owner_bootstrap.learn_owner_from_message(
        incoming("第二次", "om_learn_6", chat_id=OWNER_CHAT, sender="ou_owner_real")
    )
    assert learned == "ou_owner_real"
    with session_scope() as session:
        assert owner_bootstrap.resolve_owner_open_id(session) == "ou_owner_real"


def test_first_message_is_admitted_after_learning(settings, monkeypatch):
    _configure(settings, monkeypatch)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    from app.handlers.events import persist_event

    accepted, reason, should_process = persist_event(
        incoming("请问年假怎么申请", "om_learn_7", chat_id=OWNER_CHAT, sender="ou_owner_real")
    )
    assert accepted and should_process
    assert reason == "p2p_ok"
    assert settings.feishu_owner_open_id == "ou_owner_real"
