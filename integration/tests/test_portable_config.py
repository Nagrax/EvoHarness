"""Configuration must follow the selected checkout and root service settings."""
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from integration import config


def test_default_paths_belong_to_checkout(monkeypatch):
    monkeypatch.delenv("EVOHARNESS_INTEGRATION_DATA", raising=False)
    monkeypatch.delenv("EVOHARNESS_UPSTREAM_LEDGER", raising=False)
    code = (
        "from integration import config as c; "
        "assert c.DATA.is_relative_to(c.REPO); "
        "assert c.UPSTREAM_LEDGER.is_relative_to(c.REPO)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=config.REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_server_url_tracks_root_port_and_explicit_url():
    assert config.server_url({"PORT": "9011", "HOST": "0.0.0.0"}) == "http://127.0.0.1:9011"
    assert config.server_url({"EVOHARNESS_HOST": "::1", "PORT": "9011"}) == "http://[::1]:9011"
    assert config.server_url({"EVOHARNESS_SERVER_URL": "https://agent.example/prefix/", "PORT": "9011"}) == "https://agent.example/prefix"


def test_relative_paths_resolve_from_checkout(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_INTEGRATION_DATA", "var/integration")
    assert config._configured_path("EVOHARNESS_INTEGRATION_DATA", Path("unused")) == (config.REPO / "var/integration").resolve()


@pytest.mark.parametrize("value", ["file:///tmp/socket", "https://user:private@agent.example", "http://agent.example:wrong", "https://agent.example/?token=private"])
def test_server_url_rejects_invalid_or_secret_bearing_values(value):
    with pytest.raises(ValueError) as failure:
        config.server_url({"EVOHARNESS_SERVER_URL": value})
    assert value not in str(failure.value)
    assert "private" not in str(failure.value)


def test_database_path_uses_service_url(tmp_path):
    target = tmp_path / "team.db"
    assert config.feishu_database_path({"DATABASE_URL": "sqlite:///" + str(target)}) == target.resolve()
    assert config.feishu_database_path({}) == (config.FEISHU_AGENT / "data/agent.db").resolve()


@pytest.mark.parametrize("url", ["postgresql://user:private@db/team", "sqlite:///:memory:"])
def test_intake_rejects_unsupported_db_without_exposing_credentials(url):
    with pytest.raises(ValueError) as failure:
        config.feishu_database_path({"DATABASE_URL": url})
    assert url not in str(failure.value)
    assert "private" not in str(failure.value)


def test_envfile_quoting_and_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FEISHU_AGENT", tmp_path)
    (tmp_path / ".env").write_text('LLM_MODEL="quoted-model"\nFEISHU_OWNER_OPEN_ID="ou_file"\n', encoding="utf-8")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("FEISHU_OWNER_OPEN_ID", "ou_process")
    values = config.load_feishu_env()
    assert values["LLM_MODEL"] == "quoted-model"
    assert values["FEISHU_OWNER_OPEN_ID"] == "ou_process"
