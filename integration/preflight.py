"""飞书连接预检（L9046/L9051）：审批通过前验证就绪度，通过后秒级确认可用。

两级检查：
- static（无网络，永远安全）：.env 字段完整性、凭证格式（cli_/ou_）、
  LLM 配置、部署模式、white/black list 语法
- live（--live 才走真实网络）：用 App ID/Secret 换 tenant_access_token，
  验证凭证真实性，并探测 chat/message 两类 scope；--probe-send 额外探测发信
  scope（只发给无效 open_id，不会真正投递）。

输出 readiness dict；闭环报告引用 static 部分。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):  # 直跑 python integration/preflight.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integration.config import feishu_live, feishu_owner_target, load_feishu_env

BASE = "https://open.feishu.cn"
INVALID_OPEN_ID = "ou_00000000000000000000000000000000"
REQUIRED_SCOPES = [
    "im:message",
    "im:message.p2p_msg:readonly",
    "im:message.group_msg",
    "im:chat:readonly",
    "im:message:send_as_bot",
]
_SCOPE_RE = re.compile(r"required:\s*\[(.*?)\]", re.S)
# 权限已生效、只是探针故意用了无效 id 时的错误码/关键字（否则无法在只读安全
# 的前提下验证 scope）。这类返回说明 scope 通了，不是权限拒绝。
_SCOPE_OK_CODES = {99992402}
_SCOPE_OK_HINTS = (
    "invalid container_id",
    "not a valid {open_id}",
    "field_violations",
    "invalid request parameter",
)


def _scope_granted(probe: dict) -> bool:
    if probe.get("ok"):
        return True
    code = probe.get("code")
    if code is not None and int(code) in _SCOPE_OK_CODES:
        return True
    message = str(probe.get("msg") or "")
    return not probe.get("missing") and any(h in message for h in _SCOPE_OK_HINTS)


def _missing_scopes(message: str) -> list[str]:
    match = _SCOPE_RE.search(message or "")
    if not match:
        return []
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def static_checks(env: dict | None = None) -> dict:
    env = env or load_feishu_env()
    app_id = env.get("FEISHU_APP_ID", "")
    secret = env.get("FEISHU_APP_SECRET", "")
    owner = env.get("FEISHU_OWNER_OPEN_ID", "")
    chat_id = env.get("FEISHU_OWNER_CHAT_ID", "")
    email = env.get("FEISHU_OWNER_EMAIL", "")
    llm_key = env.get("LLM_API_KEY", "")
    mode = env.get("FEISHU_DEPLOYMENT_MODE", "offline")

    checks = {
        "appIdFormat": app_id.startswith("cli_"),
        "appSecretFilled": bool(secret) and not secret.startswith("offline"),
        # open_id 按应用隔离，可能要先从首条消息学到；有 chat_id/邮箱同样可投递。
        "ownerTargetPresent": bool(
            owner.startswith("ou_") or chat_id.startswith("oc_") or "@" in email
        ),
        "llmKeyFilled": bool(llm_key) and "your" not in llm_key,
        "llmBaseFilled": bool(env.get("LLM_BASE_URL", "")),
        "modeValid": mode in {"offline", "readonly_shadow", "single_approval", "whitelist_auto"},
        "matcherModeValid": (env.get("MATCHER_MODE", "keyword") or "keyword").lower()
        in {"embedding", "keyword"},
    }
    missing = [k for k, v in checks.items() if not v]
    ready = feishu_live(env) and checks["llmKeyFilled"] and checks["modeValid"]
    hints = {
        "appIdFormat": "App ID 应以 cli_ 开头（开发者后台→凭证与基础信息）",
        "appSecretFilled": "App Secret 未填或仍是占位值",
        "ownerTargetPresent": "至少配置 FEISHU_OWNER_CHAT_ID / FEISHU_OWNER_OPEN_ID / "
        "FEISHU_OWNER_EMAIL 之一（chat_id 最稳，open_id 跨应用会变）",
        "llmKeyFilled": "LLM_API_KEY 未填（DeepSeek/智谱等 OpenAI 兼容 key）",
        "llmBaseFilled": "LLM_BASE_URL 未填",
        "modeValid": f"未知部署模式 {mode}（offline/readonly_shadow/single_approval/whitelist_auto）",
        "matcherModeValid": "MATCHER_MODE 只能是 keyword 或 embedding",
    }
    return {
        "ready": ready,
        "checks": checks,
        "missing": missing,
        "hints": [hints[k] for k in missing],
        "mode": mode,
        "ownerTarget": list(feishu_owner_target(env)),
        "ownerBootstrap": (env.get("FEISHU_OWNER_BOOTSTRAP", "0") or "").strip()
        in {"1", "true", "True", "yes"},
    }


def _request(session, token: str, method: str, path: str, **kwargs) -> dict:
    response = session.request(
        method, BASE + path, headers={"Authorization": f"Bearer {token}"},
        timeout=12, **kwargs,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:200]}
    msg = str(body.get("msg") or body.get("error", {}).get("message") or "")
    code = body.get("code")
    return {
        "http": response.status_code,
        "code": code,
        "ok": code is not None and int(code) == 0,
        "msg": msg[:300],
        "missing": _missing_scopes(msg),
        "data": body.get("data") or {},
        "body": body,
    }


def live_check(env: dict | None = None, timeout: int = 12, *, probe_send: bool = False) -> dict:
    """真实换取 tenant_access_token，并探测 chat/message scope。"""
    env = env or load_feishu_env()
    app_id = env.get("FEISHU_APP_ID", "")
    secret = env.get("FEISHU_APP_SECRET", "")
    if not (
        app_id.startswith("cli_")
        and secret
        and not secret.startswith("offline")
    ):
        return {"ok": False, "stage": "credentials", "error": "App ID/Secret 未填"}
    try:
        import requests

        session = requests.Session()
        resp = session.post(
            BASE + "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": env["FEISHU_APP_ID"], "app_secret": env["FEISHU_APP_SECRET"]},
            timeout=timeout,
        )
        data = resp.json()
        code = int(data.get("code", -1))
        if code != 0 or not data.get("tenant_access_token"):
            return {"ok": False, "stage": "credentials", "code": code,
                    "msg": data.get("msg", "")}
        token = data["tenant_access_token"]

        bot = _request(session, token, "GET", "/open-apis/bot/v3/info")
        bot_info = (bot.get("body") or {}).get("bot") or (bot.get("data") or {}).get("bot") or {}
        chats = _request(session, token, "GET", "/open-apis/im/v1/chats?page_size=1")
        history = _request(
            session, token, "GET",
            "/open-apis/im/v1/messages?container_id_type=chat"
            "&container_id=oc_probe_invalid&page_size=1",
        )
        probes = {
            "bot": bot,
            "chatList": chats,
            "history": history,
        }
        if probe_send:
            probes["send"] = _request(
                session, token, "POST",
                "/open-apis/im/v1/messages?receive_id_type=open_id",
                json={
                    "receive_id": INVALID_OPEN_ID,
                    "msg_type": "text",
                    "content": json.dumps({"text": "scope probe"}, ensure_ascii=False),
                },
            )
        missing: list[str] = []
        for probe in probes.values():
            missing.extend(probe.get("missing") or [])
        missing = sorted(set(missing))
        return {
            "ok": bool(bot.get("ok")),
            "stage": "scopes",
            "bot": {
                "openId": bot_info.get("open_id", ""),
                "name": bot_info.get("app_name", ""),
                "activateStatus": bot_info.get("activate_status"),
            },
            "scopes": {
                "chatList": bool(chats.get("ok")),
                "history": _scope_granted(history),
                "send": _scope_granted(probes["send"]) if probe_send else None,
            },
            "missingScopes": missing,
            "permissionUrl": (
                f"{BASE}/app/{env['FEISHU_APP_ID']}/auth"
                f"?q={','.join(REQUIRED_SCOPES)}&op_from=openapi&token_type=tenant"
            ),
            "note": "凭证有效；scope 缺失时消息/会话能力不可用，详见 missingScopes",
        }
    except Exception as exc:  # noqa: BLE001 — 网络/凭据问题要显式可见
        return {"ok": False, "stage": "network", "error": f"{type(exc).__name__}: {exc}"[:160]}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    live = "--live" in sys.argv
    result = static_checks()
    if live:
        result["live"] = live_check(probe_send="--probe-send" in sys.argv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
