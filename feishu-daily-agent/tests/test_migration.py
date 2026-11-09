"""F02：旧原型库迁移、备份与可重入。"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.core.migrations import SCHEMA_VERSION, migrate


LEGACY_SCHEMA = """
CREATE TABLE chats (
  chat_id VARCHAR(64) PRIMARY KEY, chat_type VARCHAR(16), name VARCHAR(255),
  enabled BOOLEAN, last_sync_time BIGINT, updated_at DATETIME
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id VARCHAR(64) UNIQUE,
  chat_id VARCHAR(64), sender_id VARCHAR(64), sender_name VARCHAR(255),
  msg_type VARCHAR(32), content_text TEXT, create_time BIGINT,
  is_from_bot BOOLEAN, is_mention BOOLEAN, category VARCHAR(16),
  summary VARCHAR(128), due VARCHAR(64), handled BOOLEAN, raw_json TEXT,
  created_at DATETIME
);
CREATE TABLE experiences (
  id INTEGER PRIMARY KEY AUTOINCREMENT, question_pattern TEXT,
  canonical_answer TEXT, keywords TEXT, embedding_text TEXT, embedding_json TEXT,
  status VARCHAR(16), hit_count INTEGER, reject_count INTEGER,
  auto_reply_count INTEGER, source_message_id VARCHAR(64),
  created_at DATETIME, updated_at DATETIME
);
CREATE TABLE reply_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id VARCHAR(64), chat_id VARCHAR(64),
  experience_id INTEGER, draft_content TEXT, score FLOAT, confidence FLOAT,
  mode VARCHAR(16), status VARCHAR(16), sent_message_id VARCHAR(64),
  note TEXT, created_at DATETIME, reviewed_at DATETIME
);
CREATE TABLE review_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER, action VARCHAR(32),
  detail TEXT, created_at DATETIME
);
INSERT INTO experiences (question_pattern, canonical_answer, status)
VALUES ('年假怎么申请', '旧答案', 'verified');
INSERT INTO experiences (question_pattern, canonical_answer, status)
VALUES ('报销怎么弄', '旧答案2', 'draft');
INSERT INTO messages (message_id, chat_id, create_time, content_text, created_at)
VALUES ('om_old', 'oc_old', 1700000000000, '旧消息', '2026-01-01 00:00:00');
"""


def test_v2_database_gets_category_column(tmp_path: Path):
    db = tmp_path / "v2.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as conn:
        for statement in LEGACY_SCHEMA.split(";"):
            if statement.strip():
                conn.execute(text(statement))
        conn.execute(text("ALTER TABLE experiences ADD COLUMN tenant VARCHAR(64) NOT NULL DEFAULT 'default'"))
        conn.execute(text("ALTER TABLE experiences ADD COLUMN intent_id VARCHAR(64) NOT NULL DEFAULT ''"))
        conn.execute(text("ALTER TABLE experiences ADD COLUMN version INTEGER NOT NULL DEFAULT 1"))
        conn.execute(text(
            "UPDATE experiences SET tenant='default', intent_id='i' || id, version=1"
        ))
        conn.execute(text("CREATE UNIQUE INDEX ux_exp_version ON experiences (tenant, intent_id, version)"))
        conn.execute(text(
            "INSERT INTO experiences (question_pattern, canonical_answer, status, tenant, intent_id, version) "
            "SELECT question_pattern, 'v2', 'candidate', 'default', intent_id, 2 "
            "FROM experiences WHERE id=(SELECT MIN(id) FROM experiences)"
        ))
        conn.execute(text(
            "CREATE TABLE system_state (key VARCHAR(64) PRIMARY KEY, value TEXT, updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO system_state (key, value) VALUES ('schema_version', '2')"
        ))
    report = migrate(engine, backup_dir=tmp_path)
    assert report["ok"]
    columns = {c["name"] for c in inspect(engine).get_columns("experiences")}
    assert "category" in columns
    with engine.begin() as conn:
        version = conn.execute(
            text("SELECT value FROM system_state WHERE key='schema_version'")
        ).scalar_one()
        assert int(version) == SCHEMA_VERSION
        # 模拟 v2 库已有同 intent 的两个版本：再迁移不得重置或撞 UNIQUE。
        conn.execute(text("UPDATE system_state SET value='2' WHERE key='schema_version'"))
    again = migrate(engine, backup_dir=tmp_path)
    assert again["ok"]
    with engine.begin() as conn:
        assert int(conn.execute(
            text("SELECT value FROM system_state WHERE key='schema_version'")
        ).scalar_one()) == SCHEMA_VERSION
        intent = conn.execute(text(
            "SELECT intent_id FROM experiences WHERE id=(SELECT MIN(id) FROM experiences)"
        )).scalar_one()
        versions = conn.execute(
            text("SELECT version FROM experiences WHERE intent_id=:intent ORDER BY version"),
            {"intent": intent},
        ).fetchall()
        assert [row[0] for row in versions] == [1, 2]


def test_legacy_database_migrates_and_is_idempotent(tmp_path: Path):
    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as conn:
        for statement in LEGACY_SCHEMA.split(";"):
            if statement.strip():
                conn.execute(text(statement))
    report = migrate(engine, backup_dir=tmp_path)
    assert report["ok"]
    assert report["backup"] and Path(report["backup"]).is_file()
    columns = {c["name"] for c in inspect(engine).get_columns("experiences")}
    assert {"tenant", "intent_id", "version", "patterns_json", "visibility", "category"} <= columns
    message_columns = {c["name"] for c in inspect(engine).get_columns("messages")}
    assert {"tenant", "thread_id", "content_hash", "recalled"} <= message_columns
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT status, version, intent_id FROM experiences ORDER BY id")).fetchall()
        assert [row[0] for row in rows] == ["approved_auto", "candidate"]
        assert all(row[1] == 1 for row in rows)
        assert all(row[2] for row in rows)
        heads = conn.execute(text("SELECT COUNT(*) FROM experience_heads")).scalar_one()
        assert heads == 2
        version = conn.execute(
            text("SELECT value FROM system_state WHERE key='schema_version'")
        ).scalar_one()
        assert int(version) == SCHEMA_VERSION
    again = migrate(engine, backup_dir=tmp_path)
    assert again["ok"]
    assert "already-current" in again["changes"]
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM experience_heads")).scalar_one() == 2
