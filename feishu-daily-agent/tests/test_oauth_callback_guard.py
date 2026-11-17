"""Callbacks consume an issued challenge before any token exchange."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest

from scripts import oauth_server as callback


@pytest.fixture(autouse=True)
def reset_callback_state():
    callback.STATE_STORE.clear()
    callback.RESULT.clear()
    yield
    callback.STATE_STORE.clear()
    callback.RESULT.clear()


def request(path):
    handler = object.__new__(callback.Handler)
    handler.path = path
    handler.wfile = BytesIO()
    status = []
    handler.send_response = status.append
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.do_GET()
    return status[0], handler.wfile.getvalue()


@pytest.mark.parametrize("query", ["code=x", "code=x&state=unknown", "code=x&state=issued&state=other"])
def test_unknown_or_ambiguous_state_does_not_exchange_or_stop_server(monkeypatch, query):
    calls = []
    monkeypatch.setattr(callback.oauth, "exchange_code", lambda code: calls.append(code))
    callback.register_state("issued", expected_open_id="ou_expected")
    assert request("/oauth/callback?" + query)[0] == 400
    assert calls == []
    assert callback.RESULT == {}


def test_expired_state_rejected_before_exchange(monkeypatch):
    monkeypatch.setattr(callback.time, "monotonic", lambda: 10)
    callback.register_state("issued", expected_open_id="ou_expected", timeout=5)
    monkeypatch.setattr(callback.time, "monotonic", lambda: 15)
    monkeypatch.setattr(callback.oauth, "exchange_code", lambda code: pytest.fail("must not exchange"))
    assert request("/oauth/callback?code=x&state=issued")[0] == 400


def test_state_is_atomically_consumed_once():
    callback.register_state("issued", expected_open_id="ou_expected")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: callback.consume_state("issued"), range(8)))
    assert sum(value is not None for value in results) == 1


def test_wrong_user_is_not_accepted_or_exposed(monkeypatch):
    callback.register_state("issued", expected_open_id="ou_expected")
    monkeypatch.setattr(callback.oauth, "exchange_code", lambda code: {"ok": True, "accessToken": "test-token"})
    monkeypatch.setattr(callback.oauth, "fetch_user_info", lambda token: {"ok": True, "openId": "ou_other"})
    assert request("/oauth/callback?code=x&state=issued")[0] == 403
    assert callback.RESULT == {"ok": False, "error": "oauth_identity_mismatch"}


def test_valid_callback_replay_and_html_escaping(monkeypatch):
    callback.register_state("issued", expected_open_id="ou_expected")
    calls = []
    def exchange(code):
        calls.append(code)
        return {"ok": True, "accessToken": "test-token"}
    monkeypatch.setattr(callback.oauth, "exchange_code", exchange)
    monkeypatch.setattr(callback.oauth, "fetch_user_info", lambda token: {"ok": True, "openId": "ou_expected", "name": "<script>"})
    status, body = request("/oauth/callback?code=x&state=issued")
    assert status == 200
    assert b"<script>" not in body and b"&lt;script&gt;" in body
    assert callback.RESULT["ok"] is True
    assert request("/oauth/callback?code=x&state=issued")[0] == 400
    assert calls == ["x"]


@pytest.mark.parametrize("refresh_lifetime", [7200, None])
def test_refresh_rotation_updates_declared_expiry_and_preserves_omitted_expiry(monkeypatch, refresh_lifetime):
    from app.core.database import session_scope
    from app.services import identity
    monkeypatch.setattr(identity.time, "time", lambda: 1000)
    with session_scope() as session:
        saved = identity.save_authorization(session, tenant="default", result={
            "user": {"openId": "ou_expected"}, "accessToken": "old-test-token",
            "refreshToken": "old-refresh", "expiresIn": 1, "refreshExpiresIn": 500})
        saved.expires_at = 900
        response = {"ok": True, "accessToken": "new-test-token", "refreshToken": "rotated-refresh", "expiresIn": 3600}
        if refresh_lifetime is not None:
            response["refreshExpiresIn"] = refresh_lifetime
        monkeypatch.setattr(identity.oauth, "refresh_access_token", lambda token: response)
        assert identity.usable_access_token(session, "ou_expected") == "new-test-token"
        assert saved.refresh_expires_at == (1000 + refresh_lifetime if refresh_lifetime else 1500)
        assert identity.oauth.decrypt_token(saved.refresh_token_enc) == "rotated-refresh"


def test_refresh_response_retains_provider_refresh_expiry(monkeypatch):
    import requests
    from types import SimpleNamespace
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(json=lambda: {
        "access_token": "test-token", "refresh_token": "rotated-refresh",
        "expires_in": 3600, "refresh_token_expires_in": 7200}))
    result = callback.oauth.refresh_access_token("old-refresh")
    assert result["refreshExpiresIn"] == 7200
