"""旧原型库迁移：可备份、可重入、不覆盖已迁移数据。"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from evoharness.clock import utcnow
from evoharness.ids import question_hash
from evoharness.models import Base, ExperienceHead, SystemState

# 注册组织、Run、治理与知识表，保证 create_all 不依赖调用方恰好导入子模块。
import evoharness.organization.models  # noqa: F401
import evoharness.organization.access  # noqa: F401
import evoharness.organization.capabilities  # noqa: F401  # DE-42 岗位能力证据表
import evoharness.work.models  # noqa: F401
import evoharness.work.intake_journal  # noqa: F401  # DE-05 产品库事务内 intake 日志
import evoharness.work.objectives  # noqa: F401  # DE-39 目标/里程碑
import evoharness.work.plans  # noqa: F401  # DE-39 工作计划/提案
import evoharness.work.scheduling  # noqa: F401  # DE-40 资源租约表（F-N1：缺注册则生产 create_all 不建 work_resource_leases）
import evoharness.governance.models  # noqa: F401
import evoharness.knowledge.models  # noqa: F401
import evoharness.knowledge.skill_scope_events  # noqa: F401  # DE-06 撤权事件
import evoharness.channels.documents  # noqa: F401
import evoharness.channels.meeting_notes  # noqa: F401

SCHEMA_VERSION = 16
logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class MigrationContext:
    tenant_id: str = "default"
    owner_id: str = ""
    backup_keep: int = 3



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


def _copy_backup(path: Path, target_dir: Path, *, keep: int = 3) -> str | None:
    target_dir.mkdir(parents=True, exist_ok=True)
    keep = max(1, int(keep))
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


def _unique_single_column(conn, table: str, column: str) -> bool:
    for row in conn.execute(text(f"PRAGMA index_list({table})")).fetchall():
        name, unique = row[1], row[2]
        if not unique:
            continue
        cols = [info[2] for info in conn.execute(text(f'PRAGMA index_info("{name}")'))]
        if cols == [column]:
            return True
    return False


def _rebuild_twin_identities(conn) -> str:
    """旧 twin_identities（open_id 全局唯一）→ (tenant, app_id, open_id)。"""
    rows = conn.execute(text("SELECT * FROM twin_identities")).mappings().all()
    for row in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='twin_identities'")):
        name = row[0]
        if name and not str(name).startswith("sqlite_autoindex"):
            conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    conn.execute(text("ALTER TABLE twin_identities RENAME TO twin_identities_old"))
    from evoharness.models import TwinIdentity
    TwinIdentity.__table__.create(conn, checkfirst=True)
    target = [column.name for column in TwinIdentity.__table__.columns]
    now = utcnow().isoformat(sep=" ")
    for row in rows:
        payload = {key: row[key] for key in row.keys() if key in target}
        payload.setdefault("app_id", "default")
        for column in ("created_at", "updated_at"):
            if column in target and not payload.get(column):
                payload[column] = now
        placeholders = ", ".join(f":{key}" for key in payload)
        conn.execute(text(
            f"INSERT INTO twin_identities ({', '.join(payload)}) VALUES ({placeholders})"),
            payload)
    conn.execute(text("DROP TABLE twin_identities_old"))
    return f"rebuilt:{len(rows)}"


def _rebuild_sources_unique_per_owner(conn) -> str:
    """把 sources 的唯一键从 (chat, thread) 改成 (chat, thread, owner)。

    为什么必须重建表：SQLite 不支持修改既有 UNIQUE 约束，
    只能"建新表 → 拷数据 → 换名"。旧约束等于"一个群只能归属一个人"，
    正是"谁拉群谁负责"这个错误模型的技术基础（2026-11-11 用户纠正）。

    返回说明字符串，便于迁移报告引用。
    """
    if not _table_exists(conn, "sources"):
        return "skipped:no-sources"
    rows = conn.execute(text("SELECT * FROM sources")).mappings().all()
    cols = list(rows[0].keys()) if rows else [
        c["name"] for c in inspect(conn).get_columns("sources")
    ]
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)

    # 旧表的索引不会随 RENAME 消失，且索引名是全局唯一的：
    # 直接建新表会撞 "index ix_sources_tenant already exists"
    # （2026-11-11 实际踩到，那次迁移把 sources 清空了，靠 sources_old 才救回）。
    # 所以先显式删掉 sources 上的所有索引，再换表。
    for index_row in conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='sources'")
    ).fetchall():
        name = index_row[0]
        if name and not str(name).startswith("sqlite_autoindex"):
            conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))

    conn.execute(text("ALTER TABLE sources RENAME TO sources_old"))
    from evoharness.models import Source

    Source.__table__.create(conn, checkfirst=True)
    # 旧库可能有 NULL 的时间列，而新表的 NOT NULL 是模型层的默认值约束，
    # 直接拷会撞 IntegrityError。这里补当前时间兜底。
    from evoharness.clock import utcnow as _now

    now = _now().isoformat(sep=" ")
    for row in rows:
        payload = dict(row)
        for col in ("created_at", "updated_at"):
            if col in payload and payload[col] in (None, ""):
                payload[col] = now
        for col in ("tenant", "owner", "thread_id", "source_type", "discovery",
                    "read_identity", "reply_identity", "allowed_content_types",
                    "coverage_status"):
            if col in payload and payload[col] is None:
                payload[col] = "default" if col == "tenant" else ""
        conn.execute(
            text(f"INSERT INTO sources ({col_list}) VALUES ({placeholders})"),
            payload,
        )
    conn.execute(text("DROP TABLE sources_old"))
    return f"rebuilt:{len(rows)}-rows"


def backup_database(url: str, *, directory: str | Path | None = None, keep: int = 3) -> str | None:
    if not url.startswith("sqlite:///"):
        return None
    raw = url.split("sqlite:///", 1)[1].split("?", 1)[0]
    path = Path(raw)
    if not path.is_file() or raw in {":memory:", ""}:
        return None
    target_dir = Path(directory) if directory else path.parent
    return _copy_backup(path, target_dir, keep=keep)


def _backup_engine_database(engine: Engine, directory: str | Path | None, *, keep: int = 3) -> str | None:
    if engine.url.get_backend_name() != "sqlite" or not engine.url.database:
        return None
    path = Path(engine.url.database)
    if not path.is_file():
        return None
    target_dir = Path(directory) if directory else path.parent
    return _copy_backup(path, target_dir, keep=keep)


def migrate(engine: Engine, *, backup_dir: str | Path | None = None, context: MigrationContext | None = None) -> dict:
    settings = context or MigrationContext()
    backup = _backup_engine_database(engine, backup_dir, keep=settings.backup_keep)
    report = {"fromVersion": 1, "toVersion": SCHEMA_VERSION, "backup": backup, "changes": []}
    with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(bind=conn)
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

        # Extend the original Task; never create a second WorkItem status store.
        if _table_exists(conn, "tasks"):
            for column, ddl in {
                "workflow_kind": "VARCHAR(32) NOT NULL DEFAULT 'legacy_todo'",
                "org_id": "VARCHAR(64) NOT NULL DEFAULT ''",
                "space_id": "VARCHAR(64) NOT NULL DEFAULT ''",
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "goal": "TEXT NOT NULL DEFAULT ''",
                "acceptance_json": "TEXT NOT NULL DEFAULT '[]'",
                "dependencies_json": "TEXT NOT NULL DEFAULT '[]'",
                "assigned_position": "VARCHAR(64) NOT NULL DEFAULT ''",
                "next_action": "TEXT NOT NULL DEFAULT ''",
                "waiting_reason": "TEXT NOT NULL DEFAULT ''",
            }.items():
                _add(conn, "tasks", column, ddl)

        # v5：一个群允许多人各有一条来源（每人一个分身）。
        # 旧库的唯一键是 (chat, thread)，必须重建表才能放开。
        if current_version < 5:
            report["changes"].append(
                "sources-unique-per-owner:" + _rebuild_sources_unique_per_owner(conn)
            )

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
            # 待批归属：被提问者本人才能批准（2026-11-11 产品定位收窄到日常问答）。
            # 允许空串（历史数据与"无法判定归属"的草稿由管理员兜底）。
            _add(conn, "reply_drafts", "approver_id", "VARCHAR(64) NOT NULL DEFAULT ''")
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
                        "owner": settings.owner_id,
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
        # v7（R09 发送身份冻结）：outbox 记录真实发送身份，fallback 不伪装本人
        if _table_exists(conn, "outbox"):
            _add(conn, "outbox", "sent_identity", "VARCHAR(16) NOT NULL DEFAULT ''")
        # v8（N02/N04 动作意图与费用语义）：预算账本表扩展为「envelope 托管 +
        # 唯一 call_id 动作意图」——不另建技术台账，回执对账字段就位。
        if _table_exists(conn, "budget_reservations"):
            for column, ddl in {
                "kind": "VARCHAR(16) NOT NULL DEFAULT 'call'",
                "call_id": "VARCHAR(64) NOT NULL DEFAULT ''",
                "generation": "INTEGER NOT NULL DEFAULT 0",
                "grant_revision": "INTEGER NOT NULL DEFAULT 0",
                "request_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
                "intent_status": "VARCHAR(16) NOT NULL DEFAULT ''",
                "outcome": "VARCHAR(16) NOT NULL DEFAULT ''",
                "usage_status": "VARCHAR(16) NOT NULL DEFAULT ''",
                "escrow_tokens": "INTEGER NOT NULL DEFAULT 0",
                "provider_request_id": "VARCHAR(128) NOT NULL DEFAULT ''",
                "receipt_ref": "VARCHAR(128) NOT NULL DEFAULT ''",
                "checkpoint_ref": "VARCHAR(128) NOT NULL DEFAULT ''",
            }.items():
                _add(conn, "budget_reservations", column, ddl)
        # v9（BP-02 dryrun 边界）：outbox 记录投递模式；dryrun 意图在传输层
        # 拒绝（不调 transport），留痕 cancelled + dryrun_intent_blocked。
        if _table_exists(conn, "outbox"):
            _add(conn, "outbox", "delivery_mode", "VARCHAR(16) NOT NULL DEFAULT 'live'")
        # v12（DE-03 token 写入 CAS）：身份表加乐观版本，刷新/撤权不互相覆盖。
        if _table_exists(conn, "twin_identities"):
            _add(conn, "twin_identities", "token_version", "INTEGER NOT NULL DEFAULT 0")
        # v11（DE-09 身份作用域）：TwinIdentity 身份键扩展为
        # (tenant, app_id, open_id)；旧表 open_id 全局唯一，必须重建才能放开。
        if _table_exists(conn, "twin_identities"):
            only_open_id = _unique_single_column(conn, "twin_identities", "open_id")
            if "app_id" not in _columns(conn, "twin_identities") or only_open_id:
                report["changes"].append(
                    "twin-identities-scoped:" + _rebuild_twin_identities(conn))
        # v10（DE-13 可信检查回执）：检查执行事实与「被测 bundle」绑定。
        # legacy 回执保持空/0，严格策略拒绝而不是伪造统计。
        if _table_exists(conn, "work_artifact_receipts"):
            for column, ddl in {
                "exit_code": "INTEGER",
                "timed_out": "BOOLEAN NOT NULL DEFAULT 0",
                "executions": "INTEGER NOT NULL DEFAULT 0",
                "ran": "INTEGER NOT NULL DEFAULT 0",
                "bundle_sha256": "VARCHAR(64) NOT NULL DEFAULT ''",
                "node_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                # v13/v14（DE-13 测试集差集）：检查前基线/检查后现状/被删与被改写 node。
                "baseline_tests": "INTEGER NOT NULL DEFAULT 0",
                "current_tests": "INTEGER NOT NULL DEFAULT 0",
                "removed_node_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "modified_node_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                # v16（EVO-03 参数化变体保护）：真实收集的基线 node 全集及其来源。
                "baseline_node_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "baseline_node_source": "VARCHAR(16) NOT NULL DEFAULT ''",
            }.items():
                _add(conn, "work_artifact_receipts", column, ddl)
        conn.execute(
            text(
                "INSERT OR REPLACE INTO system_state (key, value, updated_at) "
                "VALUES ('schema_version', :version, :now)"
            ),
            {"version": str(SCHEMA_VERSION), "now": utcnow().isoformat(sep=" ")},
        )
        if _table_exists(conn, "knowledge_claims"):
            for column, ddl in {
                "aliases_json": "TEXT NOT NULL DEFAULT '[]'",
                "source_snapshots_json": "TEXT NOT NULL DEFAULT '[]'",
                "evidence_level": "VARCHAR(24) NOT NULL DEFAULT 'declared'",
            }.items():
                _add(conn, "knowledge_claims", column, ddl)
        report["changes"].append("migrated")
    report["ok"] = True
    return report


def ensure_schema(engine: Engine) -> dict:
    return migrate(engine)
