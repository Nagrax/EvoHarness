"""DE-03 token 写入 CAS：两个刷新者不互相覆盖，撤权后的旧刷新不复活 token。"""
from app.core.database import SessionLocal
from app.services import identity

TENANT = "cas-tenant"


def _result(open_id: str) -> dict:
    return {"user": {"openId": open_id}, "accessToken": "old-access",
            "refreshToken": "old-refresh", "expiresIn": 1}


def _create(open_id: str) -> None:
    with SessionLocal() as session:
        identity.save_authorization(session, tenant=TENANT,
                                    result=_result(open_id))
        session.commit()


def test_stale_refresh_cannot_overwrite_newer_token():
    open_id = "ou_cas_refresh"
    _create(open_id)
    with SessionLocal() as session:
        stale = identity.get_identity(session, open_id, tenant=TENANT)
        stale_version = int(stale.token_version)
        stale_id = stale.id
    with SessionLocal() as session:
        first = identity.get_identity(session, open_id, tenant=TENANT)
        assert identity._commit_refreshed_tokens(
            session, first, stale_version, access_token="token-A",
            refresh_token="refresh-A", expires_in=3600) is True
        session.commit()
    class _Detached:
        id = stale_id
    with SessionLocal() as session:
        assert identity._commit_refreshed_tokens(
            session, _Detached(), stale_version, access_token="token-B",
            refresh_token="refresh-B", expires_in=3600) is False
        session.commit()
    with SessionLocal() as session:
        row = identity.get_identity(session, open_id, tenant=TENANT)
        assert row.token_version == stale_version + 1
        assert identity.oauth.decrypt_token(row.access_token_enc) == "token-A"
        assert identity.oauth.decrypt_token(row.refresh_token_enc) == "refresh-A"


def test_revoke_invalidates_inflight_refresh():
    open_id = "ou_cas_revoke"
    _create(open_id)
    with SessionLocal() as session:
        stale = identity.get_identity(session, open_id, tenant=TENANT)
        stale_version = int(stale.token_version)
        stale_id = stale.id
    with SessionLocal() as session:
        assert identity.revoke(session, open_id, reason="test revoke") is True
        session.commit()
    class _Detached:
        id = stale_id
    with SessionLocal() as session:
        assert identity._commit_refreshed_tokens(
            session, _Detached(), stale_version, access_token="resurrected",
            expires_in=3600) is False
        session.commit()
    with SessionLocal() as session:
        row = identity.get_identity(session, open_id, tenant=TENANT)
        assert row.authorized is False
        assert identity.oauth.decrypt_token(row.access_token_enc) == ""


def test_normal_refresh_increments_version(monkeypatch):
    open_id = "ou_cas_normal"
    _create(open_id)
    with SessionLocal() as session:
        current = identity.get_identity(session, open_id, tenant=TENANT)
        before = int(current.token_version)
        current.expires_at = 500          # 已过期，触发刷新
        session.commit()
        monkeypatch.setattr(identity.time, "time", lambda: 1000)
        monkeypatch.setattr(identity.oauth, "refresh_access_token",
                            lambda token: {"ok": True, "accessToken": "new-access",
                                           "refreshToken": "new-refresh",
                                           "expiresIn": 3600})
        assert identity.usable_access_token(session, open_id) == "new-access"
        session.commit()
    with SessionLocal() as session:
        row = identity.get_identity(session, open_id, tenant=TENANT)
        assert row.token_version == before + 1
        assert identity.oauth.decrypt_token(row.access_token_enc) == "new-access"
