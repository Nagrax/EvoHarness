"""迁移回归：v5 把 sources 唯一键从 (chat, thread) 放开到 (chat, thread, owner)。

背景（2026-11-11 真实事故）：重建 sources 表时没先删旧索引，撞
`index ix_sources_tenant already exists`，迁移半途失败且 sources 被清空。
当时靠 sources_old 才救回数据。这条测试保证：

1. 旧库（有旧索引、有数据）迁移后数据一条不少；
2. 迁移后同一个群能容纳多个 owner 的来源；
3. 迁移可重复执行（幂等），不会二次清空。
"""
from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine, text

from app.core.migrations import SCHEMA_VERSION, migrate


def _make_legacy_db(path) -> None:
    """造一个"旧版"sources 表：唯一键只有 (tenant, chat_id, thread_id)。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE system_state (
            key VARCHAR(64) PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at DATETIME
        );
        CREATE TABLE sources (
            id INTEGER NOT NULL PRIMARY KEY,
            tenant VARCHAR(64) NOT NULL,
            owner VARCHAR(64) NOT NULL,
            chat_id VARCHAR(64) NOT NULL,
            thread_id VARCHAR(64) NOT NULL,
            source_type VARCHAR(16) NOT NULL,
            discovery VARCHAR(32) NOT NULL,
            read_identity VARCHAR(32) NOT NULL,
            reply_identity VARCHAR(32) NOT NULL,
            reply_allowed BOOLEAN NOT NULL,
            allowed_content_types VARCHAR(255) NOT NULL,
            enabled BOOLEAN NOT NULL,
            revoked_at DATETIME,
            coverage_status VARCHAR(16) NOT NULL,
            last_success_watermark_ms BIGINT,
            last_success_at DATETIME,
            created_at DATETIME,
            updated_at DATETIME,
            CONSTRAINT uq_source_chat_thread UNIQUE (tenant, chat_id, thread_id)
        );
        CREATE INDEX ix_sources_tenant ON sources (tenant);
        CREATE INDEX ix_sources_owner ON sources (owner);
        CREATE INDEX ix_sources_chat_id ON sources (chat_id);
        """
    )
    conn.execute(
        "INSERT INTO system_state (key, value) VALUES ('schema_version', '4')"
    )
    conn.execute(
        "INSERT INTO sources (id, tenant, owner, chat_id, thread_id, source_type,"
        " discovery, read_identity, reply_identity, reply_allowed,"
        " allowed_content_types, enabled, coverage_status)"
        " VALUES (1,'default','ou_lead','oc_group','','group','bot_list','bot','bot',1,"
        "'text,post',1,'partial')"
    )
    conn.commit()
    conn.close()


def test_migration_preserves_rows_and_relaxes_unique(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    report = migrate(engine, backup_dir=str(tmp_path))
    assert report["ok"], report

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, owner FROM sources")).fetchall()
        assert len(rows) == 1, f"迁移不能丢数据：{rows}"
        assert rows[0][1] == "ou_lead"
        version = conn.execute(
            text("SELECT value FROM system_state WHERE key='schema_version'")
        ).scalar()
        assert int(version) == SCHEMA_VERSION


def test_two_owners_can_share_one_chat_after_migration(tmp_path):
    """迁移后：同一个群里两个分身各有一条来源（这正是本次要放开的能力）。"""
    db_path = tmp_path / "legacy2.db"
    _make_legacy_db(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    migrate(engine, backup_dir=str(tmp_path))

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sources (tenant, owner, chat_id, thread_id, source_type,"
                " discovery, read_identity, reply_identity, reply_allowed,"
                " allowed_content_types, enabled, coverage_status,"
                " created_at, updated_at)"
                " VALUES ('default','ou_alice','oc_group','','group','bot_list',"
                "'bot','bot',1,'text,post',1,'partial',"
                " '2026-11-11 00:00:00','2026-11-11 00:00:00')"
            )
        )
        owners = {
            r[0]
            for r in conn.execute(
                text("SELECT owner FROM sources WHERE chat_id='oc_group'")
            ).fetchall()
        }
    assert owners == {"ou_lead", "ou_alice"}


def test_migration_is_idempotent(tmp_path):
    """重复执行不能再清空（事故的第二层保护）。"""
    db_path = tmp_path / "legacy3.db"
    _make_legacy_db(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    migrate(engine, backup_dir=str(tmp_path))
    second = migrate(engine, backup_dir=str(tmp_path))
    assert "already-current" in second["changes"]

    with engine.begin() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM sources")).scalar()
    assert count == 1
