"""The documented OAuth command needs no plaintext intermediate file."""
import json

import pytest
from sqlalchemy import select

from app.core.database import session_scope
from app.models import TwinIdentity
from app.services import oauth
from scripts import oauth_server, save_twin_auth


def result():
    return {"ok": True, "accessToken": "private-access-token",
            "refreshToken": "private-refresh-token", "expiresIn": 3600,
            "user": {"openId": "ou_owner", "name": "owner"}}


def test_direct_save_verifies_owner_and_persists_encrypted(monkeypatch, settings, capsys):
    monkeypatch.setenv("FEISHU_TOKEN_SECRET", "offline-direct-save-test-key")
    monkeypatch.setattr(oauth_server, "serve", lambda **kwargs: result())
    calls = []
    def verify(token):
        calls.append(token)
        return {"ok": True, "openId": "ou_owner", "name": "verified owner"}
    monkeypatch.setattr(oauth, "fetch_user_info", verify)
    assert oauth_server.main(["--once", "--save"]) == 0
    output = capsys.readouterr().out
    assert "private-access-token" not in output and "private-refresh-token" not in output
    assert json.loads(output)["saved"] is True
    assert calls == ["private-access-token"]
    with session_scope() as session:
        row = session.scalar(select(TwinIdentity))
        assert row.tenant == settings.tenant_id and row.app_id == settings.feishu_app_id
        assert row.open_id == "ou_owner" and row.name == "verified owner"
        assert row.authorized and row.access_token_enc != "private-access-token"
        assert row.refresh_token_enc != "private-refresh-token"
        assert oauth.decrypt_token(row.access_token_enc) == "private-access-token"


def test_forged_result_cannot_save_other_users_token(monkeypatch, capsys):
    monkeypatch.setattr(oauth_server, "serve", lambda **kwargs: result())
    monkeypatch.setattr(oauth, "fetch_user_info", lambda token: {"ok": True, "openId": "ou_other"})
    assert oauth_server.main(["--once", "--save"]) == 1
    assert json.loads(capsys.readouterr().out)["stage"] == "save"
    with session_scope() as session:
        assert session.scalar(select(TwinIdentity)) is None


def test_save_command_rejects_plaintext_export_before_consent(monkeypatch):
    monkeypatch.setattr(oauth_server, "serve", lambda **kwargs: pytest.fail("must reject before consent"))
    with pytest.raises(SystemExit) as error:
        oauth_server.main(["--save", "--json-out", "tokens.json"])
    assert error.value.code == 2


def test_legacy_import_requires_explicit_path():
    with pytest.raises(SystemExit) as error:
        save_twin_auth.main([])
    assert error.value.code == 2
