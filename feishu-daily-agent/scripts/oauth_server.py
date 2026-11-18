"""OAuth 回调服务：接收飞书授权码并换取 user_access_token。

单独起在 8791（与开放平台登记的重定向 URL 一致），不侵入主服务：
主服务跑长连接，这个只处理一次性的授权回调，失败也不影响消息收发。

用法：
    python scripts/oauth_server.py          # 前台运行，浏览器完成授权后回车退出
    python scripts/oauth_server.py --once   # 收到一次授权后自动退出

授权地址会打印出来，复制到浏览器（或由调用方直接打开）。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import oauth  # noqa: E402

STATE_STORE: dict[str, dict] = {}
STATE_LOCK = threading.Lock()
RESULT: dict = {}


def register_state(state: str, *, expected_open_id: str, timeout: int = 180) -> None:
    if not state or not expected_open_id:
        raise ValueError("OAuth requires a state and expected user identity")
    with STATE_LOCK:
        STATE_STORE[state] = {"expires_at": time.monotonic() + timeout,
                              "expected_open_id": expected_open_id}


def consume_state(state: str) -> dict | None:
    with STATE_LOCK:
        record = STATE_STORE.pop(state, None)
    if not record or time.monotonic() >= record["expires_at"]:
        return None
    return record


def _page(title: str, body: str) -> bytes:
    return (
        f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>body{{font-family:-apple-system,"
        f"'Segoe UI',Microsoft YaHei,sans-serif;max-width:620px;margin:80px auto;"
        f"line-height:1.7;color:#1f2329}}.ok{{color:#00875a}}.err{{color:#cb2634}}"
        f"</style></head><body>{body}</body></html>"
    ).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        if parsed.path != "/oauth/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        error = (params.get("error") or [""])[0]

        record = consume_state(state) if len(params.get("state", [])) == 1 else None
        if record is None:
            self.send_response(400)
            self.end_headers()
            return

        if error:
            RESULT.update(ok=False, error=error)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_page("授权失败", f"<h2 class='err'>授权被拒绝</h2><p>{escape(error)}</p>"))
            return
        if not code:
            RESULT.update(ok=False, error="missing_code")
            self.send_response(400)
            self.end_headers()
            return

        result = oauth.exchange_code(code)
        if result.get("ok"):
            info = oauth.fetch_user_info(result["accessToken"])
            if not info.get("ok") or info.get("openId") != record["expected_open_id"]:
                RESULT.update(ok=False, error="oauth_identity_mismatch")
                self.send_response(403)
                self.end_headers()
                return
            RESULT.update(result, user=info, state=state)
            body = (
                "<h2 class='ok'>授权成功 ✅</h2>"
                f"<p>已获得 <b>{escape(info.get('name') or '用户')}</b> 的授权"
                f"（open_id {escape(info.get('openId', '')[-8:])}）。</p>"
                "<p>可以关闭这个页面，回到终端继续。</p>"
            )
            self.send_response(200)
        else:
            RESULT.update(result)
            body = (
                "<h2 class='err'>换取 token 失败</h2>"
                f"<p>code={escape(str(result.get('code')))} {escape(str(result.get('error', '')))}</p>"
            )
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_page("授权结果", body))

    def log_message(self, *args) -> None:
        pass


def serve(*, once: bool, timeout: int = 180, expected_open_id: str | None = None) -> dict:
    port = 8791
    try:
        from app.core.config import get_settings

        port = int(getattr(get_settings(), "oauth_callback_port", 8791))
    except Exception:  # noqa: BLE001
        pass

    url, state = oauth.build_authorize_url()
    from app.core.config import get_settings
    register_state(state, expected_open_id=expected_open_id or get_settings().feishu_owner_open_id,
                   timeout=timeout)
    RESULT.clear()
    print("=" * 70)
    print("数字分身授权 —— 请在浏览器打开下面的链接并点「授权」：")
    print()
    print(url)
    print()
    print(f"（回调地址：{oauth.redirect_uri() or '未配置！先设 FEISHU_REDIRECT_URI'}）")
    print("=" * 70)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if once:
            deadline = time.time() + timeout
            while time.time() < deadline and not RESULT:
                time.sleep(0.3)
        else:
            input("按回车结束授权服务…")
    finally:
        server.shutdown()
        server.server_close()
        with STATE_LOCK:
            STATE_STORE.pop(state, None)
    return RESULT


def save_authorization(result: dict, *, expected_open_id: str | None = None) -> dict:
    """Verify the token's owner again, then store it encrypted in the product DB."""
    from app.core.config import get_settings

    settings = get_settings()
    expected = (expected_open_id or settings.feishu_owner_open_id or "").strip()
    supplied = (result.get("user") or {}).get("openId")
    token = result.get("accessToken")
    if (not result.get("ok") or not expected or supplied != expected
            or not isinstance(token, str) or not token.strip()
            or float(result.get("expiresIn") or 0) <= 0):
        raise ValueError("oauth_identity_or_token_invalid")
    verified = oauth.fetch_user_info(token)
    if not verified.get("ok") or verified.get("openId") != expected:
        raise ValueError("oauth_identity_mismatch")

    from app.core.database import engine, session_scope
    from app.services import identity
    from evoharness.migrations import migrate

    if engine.url.get_backend_name() == "sqlite" and engine.url.database not in {
        None, "", ":memory:"
    }:
        Path(engine.url.database).parent.mkdir(parents=True, exist_ok=True)
    migrate(engine)
    with session_scope() as session:
        identity.save_authorization(
            session, tenant=settings.tenant_id, app_id=settings.feishu_app_id,
            result={**result, "user": verified},
        )
    return {"saved": True, "openIdLast8": expected[-8:]}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="收到一次授权后退出")
    parser.add_argument("--json-out", default="", help="把结果写入 JSON 文件")
    parser.add_argument("--save", action="store_true", help="验证本人身份后直接加密落库，不生成明文 token 文件")
    parser.add_argument("--expected-open-id", default=None, help="本次授权用户；默认配置的 owner")
    args = parser.parse_args(argv)
    if args.save and args.json_out:
        parser.error("--save 与 --json-out 不可同时使用")

    result = serve(once=args.once, expected_open_id=args.expected_open_id)
    if args.save and result.get("ok"):
        try:
            result = {**result, **save_authorization(
                result, expected_open_id=args.expected_open_id)}
        except Exception as exc:
            result = {"ok": False, "stage": "save", "error": type(exc).__name__}
    printable = dict(result)
    if printable.get("accessToken"):
        printable["accessToken"] = "…（已省略）"
    if printable.get("refreshToken"):
        printable["refreshToken"] = "…（已省略）"
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    if args.json_out and result.get("ok"):
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
