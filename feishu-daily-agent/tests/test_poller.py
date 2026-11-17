"""历史轮询兜底回归。

背景（2026-11-10 实机接入）：事件订阅与卡片回调要发布新版本并过企业管理员
审核才生效；审核期间长连接能连上但收不到推送（开放平台事件日志可证）。
历史消息接口在已有权限下即可用，因此轮询必须能独立推进闭环。
"""
from __future__ import annotations

from app.core.database import session_scope
from app.models import Message, ReplyDraft
from app.pipeline import poller
from tests.helpers import FakeTransport, incoming

OWNER_CHAT = "oc_owner_poll"


def _configure(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "feishu_owner_chat_id", OWNER_CHAT)
    monkeypatch.setattr(settings, "feishu_owner_open_id", "ou_poll_owner")
    monkeypatch.setattr(settings, "feishu_chat_whitelist", OWNER_CHAT)
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", OWNER_CHAT)
    monkeypatch.setattr(settings, "deployment_mode", "readonly_shadow")


def _transport(*items) -> FakeTransport:
    transport = FakeTransport()
    transport.pages[OWNER_CHAT] = [list(items)]
    return transport


def test_poll_ingests_new_message_and_creates_draft(settings, monkeypatch):
    _configure(settings, monkeypatch)
    transport = _transport(
        incoming("请问年假怎么申请？", "om_poll_1", chat_id=OWNER_CHAT, sender="ou_poll_owner")
    )
    stats = poller.poll_once(transport=transport)
    assert stats["chats"] == 1
    assert stats["fetched"] == 1
    assert stats["new"] == 1
    with session_scope() as session:
        assert session.query(Message).filter(Message.message_id == "om_poll_1").one()
        draft = session.query(ReplyDraft).filter(ReplyDraft.message_id == "om_poll_1").one()
        assert str(draft.status) == "pending"
        assert draft.shadow is True


def test_poll_is_idempotent(settings, monkeypatch):
    _configure(settings, monkeypatch)
    payload = incoming("请问年假怎么申请？", "om_poll_2", chat_id=OWNER_CHAT, sender="ou_poll_owner")
    poller.poll_once(transport=_transport(payload))
    stats = poller.poll_once(transport=_transport(payload))
    assert stats["fetched"] == 1
    assert stats["new"] == 0
    with session_scope() as session:
        assert session.query(Message).filter(Message.message_id == "om_poll_2").count() == 1
        assert session.query(ReplyDraft).filter(ReplyDraft.message_id == "om_poll_2").count() == 1


def test_poll_records_bot_echo_without_reply(settings, monkeypatch):
    _configure(settings, monkeypatch)
    transport = _transport(
        incoming("收到，我先确认一下", "om_poll_echo", chat_id=OWNER_CHAT,
                 sender="ou_bot", sender_type="app")
    )
    stats = poller.poll_once(transport=transport)
    assert stats["new"] == 0
    with session_scope() as session:
        assert session.query(Message).filter(Message.message_id == "om_poll_echo").one()
        assert session.query(ReplyDraft).filter(
            ReplyDraft.message_id == "om_poll_echo"
        ).count() == 0


def test_poll_skips_unwhitelisted_chat(settings, monkeypatch):
    _configure(settings, monkeypatch)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", "")
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "")
    with session_scope() as session:
        poller.ensure_owner_source(session)
    stats = poller.poll_once(transport=FakeTransport())
    assert stats["chats"] == 0


def test_poll_survives_fetch_error(settings, monkeypatch):
    _configure(settings, monkeypatch)

    class Boom(FakeTransport):
        def fetch_page(self, *args, **kwargs):
            raise RuntimeError("history unavailable")

    stats = poller.poll_once(transport=Boom())
    assert stats["chats"] == 1
    assert stats["errors"], "取数失败必须显式记录，不能静默当成功"


def test_poll_window_ends_at_now(settings, monkeypatch):
    """回归：naive UTC 直接 .timestamp() 会差 8 小时，窗口整体偏到 8 小时前。

    2026-11-10 实机表现：轮询每轮 fetched=0，看起来"没有新消息"，
    实际是窗口落在 8 小时前。这里直接校验传给 transport 的时间窗。
    """
    _configure(settings, monkeypatch)
    captured: dict = {}

    class Recorder(FakeTransport):
        def fetch_page(self, chat_id, thread_id, start_ms, end_ms, page_token):
            captured["start"] = start_ms
            captured["end"] = end_ms
            return super().fetch_page(chat_id, thread_id, start_ms, end_ms, page_token)

    from app.core.clock import epoch_ms

    poller.poll_once(transport=Recorder())
    now_ms = epoch_ms()
    assert abs(captured["end"] - now_ms) < 60_000, "窗口终点必须是此刻"
    assert abs((captured["end"] - captured["start"]) / 1000 - 3600) < 120
