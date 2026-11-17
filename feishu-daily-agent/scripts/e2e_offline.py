"""端到端驱动：用真实数据库跑实时事件处理器（不依赖飞书长连接）。

模拟飞书事件结构，验证「消息准入 → 分类 → 任务抽取 → 经验匹配/草稿」链路，
并打印落库结果。仅用于本机联调，不写入测试库。
"""
from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from types import SimpleNamespace as NS  # noqa: E402

from app.handlers.events import handle_message_event  # noqa: E402


def msg(text: str, mid: str, *, chat: str, sender: str, chat_type: str = "p2p"):
    return NS(
        header=NS(event_id=f"evt-{mid}"),
        event=NS(
            sender=NS(sender_id=NS(open_id=sender), sender_type="user"),
            message=NS(
                message_id=mid,
                chat_id=chat,
                chat_type=chat_type,
                message_type="text",
                content='{"text":"%s"}' % text,
                create_time=str(int(time.time() * 1000)),
                thread_id="",
                mentions=[],
            ),
        ),
    )


def main() -> None:
    chat = sys.argv[1]
    sender = sys.argv[2]
    cases = [
        ("请问年假怎么申请？", "om_e2e_q1"),
        ("任务：把页脚改成显示模型和协议", "om_e2e_t1"),
        ("通知：本周五 20:00 系统维护", "om_e2e_n1"),
        ("问：EvoHarness 的闭环台账落在哪个目录？", "om_e2e_ask1"),
    ]
    for text, mid in cases:
        handle_message_event(msg(text, mid, chat=chat, sender=sender))
        print("dispatched", mid, text)
    time.sleep(6)


if __name__ == "__main__":
    main()
