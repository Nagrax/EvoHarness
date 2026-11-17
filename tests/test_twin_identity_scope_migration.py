"""DE-09 TwinIdentity 作用域迁移：旧 open_id 全局唯一 → (tenant, app_id, open_id)。"""
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine, inspect, text

from evoharness.migrations import SCHEMA_VERSION, migrate


def _legacy_db(path: Path) -> None:
    engine = create_engine("sqlite:///" + path.as_posix(), future=True)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE twin_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant VARCHAR(64) NOT NULL DEFAULT 'default',
                open_id VARCHAR(64) NOT NULL UNIQUE,
                union_id VARCHAR(64) NOT NULL DEFAULT '',
                name VARCHAR(128) NOT NULL DEFAULT '',
                authorized BOOLEAN NOT NULL DEFAULT 0,
                scope TEXT NOT NULL DEFAULT '',
                access_token_enc TEXT NOT NULL DEFAULT '',
                refresh_token_enc TEXT NOT NULL DEFAULT '',
                expires_at FLOAT,
                refresh_expires_at FLOAT,
                last_error TEXT NOT NULL DEFAULT '',
                authorized_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        conn.execute(text(
            "INSERT INTO twin_identities (tenant, open_id, authorized, created_at, updated_at) "
            "VALUES ('t1', 'ou_same', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))
    engine.dispose()


def test_legacy_twin_identities_are_rebuilt_with_scoped_key(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    engine = create_engine("sqlite:///" + db.as_posix(), future=True)
    report = migrate(engine)
    assert report["toVersion"] == SCHEMA_VERSION
    assert any(change.startswith("twin-identities-scoped:rebuilt:1")
               for change in report["changes"])
    columns = {column["name"] for column in inspect(engine).get_columns("twin_identities")}
    assert "app_id" in columns
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT tenant, app_id, open_id, authorized FROM twin_identities")).one()
        assert tuple(row) == ("t1", "default", "ou_same", 1)
        conn.execute(text(
            "INSERT INTO twin_identities (tenant, app_id, open_id, union_id, name, "
            "authorized, scope, access_token_enc, refresh_token_enc, last_error, "
            "token_version, created_at, updated_at) VALUES ('t2', 'default', "
            "'ou_same', '', '', 1, '', '', '', '', 0, '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00')"))
        count = conn.execute(text(
            "SELECT COUNT(*) FROM twin_identities WHERE open_id='ou_same'")).scalar()
        assert count == 2
    engine.dispose()


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "legacy2.db"
    _legacy_db(db)
    engine = create_engine("sqlite:///" + db.as_posix(), future=True)
    migrate(engine)
    second = migrate(engine)
    assert second["changes"] == ["already-current"]
    engine.dispose()
