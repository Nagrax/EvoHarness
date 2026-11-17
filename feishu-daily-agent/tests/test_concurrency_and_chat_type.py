"""并发与类型修正回归。

真实服务里实时事件跑在线程池（4 worker），SQLite 默认 5s 忙等会抛
"database is locked"（2026-11-10 实机联调复现）；同时 p2p chat 曾被按 oc_
前缀误登记成 group，需要能就地修正。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.core.database import session_scope
from app.models import Chat, Message
from app.services.reply_engine import upsert_chat
from tests.helpers import incoming


def test_concurrent_message_writes_do_not_lock(source):
    from app.domain import intake

    def write(index: int) -> bool:
        with session_scope() as session:
            intake.ingest_message(
                session, incoming(f"并发消息 {index}", f"om_concurrent_{index}"), source=source
            )
        return True

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(8)))
    assert all(results)
    with session_scope() as session:
        assert session.query(Message).filter(
            Message.message_id.like("om_concurrent_%")
        ).count() == 8


def test_upsert_chat_corrects_stale_type():
    with session_scope() as session:
        session.add(Chat(chat_id="oc_type_fix", tenant="default", chat_type="group", name=""))
        session.flush()
        chat = upsert_chat(session, "oc_type_fix", "p2p", "")
        assert chat.chat_type == "p2p"
