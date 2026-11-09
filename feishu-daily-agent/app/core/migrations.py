"""旧原型库迁移：可备份、可重入、不覆盖已迁移数据。"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.ids import question_hash
from app.models import Base, ExperienceHead, SystemState

SCHEMA_VERSION = 3
logger = logging.getLogger(__name__)


def _prune_backups(target: Path, keep: int) -> None:
    """只保留最近 keep 份迁移备份；每次启动都复制会让 49MB 备份无限堆积。"""
    if keep <= 0:
        return
    base = target.name.split(".bak-", 1)[0]
    backups = sorted(
        target.parent.glob(f"{base}.bak-*"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[keep:]:
        try:
            stale.unlink()
        except OSError as exc:
            logger.warning("prune backup failed: %s (%s)", stale, exc)


def _copy_backup(path: Path, target_dir: Path) -> str | None:
    target_dir.mkdir(parents=True, exist_ok=True)
    keep = max(1, int(get_settings().migration_backup_keep or 3))
    target = target_dir / f"{path.name}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        free = shutil.disk_usage(target_dir).free
        if free < path.stat().st_size + 10 * 1024 * 1024:
            logger.warning(
                "skip db backup: free=%s bytes, db=%s bytes", free, path.stat().st_size
            )
            _prune_backups(target, keep)
            return None
        shutil.copy2(path, target)
    except OSError as exc:
        logger.warning("db backup failed: %s", exc)
        _prune_backups(target, keep)
        return None
    _prune_backups(target, keep)
    return str(target)


def _columns(conn, table: str) -> set[str]:
    return {column["name"] for column in inspect(conn).get_columns(table)}


def _add(conn, table: str, column: str, ddl: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def _table_exists(conn, table: str) -> bool:
    return table in inspect(conn).get_table_names()


def backup_database(url: str, *, directory: str | Path | None = None) -> str | None:
    if not url.startswith("sqlite:///"):
        return None
    raw = url.split("sqlite:///", 1)[1].split("?", 1)[0]
    path = Path(raw)
    if not path.is_file() or raw in {":memory:", ""}:
        return None
    target_dir = Path(directory) if directory else path.parent
    return _copy_backup(path, target_dir)


def _backup_engine_database(engine: Engine, directory: str | Path | None) -> str | None:
    if engine.url.get_backend_name() != "sqlite" or not engine.url.database:
        return None
    path = Path(engine.url.database)
    if not path.is_file():
        return None
    target_dir = Path(directory) if directory else path.parent
    return _copy_backup(path, target_dir)


def migrate(engine: Engine, *, backup_dir: str | Path | None = None) -> dict:
    settings = get_settings()
    backup = _backup_engine_database(engine, backup_dir)
    Base.metadata.create_all(bind=engine)
    report = {"fromVersion": 1, "toVersion": SCHEMA_VERSION, "backup": backup, "changes": []}
    with engine.begin() as conn:
        version_row = None
        if _table_exists(conn, "system_state"):
            version_row = conn.execute(
                text("SELECT value FROM system_state WHERE key='schema_version'")
            ).fetchone()
        current_version = int(version_row[0]) if version_row else 1
        report["fromVersion"] = current_version
        if current_version >= SCHEMA_VERSION:
            report["changes"].append("already-current")
            report["ok"] = True
            return report

        if _table_exists(conn, "chats"):
            _add(conn, "chats", "tenant", "VARCHAR(64) NOT NULL DEFAULT 'default'")
        if _table_exists(conn, "messages"):
            _add(conn, "messages", "tenant", "VARCHAR(64) NOT NULL DEFAULT 'default'")
            _add(conn, "messages", "source_id", "INTEGER")
            _add(conn, "messages", "thread_id", "VARCHAR(64) NOT NULL DEFAULT ''")
            _add(conn, "messages", "content_hash", "VARCHAR(64) NOT NULL DEFAULT ''")
            _add(conn, "messages", "event_time_ms", "BIGINT NOT NULL DEFAULT 0")
            _add(conn, "messages", "revision", "INTEGER NOT NULL DEFAULT 1")
            _add(conn, "messages", "edited", "BOOLEAN NOT NULL DEFAULT 0")
            _add(conn, "messages", "recalled", "BOOLEAN NOT NULL DEFAULT 0")
            _add(conn, "messages", "raw_retention_until", "DATETIME")
            _add(conn, "messages", "updated_at", "DATETIME")
            conn.execute(
                text(
                    "UPDATE messages SET event_time_ms=create_time "
                    "WHERE event_time_ms=0 OR event_time_ms IS NULL"
                )
            )
            conn.execute(
                text(
                    "UPDATE messages SET updated_at=created_at "
                    "WHERE updated_at IS NULL"
                )
            )
            conn.execute(
                text(
                    "UPDATE messages SET content_hash=lower(hex(randomblob(16))) "
                    "WHERE content_hash=''"
                )
            )
        if _table_exists(conn, "reply_drafts"):
            _add(conn, "reply_drafts", "tenant", "VARCHAR(64) NOT NULL DEFAULT 'default'")
            _add(conn, "reply_drafts", "content_hash", "VARCHAR(64) NOT NULL DEFAULT ''")
            _add(conn, "reply_drafts", "version", "INTEGER NOT NULL DEFAULT 1")
            _add(conn, "reply_drafts", "shadow", "BOOLEAN NOT NULL DEFAULT 0")
            _add(conn, "reply_drafts", "approved_by", "VARCHAR(64) NOT NULL DEFAULT ''")
            _add(conn, "reply_drafts", "approved_at", "DATETIME")
            _add(conn, "reply_drafts", "superseded_by", "INTEGER")
        if _table_exists(conn, "experiences"):
            _add(conn, "experiences", "tenant", "VARCHAR(64) NOT NULL DEFAULT 'default'")
            _add(conn, "experiences", "owner", "VARCHAR(64) NOT NULL DEFAULT ''")
            _add(conn, "experiences", "intent_id", "VARCHAR(64) NOT NULL DEFAULT ''")
            _add(conn, "experiences", "version", "INTEGER NOT NULL DEFAULT 1")
            _add(conn, "experiences", "patterns_json", "TEXT NOT NULL DEFAULT '[]'")
            _add(conn, "experiences", "negative_examples_json", "TEXT NOT NULL DEFAULT '[]'")
            _add(conn, "experiences", "audience_json", "TEXT NOT NULL DEFAULT '{}'")
            _add(conn, "experiences", "preconditions", "TEXT NOT NULL DEFAULT ''")
            _add(conn, "experiences", "steps", "TEXT NOT NULL DEFAULT ''")
            _add(conn, "experiences", "allowed_links", "TEXT NOT NULL DEFAULT ''")
            _add(conn, "experiences", "source_ids", "TEXT NOT NULL DEFAULT '[]'")
            _add(conn, "experiences", "sensitive_level", "VARCHAR(16) NOT NULL DEFAULT 'private'")
            _add(conn, "experiences", "visibility", "VARCHAR(16) NOT NULL DEFAULT 'private'")
            _add(conn, "experiences", "valid_until", "DATETIME")
            _add(conn, "experiences", "previous_id", "INTEGER")
            _add(conn, "experiences", "category", "VARCHAR(16) NOT NULL DEFAULT 'faq'")
            _add(conn, "experiences", "reason", "TEXT NOT NULL DEFAULT ''")
            _add(conn, "experiences", "created_by", "VARCHAR(64) NOT NULL DEFAULT ''")
            _add(conn, "experiences", "created_at", "DATETIME")
            _add(conn, "experiences", "updated_at", "DATETIME")
            # 只有 v1 原型库需要回填 intent/version/status；v2+ 已有版本语义，
            # 重复回填会重置版本并在同 intent 多版本时撞 UNIQUE。
            rows = [] if current_version >= 2 else conn.execute(
                text("SELECT id, question_pattern, status, source_message_id FROM experiences")
            ).fetchall()
            status_map = {
                "draft": "candidate",
                "verified": "approved_auto",
                "disabled": "paused",
            }
            for row in rows:
                intent = question_hash(row[1] or "")
                conn.execute(
                    text(
                        "UPDATE experiences SET tenant=:tenant, owner=:owner, intent_id=:intent, "
                        "version=1, patterns_json=:patterns, created_at=COALESCE(created_at, :now), "
                        "updated_at=COALESCE(updated_at, :now), status=:status WHERE id=:id"
                    ),
                    {
                        "tenant": settings.tenant_id,
                        "owner": settings.feishu_owner_open_id,
                        "intent": intent,
                        "patterns": __import__("json").dumps([row[1] or ""], ensure_ascii=False),
                        "now": utcnow().isoformat(sep=" "),
                        "status": status_map.get(row[2], row[2]),
                        "id": row[0],
                    },
                )
                conn.execute(
                    text(
                        "INSERT OR REPLACE INTO experience_heads (intent_id, tenant, experience_id, updated_at) "
                        "VALUES (:intent, :tenant, :id, :now)"
                    ),
                    {
                        "intent": intent,
                        "tenant": settings.tenant_id,
                        "id": row[0],
                        "now": utcnow().isoformat(sep=" "),
                    },
                )
        if _table_exists(conn, "review_logs"):
            _add(conn, "review_logs", "tenant", "VARCHAR(64) NOT NULL DEFAULT 'default'")
            _add(conn, "review_logs", "action_id", "VARCHAR(64) NOT NULL DEFAULT ''")
            _add(conn, "review_logs", "target_type", "VARCHAR(32) NOT NULL DEFAULT 'draft'")
            _add(conn, "review_logs", "target_id", "VARCHAR(64) NOT NULL DEFAULT ''")
            _add(conn, "review_logs", "target_version", "INTEGER NOT NULL DEFAULT 0")
            _add(conn, "review_logs", "operator_id", "VARCHAR(64) NOT NULL DEFAULT ''")
        conn.execute(
            text(
                "INSERT OR REPLACE INTO system_state (key, value, updated_at) "
                "VALUES ('schema_version', :version, :now)"
            ),
            {"version": str(SCHEMA_VERSION), "now": utcnow().isoformat(sep=" ")},
        )
        report["changes"].append("migrated")
    report["ok"] = True
    return report


def ensure_schema(engine: Engine) -> dict:
    return migrate(engine)
