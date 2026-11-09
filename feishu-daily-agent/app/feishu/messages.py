"""飞书消息的统一数据结构与适配器。

本模块不导入 lark_oapi，离线测试可在没有飞书 SDK/网络的机器上运行。
真正的网络读写集中在 `app.feishu.transport`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List

from app.core.config import get_settings
from app.feishu.parser import clean_mention_placeholders, parse_message_content


@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str  # p2p / group
    sender_id: str
    sender_type: str  # user / app
    msg_type: str
    text: str
    create_time: int  # 毫秒
    thread_id: str = ""
    sender_name: str = ""
    mention_ids: List[str] = field(default_factory=list)
    mention_names: List[str] = field(default_factory=list)
    raw: str = ""
    event_time_ms: int = 0

    @property
    def bot_open_id(self) -> str:
        return get_settings().feishu_bot_open_id.strip()

    @property
    def is_from_bot(self) -> bool:
        bot_open_id = self.bot_open_id
        if bot_open_id:
            return self.sender_id == bot_open_id
        # 身份未知时只信任 sender_type；不再用“任意 @”推断机器人。
        return self.sender_type == "app"

    @property
    def mentions_bot(self) -> bool:
        bot_open_id = self.bot_open_id
        if not bot_open_id:
            return False
        return bot_open_id in self.mention_ids

    @property
    def mentions_someone_unknown(self) -> bool:
        return bool(self.mention_ids) and not self.bot_open_id


def _extract_mentions(mentions: Any) -> tuple[list[str], list[str]]:
    ids, names = [], []
    for m in mentions or []:
        try:
            # 事件 API 的 m.id 是含 open_id 的对象；历史接口的 m.id 是字符串 open_id。
            # 原实现只按对象取值，str.open_id 抛 AttributeError 被吞掉，历史消息会丢 @ 提及。
            raw_id = m.id
            if isinstance(raw_id, str):
                open_id = raw_id
            else:
                open_id = getattr(raw_id, "open_id", "") or ""
            if open_id:
                ids.append(open_id)
            name = getattr(m, "name", "")
            if name:
                names.append(name)
        except AttributeError:
            continue
    return ids, names


def from_history_item(item: Any) -> IncomingMessage:
    mention_ids, mention_names = _extract_mentions(getattr(item, "mentions", None))
    raw_content = item.body.content if getattr(item, "body", None) else ""
    text = parse_message_content(item.msg_type, raw_content)
    if item.msg_type == "text":
        text = clean_mention_placeholders(text)
    sender = getattr(item, "sender", None)
    return IncomingMessage(
        message_id=item.message_id,
        chat_id=item.chat_id,
        chat_type=getattr(item, "chat_type", "") or "",
        sender_id=sender.id if sender else "",
        sender_type=sender.sender_type if sender else "",
        msg_type=item.msg_type,
        text=text,
        create_time=int(item.create_time),
        thread_id=getattr(item, "thread_id", "") or "",
        sender_name=getattr(sender, "name", "") or "" if sender else "",
        mention_ids=mention_ids,
        mention_names=mention_names,
        raw=raw_content,
    )


def from_event(event: Any) -> IncomingMessage:
    message = event.message
    # 飞书事件模型字段名是 message_type；历史接口模型是 msg_type，两处都要兼容。
    msg_type = getattr(message, "message_type", None) or getattr(message, "msg_type", "") or ""
    mention_ids, mention_names = _extract_mentions(getattr(message, "mentions", None))
    raw_content = getattr(message, "content", "") or ""
    text = parse_message_content(msg_type, raw_content)
    if msg_type == "text":
        text = clean_mention_placeholders(text)
    sender = getattr(event, "sender", None)
    sender_id_obj = getattr(sender, "sender_id", None) if sender else None
    return IncomingMessage(
        message_id=message.message_id,
        chat_id=message.chat_id,
        chat_type=message.chat_type,
        sender_id=getattr(sender_id_obj, "open_id", "") if sender_id_obj else "",
        sender_type=getattr(sender, "sender_type", "") or "" if sender else "",
        msg_type=msg_type,
        text=text,
        create_time=int(message.create_time),
        thread_id=getattr(message, "thread_id", "") or "",
        sender_name=getattr(sender, "name", "") or "" if sender else "",
        mention_ids=mention_ids,
        mention_names=mention_names,
        raw=raw_content,
    )
