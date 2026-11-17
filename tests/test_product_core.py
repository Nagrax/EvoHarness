"""One schema and one task writer across root and channel entrypoints."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from evoharness.application import Application
from evoharness.models import Base, Message, Source, Task
from evoharness.migrations import migrate
from evoharness.storage import normalize_database_url, sqlite_database_path


def test_native_sqlite_filename_is_not_url_encoded(tmp_path):
    target = tmp_path / "team database.db"
    value = "sqlite:///" + target.as_posix()
    normalized = normalize_database_url(value, base_dir=tmp_path)
    assert normalized == value
    assert sqlite_database_path(normalized, base_dir=tmp_path) == target.resolve()

def test_root_service_operates_existing_database_without_channel_sdk(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "product.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        source = Source(tenant="t", owner="alice", chat_id="chat", reply_allowed=False)
        session.add(source)
        session.flush()
        source_id = source.id
        session.add(Message(message_id="m", tenant="t", chat_id="chat", source_id=source_id,
                            content_text="prepare report", create_time=1))
    app = Application(factory)
    ids = app.create_from_extraction("m", tenant="t", owner="alice", source_id=source_id,
        payload={"tasks": [{"action": "prepare report", "assignee": "alice", "needs_confirmation": False}]})
    assert app.tasks(tenant="t", owner="alice")[0]["id"] == ids[0]
    app.task_status(ids[0], tenant="t", owner="alice", status="done")
    with factory() as session:
        assert session.get(Task, ids[0]).status == "done"
        assert len(list(session.scalars(select(Task)))) == 1
    assert app.tasks(tenant="t", owner="bob") == []

def test_core_migration_is_reentrant_and_preserves_ids(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "migration.db"))
    first = migrate(engine)
    second = migrate(engine)
    assert first["ok"] and second["ok"]
    assert "already-current" in second["changes"]

def test_v1_database_upgrade_extends_task_and_adds_organization_tables(tmp_path):
    """旧 v1 业务库升级：Task 原表补组织列，组织/Run 表并入同一业务库，不另建状态库。"""
    from sqlalchemy import text, inspect
    path = tmp_path / "legacy.db"
    engine = create_engine("sqlite:///" + str(path))
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, task_key VARCHAR(128),"
            " tenant VARCHAR(64) DEFAULT 'default', owner VARCHAR(64) DEFAULT '',"
            " chat_id VARCHAR(64), thread_id VARCHAR(64) DEFAULT '', title TEXT,"
            " action TEXT DEFAULT '', assignee_id VARCHAR(64) DEFAULT '',"
            " assignee_name VARCHAR(255) DEFAULT '', due_at DATETIME,"
            " due_text VARCHAR(64) DEFAULT '', status VARCHAR(24) DEFAULT 'needs_confirmation',"
            " confidence FLOAT DEFAULT 0.0, needs_confirmation BOOLEAN DEFAULT 1,"
            " evidence_json TEXT DEFAULT '[]', source_message_ids TEXT DEFAULT '[]',"
            " created_at DATETIME, updated_at DATETIME)"))
        conn.execute(text("CREATE TABLE system_state (key VARCHAR(64) PRIMARY KEY,"
                          " value VARCHAR(128), updated_at DATETIME)"))
        conn.execute(text("INSERT INTO tasks (task_key, tenant, owner, chat_id, title, status)"
                          " VALUES ('legacy-1', 'default', 'alice', 'chat', '旧待办', 'open')"))
        conn.execute(text("INSERT INTO system_state (key, value) VALUES ('schema_version', '1')"))
    report = migrate(engine, backup_dir=tmp_path / "backups")
    assert report["ok"]
    columns = set(inspect(engine).get_columns("tasks")[i]["name"]
                  for i in range(len(inspect(engine).get_columns("tasks"))))
    assert {"workflow_kind", "org_id", "space_id", "revision", "goal", "acceptance_json"} <= columns
    tables = set(inspect(engine).get_table_names())
    assert {"organizations", "business_spaces", "organization_units", "organization_positions",
            "organization_memberships", "employee_profiles", "position_assignments",
            "organization_events", "execution_runs", "work_artifacts"} <= tables
    with engine.begin() as conn:
        row = conn.execute(text("SELECT status, workflow_kind, revision FROM tasks")).fetchone()
        # 历史 open 待办保持普通待办语义，不被冒充为委派交付
        assert row == ("open", "legacy_todo", 1)
    engine.dispose()

def test_core_import_does_not_load_channel_or_open_database():
    import subprocess
    code = '''
import sys
class NoChannel:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "app" or fullname.startswith("app.") or fullname.startswith("lark_oapi"):
            raise RuntimeError("reverse channel dependency")
sys.meta_path.insert(0, NoChannel())
from evoharness.application import Application
assert "app" not in sys.modules
'''
    proc = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


def test_first_open_creates_sqlite_parent_and_reopen_preserves_database(tmp_path):
    target = tmp_path / "new workspace" / "data" / "org.db"
    app = Application.open("sqlite:///" + target.as_posix())
    try:
        with app.engine.begin() as conn:
            from sqlalchemy import text
            conn.execute(text("INSERT INTO system_state (key, value, updated_at) VALUES ('fixture', 'kept', CURRENT_TIMESTAMP)"))
        assert target.is_file()
    finally:
        app.engine.dispose()
    reopened = Application.open("sqlite:///" + target.as_posix())
    try:
        with reopened.engine.connect() as conn:
            assert conn.execute(text("SELECT value FROM system_state WHERE key='fixture'")).scalar() == "kept"
    finally:
        reopened.engine.dispose()
