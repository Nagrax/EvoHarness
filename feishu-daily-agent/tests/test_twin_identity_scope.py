"""DE-09 身份作用域：同一 open_id 在两个租户下独立授权，未指定作用域不猜身份。"""
from app.core.database import session_scope
from app.services import identity

RESULT = {"user": {"openId": "ou_scoped_same"}, "accessToken": "a-token",
          "refreshToken": "r-token", "expiresIn": 3600}


def test_same_open_id_is_isolated_per_tenant():
    with session_scope() as session:
        first = identity.save_authorization(session, tenant="tenant-a",
                                            result=RESULT)
        second = identity.save_authorization(session, tenant="tenant-b",
                                             result=RESULT)
        assert first.id != second.id
        assert first.app_id == "default" and second.app_id == "default"
        assert identity.get_identity(session, "ou_scoped_same",
                                     tenant="tenant-a").id == first.id
        assert identity.get_identity(session, "ou_scoped_same",
                                     tenant="tenant-b").id == second.id


def test_unscoped_lookup_with_multiple_rows_fails_closed():
    with session_scope() as session:
        identity.save_authorization(session, tenant="tenant-c", result=RESULT)
        identity.save_authorization(session, tenant="tenant-d", result=RESULT)
        try:
            identity.get_identity(session, "ou_scoped_same")
        except ValueError as exc:
            assert "identity_ambiguous_scope" in str(exc)
        else:  # pragma: no cover - 明确失败
            raise AssertionError("未指定作用域时必须拒绝猜身份")


def test_app_id_is_part_of_the_identity_key():
    with session_scope() as session:
        app_a = identity.save_authorization(session, tenant="tenant-e",
                                            result=RESULT, app_id="app-a")
        app_b = identity.save_authorization(session, tenant="tenant-e",
                                            result=RESULT, app_id="app-b")
        assert app_a.id != app_b.id
        assert identity.get_identity(session, "ou_scoped_same", tenant="tenant-e",
                                     app_id="app-a").id == app_a.id


def test_scoped_token_and_state_lookup_use_app_id():
    with session_scope() as session:
        identity.save_authorization(session, tenant="tenant-g",
                                    app_id="app-live", result=RESULT)
        state = identity.authorization_state(session, "ou_scoped_same",
                                             tenant="tenant-g", app_id="app-live")
        assert state["authorized"] is True
        token = identity.usable_access_token(session, "ou_scoped_same",
                                             tenant="tenant-g", app_id="app-live")
        assert token == "a-token"


def test_scoped_revoke_only_targets_that_identity():
    with session_scope() as session:
        app_a = identity.save_authorization(session, tenant="tenant-h",
                                            app_id="app-a", result=RESULT)
        app_b = identity.save_authorization(session, tenant="tenant-h",
                                            app_id="app-b", result=RESULT)
        assert identity.revoke(session, "ou_scoped_same", tenant="tenant-h",
                               app_id="app-a") is True
        assert app_a.authorized is False
        assert app_b.authorized is True
