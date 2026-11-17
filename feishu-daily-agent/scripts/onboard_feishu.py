"""飞书接入向导：凭证/权限自检、会话发现、owner 回填。

用法（在 feishu-daily-agent 目录）：
    python scripts/onboard_feishu.py --check
    python scripts/onboard_feishu.py --check --probe-send
    python scripts/onboard_feishu.py --discover
    python scripts/onboard_feishu.py --discover --apply

设计原则：
- 只读探测优先；``--probe-send`` 才验证发信 scope，且只发给一个无效 open_id，
  飞书会先校验权限/参数，不会真正投递给任何人。
- 权限缺失时输出精确的 scope 列表和开放平台申请链接，方便一次补齐。
- 发现 p2p 会话后，``--apply`` 把 owner open_id / chat_id 写回 .env；
  之后重启服务即可按显式白名单运行，不再依赖首条消息引导。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.envfile import update_env_file  # noqa: E402

ENV_PATH = ROOT / ".env"
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


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip())
    for key in list(env) + ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def mask(value: str, keep: int = 6) -> str:
    value = value or ""
    return value if len(value) <= keep else f"...{value[-keep:]}"


def missing_scopes(message: str) -> list[str]:
    match = _SCOPE_RE.search(message or "")
    if not match:
        return []
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def _request(session, token: str, method: str, path: str, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    response = session.request(
        method, BASE + path, headers=headers, timeout=20, **kwargs
    )
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:300]}
    msg = str(body.get("msg") or body.get("error", {}).get("message") or "")
    return {
        "http": response.status_code,
        "code": body.get("code"),
        "msg": msg[:400],
        "missing": missing_scopes(msg),
        "data": body.get("data") or {},
        "body": body,
    }


def tenant_token(session, app_id: str, app_secret: str) -> str:
    response = session.post(
        BASE + "/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=20,
    )
    body = response.json()
    if int(body.get("code", -1)) != 0 or not body.get("tenant_access_token"):
        raise RuntimeError(f"换取 tenant_access_token 失败: {body.get('code')} {body.get('msg')}")
    return body["tenant_access_token"]


def _ok(result: dict) -> bool:
    code = result.get("code")
    return code is not None and int(code) == 0


# 权限已生效但参数本身非法时的错误码/关键字：这类返回说明 scope 已经通了，
# 只是探针故意用了无效的 id（否则无法在只读安全的前提下验证发信权限）。
_SCOPE_OK_CODES = {99992402}  # invalid parameter
_SCOPE_OK_HINTS = (
    "invalid container_id",
    "not a valid {open_id}",
    "field_violations",
    "invalid request parameter",
)


def _scope_granted(result: dict) -> bool:
    """scope 是否已生效：成功，或仅因参数非法而失败（不是权限拒绝）。"""
    if _ok(result):
        return True
    message = str(result.get("msg") or "")
    if not result.get("missing") and any(h in message for h in _SCOPE_OK_HINTS):
        return True
    return False


def _chat_candidates(items: list[dict]) -> list[dict]:
    candidates = []
    for item in items:
        chat_id = str(item.get("chat_id") or "")
        if not chat_id:
            continue
        p2p_target = str(item.get("p2p_target_id") or "")
        chat_type = str(item.get("chat_type") or "")
        chat_mode = str(item.get("chat_mode") or "")
        is_p2p = chat_type == "p2p" or bool(p2p_target) or chat_mode == "p2p"
        if not is_p2p:
            continue
        candidates.append(
            {
                "chat_id": chat_id,
                "owner_open_id": p2p_target,
                "name": str(item.get("name") or ""),
                "chat_mode": chat_mode,
            }
        )
    return candidates


def check(env: dict[str, str], *, probe_send: bool = False) -> dict:
    app_id = env.get("FEISHU_APP_ID", "")
    app_secret = env.get("FEISHU_APP_SECRET", "")
    result: dict = {
        "ok": False,
        "appId": mask(app_id, 8),
        "env": str(ENV_PATH),
        "checks": {},
        "probes": {},
        "missingScopes": [],
        "nextActions": [],
    }
    if not app_id.startswith("cli_") or not app_secret or app_secret.startswith("offline"):
        result["checks"]["credentials"] = False
        result["nextActions"].append("先在 .env 填 FEISHU_APP_ID / FEISHU_APP_SECRET")
        return result
    result["checks"]["credentials"] = True

    import requests

    session = requests.Session()
    try:
        token = tenant_token(session, app_id, app_secret)
    except Exception as exc:  # noqa: BLE001
        result["probes"]["token"] = {"ok": False, "error": str(exc)[:300]}
        return result
    result["probes"]["token"] = {"ok": True}

    bot = _request(session, token, "GET", "/open-apis/bot/v3/info")
    bot_info = (
        (bot.get("data") or {}).get("bot")
        or (bot.get("body") or {}).get("bot")
        or {}
    )
    result["probes"]["bot"] = {
        "ok": _ok(bot),
        "openId": bot_info.get("open_id", ""),
        "name": bot_info.get("app_name", ""),
        "activateStatus": bot_info.get("activate_status"),
        "error": bot.get("msg") if not _ok(bot) else "",
    }

    chats = _request(session, token, "GET", "/open-apis/im/v1/chats?page_size=50")
    result["probes"]["chats"] = {
        "ok": _ok(chats),
        "missing": chats.get("missing", []),
        "error": chats.get("msg") if not _ok(chats) else "",
    }
    items = list((chats.get("data") or {}).get("items") or [])
    result["chatsSeen"] = len(items)
    result["p2pCandidates"] = [
        {
            "chatId": mask(c["chat_id"]),
            "ownerOpenId": mask(c["owner_open_id"]),
            "name": c["name"],
        }
        for c in _chat_candidates(items)
    ]

    history = _request(
        session,
        token,
        "GET",
        "/open-apis/im/v1/messages?container_id_type=chat"
        "&container_id=oc_probe_invalid&page_size=1",
    )
    result["probes"]["history"] = {
        "ok": _scope_granted(history),
        "missing": history.get("missing", []),
        "error": history.get("msg") if not _ok(history) else "",
    }

    if probe_send:
        send = _request(
            session,
            token,
            "POST",
            "/open-apis/im/v1/messages?receive_id_type=open_id",
            json={
                "receive_id": INVALID_OPEN_ID,
                "msg_type": "text",
                "content": json.dumps({"text": "scope probe"}, ensure_ascii=False),
            },
        )
        result["probes"]["send"] = {
            "ok": _scope_granted(send),
            "missing": send.get("missing", []),
            "error": send.get("msg") if not _ok(send) else "",
        }

    missing: list[str] = []
    for probe in result["probes"].values():
        missing.extend(probe.get("missing") or [])
    result["missingScopes"] = sorted(set(missing))
    result["permissionUrl"] = (
        f"{BASE}/app/{app_id}/auth?q={','.join(REQUIRED_SCOPES)}"
        "&op_from=openapi&token_type=tenant"
    )
    result["ok"] = (
        result["probes"]["bot"].get("ok")
        and result["probes"]["chats"].get("ok")
        and result["probes"]["history"].get("ok")
        and (not probe_send or result["probes"]["send"].get("ok"))
    )
    if not result["probes"]["chats"].get("ok"):
        result["nextActions"].append(
            "开放平台→权限管理：开通 im:chat:readonly（用于列举会话）"
        )
    if not result["probes"]["history"].get("ok"):
        result["nextActions"].append(
            "开放平台→权限管理：开通 im:message.p2p_msg:readonly / im:message.group_msg"
        )
    if probe_send and not result["probes"]["send"].get("ok"):
        result["nextActions"].append(
            "开放平台→权限管理：开通 im:message:send_as_bot（或 im:message）"
        )
    if result["missingScopes"]:
        result["nextActions"].append(f"权限申请链接：{result['permissionUrl']}")
    if result["ok"] and not result["p2pCandidates"]:
        result["nextActions"].append(
            "权限已就绪但还没有 p2p 会话：先在飞书里给机器人发一条消息，再跑 --discover"
        )
    return result


def discover(env: dict[str, str]) -> dict:
    app_id = env.get("FEISHU_APP_ID", "")
    secret = env.get("FEISHU_APP_SECRET", "")
    import requests

    session = requests.Session()
    token = tenant_token(session, app_id, secret)
    chats = _request(session, token, "GET", "/open-apis/im/v1/chats?page_size=50")
    items = list((chats.get("data") or {}).get("items") or [])
    candidates = _chat_candidates(items)
    return {
        "ok": _ok(chats),
        "chatsSeen": len(items),
        "p2pCandidates": candidates,
        "missingScopes": chats.get("missing", []),
        "error": chats.get("msg") if not _ok(chats) else "",
    }


def _merge_csv(current: str, values: list[str]) -> str:
    seen = [item.strip() for item in (current or "").split(",") if item.strip()]
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return ",".join(seen)


def apply_owner(env: dict[str, str], owner: str, chat_id: str) -> dict:
    if not owner.startswith("ou_"):
        raise ValueError("owner open_id 必须以 ou_ 开头")
    if not chat_id.startswith("oc_"):
        raise ValueError("chat_id 必须以 oc_ 开头")
    updates = {
        "FEISHU_OWNER_OPEN_ID": owner,
        "FEISHU_OWNER_CHAT_ID": chat_id,
        "FEISHU_CHAT_WHITELIST": _merge_csv(env.get("FEISHU_CHAT_WHITELIST", ""), [chat_id]),
        "FEISHU_REPLY_ALLOWED_CHATS": _merge_csv(
            env.get("FEISHU_REPLY_ALLOWED_CHATS", ""), [chat_id]
        ),
        "FEISHU_DAILY_REPORT_ENABLED": "1",
    }
    if not env.get("FEISHU_BOT_OPEN_ID"):
        try:
            import requests

            session = requests.Session()
            token = tenant_token(session, env["FEISHU_APP_ID"], env["FEISHU_APP_SECRET"])
            bot = _request(session, token, "GET", "/open-apis/bot/v3/info")
            bot_id = (
                (bot.get("data") or {}).get("bot")
                or (bot.get("body") or {}).get("bot")
                or {}
            ).get("open_id", "")
            if bot_id:
                updates["FEISHU_BOT_OPEN_ID"] = bot_id
        except Exception:  # noqa: BLE001 — 可选字段，失败不影响回填
            pass
    update_env_file(ENV_PATH, updates)
    return {"ok": True, "env": str(ENV_PATH), "updated": sorted(updates)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="凭证与权限自检（默认）")
    parser.add_argument("--discover", action="store_true", help="列举会话并给 p2p 候选")
    parser.add_argument("--apply", action="store_true", help="把 owner/chat 写回 .env")
    parser.add_argument("--owner", default="", help="显式 owner open_id（ou_ 开头）")
    parser.add_argument("--chat", default="", help="显式 chat_id（oc_ 开头）")
    parser.add_argument("--probe-send", action="store_true", help="探测发信 scope（不会真发给人）")
    parser.add_argument("--json-out", default="", help="把结果写入 JSON 文件")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    env = load_env(ENV_PATH)

    if args.discover:
        result = discover(env)
        if args.apply:
            candidates = result.get("p2pCandidates") or []
            owner = args.owner or (candidates[0]["owner_open_id"] if len(candidates) == 1 else "")
            chat = args.chat or (candidates[0]["chat_id"] if len(candidates) == 1 else "")
            if not owner or not chat:
                result["applyError"] = "需要唯一 p2p 候选，或显式 --owner/--chat"
                result["ok"] = False
            else:
                result["apply"] = apply_owner(env, owner, chat)
    else:
        result = check(env, probe_send=args.probe_send)

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
