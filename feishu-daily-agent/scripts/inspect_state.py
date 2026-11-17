"""只读检查本机 agent.db 的闭环状态（联调取证用，不改数据）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(__file__).resolve().parents[1] / "data" / "agent.db"


def rows(db: sqlite3.Connection, sql: str) -> list[dict]:
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(sql)]


def main() -> int:
    if not DB.is_file():
        print(f"no db at {DB}")
        return 2
    db = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    sections = {
        "messages": "select message_id, chat_id, sender_id, category, handled, "
        "substr(content_text,1,28) as text from messages order by id desc limit 8",
        "tasks": "select id, title, status, needs_confirmation from tasks order by id",
        "drafts": "select id, message_id, status, mode, shadow, round(score,3) as score, "
        "substr(draft_content,1,36) as content from reply_drafts order by id",
        "decisions": "select message_id, decision, reason_codes, round(similarity,3) as sim "
        "from reply_decisions order by id",
        "chats": "select chat_id, chat_type, name from chats",
        "outbox": "select id, kind, status, target_type, target_id, receipt_message_id "
        "from outbox order by id",
        "daily": "select id, logical_date, status, revision from daily_reports order by id",
        "experiences": "select id, status, version, visibility, substr(canonical_answer,1,30) as a "
        "from experiences order by id",
    }
    for name, sql in sections.items():
        print(f"== {name} ==")
        try:
            data = rows(db, sql)
        except sqlite3.Error as exc:
            print("  ERR", exc)
            continue
        if not data:
            print("  (empty)")
        for item in data:
            print(" ", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
