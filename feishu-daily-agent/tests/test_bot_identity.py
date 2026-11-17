"""回归：历史接口与实时事件的机器人身份口径不同。

背景（2026-11-10 实机接入）：`im/v1/messages` 历史接口返回的机器人消息里
`sender.id` 是 **app_id**（cli_ 前缀），而实时事件的 `sender_id.open_id` 是
机器人 open_id。旧 `is_from_bot` 只要配置了 bot_open_id 就只比 open_id，
于是轮询拉回来的自家回执被当成用户消息，会再触发一轮回复（自回复环）。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.feishu.messages import IncomingMessage, from_history_item

APP_ID = "cli_aa78dd83aef8dbb0"
BOT_OPEN_ID = "ou_4fcc5742a07c5e0b2ddd174e58644301"


def _history_item(sender_id: str, sender_type: str, text: str = "你好"):
    return SimpleNamespace(
        message_id="om_hist_1",
        chat_id="oc_x",
        chat_type="p2p",
        msg_type="text",
        create_time="1794285000000",
        thread_id="",
        mentions=[],
        body=SimpleNamespace(content=f'{{"text":"{text}"}}'),
        sender=SimpleNamespace(id=sender_id, sender_type=sender_type, name=""),
    )


def test_history_bot_message_with_app_id_is_bot(settings, monkeypatch):
    monkeypatch.setattr(settings, "feishu_bot_open_id", BOT_OPEN_ID)
    incoming = from_history_item(_history_item(APP_ID, "app"))
    assert incoming.is_from_bot is True


def test_history_bot_message_without_configured_bot_id_is_bot(settings, monkeypatch):
    monkeypatch.setattr(settings, "feishu_bot_open_id", "")
    incoming = from_history_item(_history_item(APP_ID, "app"))
    assert incoming.is_from_bot is True


def test_history_user_message_is_not_bot(settings, monkeypatch):
    monkeypatch.setattr(settings, "feishu_bot_open_id", BOT_OPEN_ID)
    incoming = from_history_item(_history_item("ou_owner_real", "user"))
    assert incoming.is_from_bot is False


def test_realtime_event_bot_open_id_is_bot(settings, monkeypatch):
    monkeypatch.setattr(settings, "feishu_bot_open_id", BOT_OPEN_ID)
    msg = IncomingMessage(
        message_id="om_evt_1",
        chat_id="oc_x",
        chat_type="p2p",
        sender_id=BOT_OPEN_ID,
        sender_type="app",
        msg_type="text",
        text="回声",
        create_time=1794285000000,
    )
    assert msg.is_from_bot is True
