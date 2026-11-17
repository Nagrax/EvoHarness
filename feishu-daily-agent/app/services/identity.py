"""数字分身身份仓库：授权落库、按需续期、提供可用的 user_access_token。

设计要点
--------
- 授权是**人的属性**，一人一条记录；不影响 Source（会话属性）。
- token 加密存储，库里没有明文。
- 取 token 时按需刷新（留 120 秒余量），刷新失败**不让调用方崩**，
  而是返回 None + 更新 `last_error`，由上层降级为机器人身份并提示重新授权。
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.services import oauth

logger = logging.getLogger(__name__)


def get_identity(session: Session, open_id: str, *, tenant: str = "",
                 app_id: str = ""):
    """按身份键取授权；给了 tenant/app 就精确到作用域。

    未给作用域且命中多条时抛 identity_ambiguous_scope——宁可见失败，
    也不跨租户猜一个身份（DE-09）。
    """
    from app.models import TwinIdentity

    open_id = (open_id or "").strip()
    if not open_id:
        return None
    query = select(TwinIdentity).where(TwinIdentity.open_id == open_id)
    if tenant:
        query = query.where(TwinIdentity.tenant == tenant)
    if app_id:
        query = query.where(TwinIdentity.app_id == app_id)
    rows = list(session.scalars(query))
    if not rows:
        return None
    if len(rows) > 1:
        raise ValueError("identity_ambiguous_scope")
    return rows[0]


def save_authorization(session: Session, *, tenant: str, result: dict,
                       app_id: str = "") -> object:
    """把一次成功的 OAuth 结果落库（加密存储；身份键 tenant+app+open_id）。"""
    from app.models import TwinIdentity

    user = result.get("user") or {}
    open_id = (user.get("openId") or "").strip()
    if not open_id:
        raise ValueError("OAuth 结果缺少 open_id")

    resolved_app = (app_id or "default").strip() or "default"
    identity = get_identity(session, open_id, tenant=tenant, app_id=resolved_app)
    if identity is None:
        identity = TwinIdentity(tenant=tenant, app_id=resolved_app,
                                open_id=open_id)
        session.add(identity)

    identity.union_id = user.get("unionId", "") or identity.union_id
    identity.name = user.get("name", "") or identity.name
    identity.authorized = True
    identity.authorized_at = utcnow()
    identity.scope = result.get("scope", "")
    identity.access_token_enc = oauth.encrypt_token(result.get("accessToken", ""))
    identity.refresh_token_enc = oauth.encrypt_token(result.get("refreshToken", ""))
    identity.expires_at = time.time() + float(result.get("expiresIn") or 0)
    identity.refresh_expires_at = (
        time.time() + float(result.get("refreshExpiresIn") or 0)
        if result.get("refreshExpiresIn")
        else None
    )
    identity.token_version = int(identity.token_version or 0) + 1
    identity.last_error = ""
    session.flush()
    logger.info("已保存数字分身授权：%s（%s）", identity.name or "?", open_id[-6:])
    return identity


def _commit_refreshed_tokens(session: Session, identity, expected_version: int, *,
                             access_token: str, refresh_token: str = "",
                             expires_in: int = 0,
                             refresh_expires_in: int | None = None) -> bool:
    """乐观 CAS 写回刷新结果：版本不符则失败（不覆盖较新的 token）。"""
    from sqlalchemy import update
    from app.models import TwinIdentity

    values = {
        "access_token_enc": oauth.encrypt_token(access_token),
        "expires_at": time.time() + float(expires_in or 0),
        "token_version": int(expected_version) + 1,
        "last_error": "",
    }
    if refresh_token:
        values["refresh_token_enc"] = oauth.encrypt_token(refresh_token)
    if refresh_expires_in:
        values["refresh_expires_at"] = time.time() + float(refresh_expires_in)
    result = session.execute(
        update(TwinIdentity)
        .where(TwinIdentity.id == identity.id,
               TwinIdentity.token_version == int(expected_version))
        .values(**values))
    session.flush()
    return result.rowcount == 1


def usable_access_token(session: Session, open_id: str, *, tenant: str = "",
                        app_id: str = "") -> str | None:
    """取一个可用的 user_access_token；过期则自动续期。

    返回 None 表示"现在不能以本人身份发言"，调用方应降级为机器人身份。
    给了 tenant/app_id 就精确到身份键；不给则沿用单行兼容口径，
    多行时必须由调用方指明作用域（get_identity 会拒绝猜身份）。
    """
    identity = get_identity(session, open_id, tenant=tenant, app_id=app_id)
    if identity is None or not identity.authorized:
        return None

    version = int(identity.token_version or 0)
    token = oauth.decrypt_token(identity.access_token_enc or "")
    if token and not oauth.token_expired(identity.expires_at):
        return token

    refresh = oauth.decrypt_token(identity.refresh_token_enc or "")
    if not refresh:
        # 没有 refresh_token（未申请 offline_access），过期后只能重新授权
        if token:
            identity.last_error = "token 已过期且无 refresh_token，需重新授权"
            session.flush()
        return None

    if identity.refresh_expires_at and time.time() >= float(identity.refresh_expires_at):
        identity.last_error = "refresh_token 已过期，需重新授权"
        session.flush()
        return None

    result = oauth.refresh_access_token(refresh)
    if not result.get("ok"):
        identity.last_error = str(result.get("error") or "refresh_failed")[:300]
        session.flush()
        logger.warning("分身 %s token 续期失败：%s", open_id[-6:], identity.last_error)
        return None

    committed = _commit_refreshed_tokens(
        session, identity, version, access_token=result["accessToken"],
        refresh_token=str(result.get("refreshToken") or ""),
        expires_in=int(result.get("expiresIn") or 0),
        refresh_expires_in=(int(result["refreshExpiresIn"])
                            if result.get("refreshExpiresIn") else None))
    if not committed:
        # 另一个刷新者已写入更新版本：用它的 token，绝不覆盖回去
        session.expire(identity)
        fresh = session.get(type(identity), identity.id)
        token = oauth.decrypt_token(fresh.access_token_enc or "")
        if token and not oauth.token_expired(fresh.expires_at):
            return token
        fresh.last_error = "token_refresh_conflict"
        session.flush()
        return None
    return result["accessToken"]


def authorization_state(session: Session, open_id: str, *, tenant: str = "",
                        app_id: str = "") -> dict:
    """给面板/日报用的状态摘要（不泄露 token）。"""
    identity = get_identity(session, open_id, tenant=tenant, app_id=app_id)
    if identity is None:
        return {"authorized": False, "reason": "not_registered"}
    return {
        "authorized": bool(identity.authorized),
        "name": identity.name,
        "openId": identity.open_id,
        "expiresAt": identity.expires_at,
        "expired": oauth.token_expired(identity.expires_at),
        "hasRefresh": bool(identity.refresh_token_enc),
        "lastError": identity.last_error,
    }


def revoke(session: Session, open_id: str, *, reason: str = "",
           tenant: str = "", app_id: str = "") -> bool:
    """撤回授权（本人退订或管理员操作）。给了作用域就只撤该身份。"""
    identity = get_identity(session, open_id, tenant=tenant, app_id=app_id)
    if identity is None:
        return False
    identity.authorized = False
    identity.access_token_enc = ""
    identity.refresh_token_enc = ""
    identity.expires_at = None
    identity.token_version = int(identity.token_version or 0) + 1
    identity.last_error = reason[:300]
    session.flush()
    return True
