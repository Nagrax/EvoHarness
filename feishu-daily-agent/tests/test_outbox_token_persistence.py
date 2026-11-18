"""Real dispatch adapters persist rotating tokens in the trusted app namespace."""
from app.core import database
from app.domain.outbox import _resolve_user_token
from app.services import identity, oauth


def save(session, app_id, token):
    return identity.save_authorization(session, tenant='tenant-a', app_id=app_id, result={
        'user': {'openId': 'ou_same'}, 'accessToken': token, 'refreshToken': 'refresh',
        'expiresIn': 7200, 'refreshExpiresIn': 10000})


def test_resolver_chooses_configured_app_and_commits_refresh(settings, monkeypatch):
    monkeypatch.setattr(settings, 'feishu_app_id', 'cli_current')
    monkeypatch.setattr(settings, 'tenant_id', 'tenant-a')
    monkeypatch.setattr(oauth, 'refresh_access_token', lambda token: {
        'ok': True, 'accessToken': 'new-current', 'refreshToken': 'new-refresh',
        'expiresIn': 7200, 'refreshExpiresIn': 10000})
    with database.session_scope() as session:
        current = save(session, 'cli_current', 'expired-current')
        current.expires_at = 0
        save(session, 'cli_other', 'other-token')
    result = _resolve_user_token(database.SessionLocal, {
        'twin_open_id': 'ou_same', '_delivery_scope': {'tenant': 'tenant-a'}})
    assert result == 'new-current'
    with database.session_scope() as session:
        current = identity.get_identity(session, 'ou_same', tenant='tenant-a', app_id='cli_current')
        other = identity.get_identity(session, 'ou_same', tenant='tenant-a', app_id='cli_other')
        assert oauth.decrypt_token(current.access_token_enc) == 'new-current'
        assert oauth.decrypt_token(current.refresh_token_enc) == 'new-refresh'
        assert oauth.decrypt_token(other.access_token_enc) == 'other-token'
        assert current.token_version == 2 and other.token_version == 1


def test_resolver_cannot_use_same_user_in_another_tenant(settings, monkeypatch):
    monkeypatch.setattr(settings, 'feishu_app_id', 'cli_current')
    with database.session_scope() as session:
        save(session, 'cli_current', 'private-token')
    assert _resolve_user_token(database.SessionLocal, {
        'twin_open_id': 'ou_same', '_delivery_scope': {'tenant': 'tenant-b'}}) == ''
