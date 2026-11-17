"""DE-02 跨入口统一配置解析回归。

同一 `--db`/`EVOHARNESS_DATABASE_URL` 在根 CLI、Web 服务与 Worker 装配得到
同一 ResolvedConfig；相对 SQLite 路径按调用方 base 解析；产物/输出根由库
位置派生；缺库给可读错误；诊断只报来源、不泄露密码；解析本身不创建文件。
"""
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from evoharness.config import DATABASE_ENV, resolve_config

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_db_wins_and_reports_source(tmp_path):
    config = resolve_config(db="sqlite:///explicit.db",
                            env={DATABASE_ENV: "sqlite:///env.db"},
                            base_dir=tmp_path)
    assert config.database_source == "arg"
    expected = (tmp_path / "explicit.db").resolve().as_posix()
    assert config.database_url == "sqlite:///" + expected
    assert config.artifacts_dir == (tmp_path / "explicit.db").resolve().parent / "api-artifacts"
    assert config.output_root == config.artifacts_dir / "runs"


def test_env_source_and_relative_path_resolution(tmp_path):
    config = resolve_config(env={DATABASE_ENV: "sqlite:///data/agent.db"},
                            base_dir=tmp_path)
    assert config.database_source == "env"
    expected = (tmp_path / "data" / "agent.db").resolve().as_posix()
    assert config.database_url == "sqlite:///" + expected
    assert config.artifacts_dir == (tmp_path / "data").resolve() / "api-artifacts"


def test_missing_database_is_readable_error(tmp_path):
    with pytest.raises(ValueError, match="database_not_configured"):
        resolve_config(env={}, base_dir=tmp_path)


def test_non_sqlite_url_has_no_derived_artifacts():
    config = resolve_config(db="postgresql+psycopg://u:p@localhost/db",
                            env={}, base_dir=ROOT)
    assert config.artifacts_dir is None
    assert config.output_root == ROOT / "runs"


def test_describe_hides_password():
    config = resolve_config(db="postgresql+psycopg://u:secret@localhost/db",
                            env={}, base_dir=ROOT)
    described = config.describe()
    assert "secret" not in described["database"]
    assert described["databaseSource"] == "arg"
    assert "database" in described and "outputRoot" in described


def test_resolution_does_not_create_files(tmp_path):
    resolve_config(env={DATABASE_ENV: "sqlite:///not-created.db"}, base_dir=tmp_path)
    assert not (tmp_path / "not-created.db").exists()


def test_root_cli_and_web_share_the_same_resolved_database(tmp_path, monkeypatch):
    db = tmp_path / "entry.db"
    monkeypatch.setenv(DATABASE_ENV, "sqlite:///" + db.as_posix())
    from evoharness.cli import _build_api
    cli_api = _build_api(SimpleNamespace(db="", artifacts=None))
    try:
        assert Path(cli_api._app.engine.url.database).resolve() == db.resolve()
        assert cli_api.artifacts_dir == db.resolve().parent / "api-artifacts"
    finally:
        cli_api._app.engine.dispose()

    import server.app as web
    previous = web._WORK_API
    web._WORK_API = None
    try:
        web_api = web._get_work_api()
        assert Path(web_api._app.engine.url.database).resolve() == db.resolve()
    finally:
        if web._WORK_API is not None:
            web._WORK_API._app.engine.dispose()
        web._WORK_API = previous


def test_worker_entry_uses_same_resolution(tmp_path, monkeypatch):
    db = tmp_path / "worker.db"
    monkeypatch.setenv(DATABASE_ENV, "sqlite:///" + db.as_posix())
    from evoharness.__main__ import _resolve_db
    resolved = _resolve_db(SimpleNamespace(db=""))
    assert resolved == "sqlite:///" + db.resolve().as_posix()
