"""测试用的假传输、消息构造器与数据库工具。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.domain.intake import Page
from app.feishu.messages import IncomingMessage
from app.feishu.transport import TransportResult


@dataclass
class FakeTransport:
    results: list[TransportResult] = field(default_factory=list)
    pages: dict[str, list[list[IncomingMessage]]] = field(default_factory=dict)
    chats: list[dict] = field(default_factory=list)
    sent_text: list[tuple[str, str, str]] = field(default_factory=list)
    sent_cards: list[tuple[str, dict, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)
    # 以用户身份（数字分身）发出的记录：(chat_id, text)
    as_user_sent: list[tuple[str, str]] = field(default_factory=list)
    # 每次回复的真实发送身份（R09 回执对账断言用，与上游冻结结论一致）
    send_identities: list[str] = field(default_factory=list)
    calls: int = 0

    def _next(self, kind: str, target: str, payload) -> TransportResult:
        self.calls += 1
        if self.results:
            result = self.results.pop(0)
        else:
            result = TransportResult("sent", receipt_id=f"om_sent_{self.calls}")
        if kind == "reply_text":
            self.replies.append((target, payload, result.receipt_id))
        elif kind == "reply_card":
            self.sent_cards.append((target, payload, result.receipt_id))
        elif kind == "send_text":
            self.sent_text.append((target, payload, result.receipt_id))
        elif kind == "send_card":
            self.sent_cards.append((target, payload, result.receipt_id))
        return result

    def reply_text(self, message_id, text, idempotency_key="", reply_in_thread=False,
                   *, user_access_token="", user_chat_id="", send_identity="bot"):
        # 记录用的是哪个身份发的：分身相关断言需要区分 user / bot
        self.send_identities.append(send_identity)
        if user_access_token and user_chat_id:
            self.as_user_sent.append((user_chat_id, text))
        return self._next("reply_text", message_id, text)

    def reply_card(self, message_id, card, idempotency_key="", reply_in_thread=False):
        return self._next("reply_card", message_id, card)

    def send_text(self, receive_id, text, idempotency_key=""):
        return self._next("send_text", receive_id, text)

    def send_card(self, receive_id, card, idempotency_key=""):
        return self._next("send_card", receive_id, card)

    def list_chats(self):
        return list(self.chats)

    def fetch_page(self, chat_id, thread_id, start_ms, end_ms, page_token):
        pages = self.pages.get(chat_id) or []
        index = int(page_token or 0)
        if index >= len(pages):
            return Page([], "", False)
        return Page(pages[index], str(index + 1), index + 1 < len(pages))


def incoming(
    text: str,
    mid: str,
    *,
    chat_id: str = "oc_1",
    chat_type: str = "p2p",
    sender: str = "ou_user1",
    sender_type: str = "user",
    create_ms: int | None = None,
    thread_id: str = "",
    mentions: list[str] | None = None,
    msg_type: str = "text",
) -> IncomingMessage:
    return IncomingMessage(
        message_id=mid,
        chat_id=chat_id,
        chat_type=chat_type,
        sender_id=sender,
        sender_type=sender_type,
        msg_type=msg_type,
        text=text,
        create_time=create_ms if create_ms is not None else int(time.time() * 1000),
        thread_id=thread_id,
        mention_ids=mentions or [],
        raw="",
    )
