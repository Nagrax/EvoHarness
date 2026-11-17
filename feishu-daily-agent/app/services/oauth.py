"""数字分身 OAuth 授权与 token 管理。

为什么用标准 OAuth 而不是抓 cookie
----------------------------------
旧的 `renew_user_token.py` 依赖本机浏览器 cookie 解密密文，
只能在作者本人的电脑上跑；同事机器上没有那些 cookie，方案无法落地。
标准 OAuth 是唯一可规模化的方式：

    authorize_url  →  用户在飞书点「授权」  →  回调拿 code  →  换 token

安全边界
--------
- token **只在服务端落库**，且用 Fernet 加密（密钥来自 `FEISHU_TOKEN_SECRET`，
  缺失时退回机器绑定的本地密钥文件，并明确告警）。
- 授权是**显式动作**：用户不点授权就永远没有 token，也就不可能以他的身份发言。
- `refresh_token` 负责续期；刷新失败标记该分身失效并通知本人，不静默降级。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"

# 数字分身需要：发消息（以用户身份）+ 读消息 + 群信息
DEFAULT_SCOPES = "im:message im:message.send_as_user im:chat:readonly offline_access"


def _settings():
    from app.core.config import get_settings

    return get_settings()


def redirect_uri() -> str:
    settings = _settings()
    return (getattr(settings, "feishu_redirect_uri", "") or "").strip()


def scopes() -> str:
    settings = _settings()
    return (getattr(settings, "feishu_oauth_scopes", "") or DEFAULT_SCOPES).strip()


def build_authorize_url(*, state: str | None = None) -> tuple[str, str]:
    """生成授权链接与 state（state 用于防 CSRF，调用方需存起来比对）。"""
    settings = _settings()
    state = state or secrets.token_urlsafe(16)
    query = {
        "app_id": settings.feishu_app_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "state": state,
        "scope": scopes(),
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(query)}", state


def exchange_code(code: str) -> dict:
    """用授权码换 user_access_token。返回 {ok, ...}，不抛异常给调用方。"""
    import requests

    settings = _settings()
    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.feishu_app_id,
        "client_secret": settings.feishu_app_secret,
        "code": code,
        "redirect_uri": redirect_uri(),
    }
    try:
        resp = requests.post(TOKEN_URL, json=payload, timeout=15)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — 网络问题要显式可见但不炸调用方
        return {"ok": False, "stage": "network", "error": f"{type(exc).__name__}: {exc}"}
    body = data.get("data") or data
    access = body.get("access_token") or ""
    if not access:
        return {
            "ok": False,
            "stage": "exchange",
            "code": data.get("code"),
            "error": str(data.get("error_description") or data.get("msg") or "")[:200],
        }
    return {
        "ok": True,
        "accessToken": access,
        "refreshToken": body.get("refresh_token", ""),
        "expiresIn": int(body.get("expires_in") or 0),
        "refreshExpiresIn": int(body.get("refresh_token_expires_in") or 0),
        "scope": body.get("scope", ""),
    }


def refresh_access_token(refresh_token: str) -> dict:
    """用 refresh_token 续期。"""
    import requests

    settings = _settings()
    payload = {
        "grant_type": "refresh_token",
        "client_id": settings.feishu_app_id,
        "client_secret": settings.feishu_app_secret,
        "refresh_token": refresh_token,
    }
    try:
        resp = requests.post(TOKEN_URL, json=payload, timeout=15)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "network", "error": f"{type(exc).__name__}: {exc}"}
    body = data.get("data") or data
    access = body.get("access_token") or ""
    if not access:
        return {
            "ok": False,
            "stage": "refresh",
            "code": data.get("code"),
            "error": str(data.get("error_description") or data.get("msg") or "")[:200],
        }
    return {
        "ok": True,
        "accessToken": access,
        "refreshToken": body.get("refresh_token", "") or refresh_token,
        "expiresIn": int(body.get("expires_in") or 0),
        "refreshExpiresIn": int(body.get("refresh_token_expires_in") or 0),
    }


def fetch_user_info(access_token: str) -> dict:
    """用 user_access_token 查本人信息（确认 token 可用、拿到 open_id）。"""
    import requests

    try:
        resp = requests.get(
            USER_INFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    body = data.get("data") or {}
    if int(data.get("code", -1)) != 0:
        return {"ok": False, "code": data.get("code"), "error": str(data.get("msg"))[:200]}
    return {
        "ok": True,
        "openId": body.get("open_id", ""),
        "unionId": body.get("union_id", ""),
        "name": body.get("name", ""),
        "email": body.get("email", ""),
    }


# ---------------- token 加密存储 ----------------

def _fernet():
    """构造 Fernet 实例。

    密钥优先取 `FEISHU_TOKEN_SECRET`（部署环境应显式提供）；
    缺失时生成/复用本地密钥文件，保证不把明文 token 写进库，
    同时明确告警——本地密钥文件不适合多机部署。
    """
    from cryptography.fernet import Fernet

    raw = (os.environ.get("FEISHU_TOKEN_SECRET") or "").strip()
    if raw:
        key = raw.encode() if len(raw) == 44 else base64.urlsafe_b64encode(
            __import__("hashlib").sha256(raw.encode()).digest()
        )
        return Fernet(key)

    key_file = Path(__file__).resolve().parents[2] / "data" / ".token_key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if key_file.is_file():
        key = key_file.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
        logger.warning(
            "FEISHU_TOKEN_SECRET 未配置，已生成本地密钥文件 %s。"
            "多机部署时必须改为显式配置，否则各机器无法互相解密。",
            key_file,
        )
    return Fernet(key)


def encrypt_token(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt_token(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except Exception as exc:  # noqa: BLE001 — 密钥变更/损坏时不能崩服务
        logger.error("token 解密失败（密钥可能已更换）：%s", exc)
        return ""


def token_expired(expires_at: float | None, *, slack_seconds: int = 120) -> bool:
    if not expires_at:
        return True
    return time.time() >= (float(expires_at) - slack_seconds)
