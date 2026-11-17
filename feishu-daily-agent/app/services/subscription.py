"""日报订阅：私聊即视为愿意接收，但必须显式告知。

产品定位（2026-11-11 用户明确）：
**愿意私聊数字员工的人，就是愿意收日报的人。**
所以不是"先订阅才发"，而是"默认开启 + 显式告知 + 随时可退订"：

- 用户第一次私聊机器人 → 自动纳入日报，并立刻收到一条明确告知
  （"已为你开启每日日报，回复退订可关闭"）；
- 告知只发一次，不天天骚扰；
- 「退订」立即移出，且不会再被自动加回来（记住拒绝）。

为什么不彻底静默加进去：不告知就推私聊消息是不礼貌的，也算一种打扰。
告知一次既尊重用户，又不给愿意用的人增加操作。
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.core.database import session_scope
from app.models import SystemState

logger = logging.getLogger(__name__)

SUBSCRIBERS_KEY = "daily_subscribers"
OPTED_OUT_KEY = "daily_opted_out"

SUBSCRIBE_WORDS = {"订阅", "订阅日报", "开启日报", "我要日报", "subscribe"}
UNSUBSCRIBE_WORDS = {"退订", "取消订阅", "关闭日报", "不要日报", "unsubscribe"}


def _load(session: Session, key: str) -> dict:
    state = session.get(SystemState, key)
    if state is None:
        return {}
    try:
        data = json.loads(state.value or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _save(session: Session, key: str, data: dict) -> None:
    state = session.get(SystemState, key) or SystemState(key=key, value="")
    state.value = json.dumps(data, ensure_ascii=False, sort_keys=True)
    session.add(state)
    session.flush()


def is_subscribed(session: Session, open_id: str) -> bool:
    open_id = (open_id or "").strip()
    if not open_id:
        return False
    return open_id in _load(session, SUBSCRIBERS_KEY)


def has_opted_out(session: Session, open_id: str) -> bool:
    return (open_id or "").strip() in _load(session, OPTED_OUT_KEY)


def subscribe(session: Session, *, open_id: str, chat_id: str, name: str = "") -> bool:
    """登记订阅；返回是否可投递（已在名单内也算 True）。"""
    open_id = (open_id or "").strip()
    if not open_id.startswith("ou_"):
        return False
    data = _load(session, SUBSCRIBERS_KEY)
    data[open_id] = {"chatId": chat_id, "name": name}
    _save(session, SUBSCRIBERS_KEY, data)
    # 重新订阅时清掉退订标记
    opted_out = _load(session, OPTED_OUT_KEY)
    if open_id in opted_out:
        opted_out.pop(open_id, None)
        _save(session, OPTED_OUT_KEY, opted_out)
    return True


def unsubscribe(session: Session, *, open_id: str) -> bool:
    """退订并记住"他不想收"，避免下次私聊又被自动加回来。"""
    open_id = (open_id or "").strip()
    data = _load(session, SUBSCRIBERS_KEY)
    was_in = open_id in data
    data.pop(open_id, None)
    _save(session, SUBSCRIBERS_KEY, data)
    opted_out = _load(session, OPTED_OUT_KEY)
    opted_out[open_id] = {"at": True}
    _save(session, OPTED_OUT_KEY, opted_out)
    logger.info("取消日报订阅 %s", open_id[-6:])
    return was_in


def subscribers(session: Session) -> list[dict]:
    data = _load(session, SUBSCRIBERS_KEY)
    return [
        {"openId": open_id, **(value if isinstance(value, dict) else {})}
        for open_id, value in sorted(data.items())
    ]


def auto_enroll(session: Session, *, open_id: str, chat_id: str, name: str = "") -> bool:
    """首次私聊时自动纳入日报；返回是否**需要发告知**。

    退订过的人不再自动加回来（尊重明确拒绝）。
    """
    open_id = (open_id or "").strip()
    if not open_id.startswith("ou_"):
        return False
    if is_subscribed(session, open_id):
        return False
    if has_opted_out(session, open_id):
        return False
    subscribe(session, open_id=open_id, chat_id=chat_id, name=name)
    return True


def handle_subscribe_command(session: Session, *, text: str, open_id: str, chat_id: str, name: str = "") -> str | None:
    """处理订阅/退订指令；不是这类指令返回 None。"""
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return None
    if cleaned in {w.lower() for w in SUBSCRIBE_WORDS}:
        was = is_subscribed(session, open_id)
        subscribe(session, open_id=open_id, chat_id=chat_id, name=name)
        if was:
            return "你已经在收日报了，每天都收得到。回复「退订」可取消。"
        return (
            "已恢复日报 ✅\n"
            "以后每天 08:30 我会把「你的待办」和「等你确认的回复」发给你。\n"
            "回复「退订」可随时取消。"
        )
    if cleaned in {w.lower() for w in UNSUBSCRIBE_WORDS}:
        if unsubscribe(session, open_id=open_id):
            return "已退订 📭 不再给你发日报。想恢复就回复「订阅」。"
        return "你本来就没在收日报。回复「订阅」可开启。"
    return None


def should_prompt(session: Session, open_id: str) -> bool:
    """是否该给这个用户展示订阅引导。

    自动纳入之后不需要"引导"，需要的是一次"告知"——由 auto_enroll 的返回值决定。
    这里保留给"退订过又想恢复"的场景提示一次。
    """
    return False


def mark_prompted(open_id: str) -> None:
    """兼容旧调用：现在用 OPTED_OUT 标记拒绝，不再需要 prompted 记录。"""


ENROLL_NOTICE = (
    "（已为你开启每日日报：每天 08:30 把「你的待办」和「等你确认的回复」发给你。"
    "不需要就回复「退订」。）"
)

SUBSCRIPTION_HINT = ENROLL_NOTICE
