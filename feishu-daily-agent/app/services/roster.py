"""使用者花名册：谁在用这个机器人、日报该发给谁。

为什么需要它
------------
之前系统里只有"一个 owner"，日报只发给 `FEISHU_OWNER_CHAT_ID`。
用户要求"每日推送是给每一个使用者的"，所以要把"使用者"从单数变成集合。

边界（刻意的取舍）
------------------
- **使用者 = 跟机器人有过**p2p**互动的真人**。群成员不算：群里谁说过话不代表
  他想每天收日报，那会变成骚扰。
- **管理员仍是单一 owner**（`FEISHU_OWNER_CHAT_ID`）：审批权不因为人多而分散。
  普通使用者收到的是"你的待办/你问过的回复"，不是全局台账。
- 花名册**由实际数据推导**（sources/messages），不另建一张表：避免两份真相。
  新用户第一次私聊机器人就自动出现在花名册里。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Chat, Message, Source

logger = logging.getLogger(__name__)


@dataclass
class User:
    open_id: str
    chat_id: str
    name: str = ""
    is_owner: bool = False
    message_count: int = 0

    def as_dict(self) -> dict:
        return {
            "openId": self.open_id,
            "chatId": self.chat_id,
            "name": self.name,
            "isOwner": self.is_owner,
            "messages": self.message_count,
        }


@dataclass
class Roster:
    owner: User | None = None
    users: list[User] = field(default_factory=list)

    def recipients(self) -> list[User]:
        """日报收件人：全部使用者（含 owner），按 open_id 去重。"""
        seen: dict[str, User] = {}
        for user in ([self.owner] if self.owner else []) + self.users:
            if user and user.open_id and user.open_id not in seen:
                seen[user.open_id] = user
        return list(seen.values())

    def as_dict(self) -> dict:
        return {
            "owner": self.owner.as_dict() if self.owner else None,
            "users": [u.as_dict() for u in self.users],
            "recipientCount": len(self.recipients()),
        }


def _owner_ids() -> tuple[str, str]:
    settings = get_settings()
    return (
        (settings.feishu_owner_open_id or "").strip(),
        (settings.feishu_owner_chat_id or "").strip(),
    )


def build_roster(session: Session, *, tenant: str | None = None) -> Roster:
    """日报收件人花名册。

    产品定位（2026-11-11 用户明确）：只解决日常问答，日报只发给**显式订阅过**
    的人。不按"用过就加入"推导——群成员没有同意收私聊推送，自动纳入等于替
    用户做决定。

    管理员（owner）始终是收件人：他要看全局台账，这是运维需要而非订阅行为。
    """
    settings = get_settings()
    tenant = tenant or settings.tenant_id
    owner_open_id, owner_chat_id = _owner_ids()

    roster = Roster()
    if owner_chat_id:
        roster.owner = User(
            open_id=owner_open_id, chat_id=owner_chat_id, name="", is_owner=True
        )

    from app.services import subscription

    by_open_id: dict[str, User] = {}
    for item in subscription.subscribers(session):
        open_id = (item.get("openId") or "").strip()
        if not open_id or not open_id.startswith("ou_"):
            continue
        if owner_open_id and open_id == owner_open_id:
            # owner 已由上方的 owner 条目负责，避免重复
            continue
        chat_id = (item.get("chatId") or "").strip()
        by_open_id[open_id] = User(
            open_id=open_id, chat_id=chat_id, name=str(item.get("name") or "")
        )

    roster.users = sorted(by_open_id.values(), key=lambda u: u.open_id)
    return roster


def register_user(session: Session, *, open_id: str, chat_id: str, name: str = "") -> User | None:
    """把一个 p2p 使用者登记进来源（首次私聊时调用）。

    只登记 p2p；群聊成员不进入日报收件人。
    """
    from app.domain import admission

    open_id = (open_id or "").strip()
    chat_id = (chat_id or "").strip()
    if not open_id.startswith("ou_") or not chat_id:
        return None
    settings = get_settings()
    source = admission.ensure_source(
        session,
        tenant=settings.tenant_id,
        owner=open_id,
        chat_id=chat_id,
        source_type="p2p",
        discovery="user_first_contact",
        reply_allowed=settings.reply_allowed_for(chat_id),
    )
    return User(open_id=open_id, chat_id=chat_id, name=name, is_owner=open_id == settings.feishu_owner_open_id)
