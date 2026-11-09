"""user_access_token 全 headless 续期（无面板依赖，2026-11-08 打通）。

链路：esentutl 复制被锁的 IAB cookie 库 → DPAPI+AES-GCM 解密 feishu passport 会话
→ headless Edge 注入 cookie 静默走授权 → 本地回调捕获 code → 换 token 写回。
v2 token 端点不返回 refresh_token，因此每次过期都跑本脚本（引擎/cron 可直接调用）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

# 子进程一律不弹控制台窗口
CREATE_NO_WINDOW = 0x08000000

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = Path(r"D:/claudecode/scratch/oauth_user_token.json")
IAB_COOKIES_SRC = Path.home() / "AppData/Roaming/ZCode/session/Partitions/zcode-embedded-browser/Network/Cookies"
IAB_LOCAL_STATE_SRC = Path.home() / "AppData/Roaming/ZCode/session/Local State"
WORK = Path(r"D:/claudecode/scratch/iab-cookies")
COOKIES_JSON = WORK / "feishu_cookies.json"
DECRYPT_SCRIPT = Path(r"D:/claudecode/scratch/decrypt_iab.py")
NAV_SCRIPT = ROOT / "scripts" / "renew_navigate.mjs"
SCOPES = "im:message im:message.send_as_user im:chat im:chat:update"


def _env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


class Capture(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):
        if "code=" in self.path:
            params = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p)
            Capture.result = {"code": params.get("code", ""), "state": params.get("state", "")}
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h1>ok</h1>".encode())
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write("<h1>waiting</h1>".encode())

    def log_message(self, *a):
        pass


def _refresh_iab_cookie_snapshot() -> None:
    """esentutl 复制被占用的 cookie 库与 Local State（IAB 进程持锁也能复制）。"""
    WORK.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["cmd", "/c", "esentutl", "/y", str(IAB_COOKIES_SRC),
         "/d", str(WORK / "Cookies-copy"), "/o"],
        capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW,
    )
    # Local State 无锁（常规复制即可；esentutl 处理不了带空格路径时兜底）
    if IAB_LOCAL_STATE_SRC.exists():
        (WORK / "LocalState-copy").write_bytes(IAB_LOCAL_STATE_SRC.read_bytes())


def _decrypt_cookies() -> int:
    r = subprocess.run(
        [sys.executable, str(DECRYPT_SCRIPT)], capture_output=True, text=True, timeout=60,
        creationflags=CREATE_NO_WINDOW,
    )
    if not COOKIES_JSON.exists():
        raise RuntimeError(f"decrypt failed: {(r.stderr or r.stdout)[-200:]}")
    return len(json.loads(COOKIES_JSON.read_text(encoding="utf-8")))


def renew() -> dict:
    env = _env()
    state = "headless-" + uuid.uuid4().hex[:8]
    _refresh_iab_cookie_snapshot()
    n_cookies = _decrypt_cookies()
    auth_url = (
        "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
        f"?app_id={env['FEISHU_APP_ID']}"
        "&redirect_uri=" + urllib.parse.quote("http://localhost:18765/callback", safe="") +
        "&response_type=code&state=" + state +
        "&scope=" + urllib.parse.quote(SCOPES, safe="")
    )
    server = HTTPServer(("127.0.0.1", 18765), Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        out = subprocess.run(
            ["node", str(NAV_SCRIPT), auth_url, str(COOKIES_JSON)],
            capture_output=True, text=True, timeout=150, creationflags=CREATE_NO_WINDOW,
        )
        callback = None
        for line in (out.stdout or "").splitlines():
            if line.startswith("CALLBACK_URL="):
                callback = line.split("=", 1)[1]
        if not callback or "code=" not in callback:
            return {"ok": False, "error": f"no callback: {(out.stdout or out.stderr)[-200:]}"}
        params = dict(p.split("=", 1) for p in callback.split("?", 1)[1].split("&") if "=" in p)
        r = requests.post(
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": env["FEISHU_APP_ID"],
                "client_secret": env["FEISHU_APP_SECRET"],
                "code": params.get("code", ""),
                "redirect_uri": "http://localhost:18765/callback",
            },
            timeout=15,
        )
        d = r.json()
        body = d.get("data") or d
        access = body.get("access_token") or ""
        if not access:
            return {"ok": False, "error": f"exchange failed: code={d.get('code')} msg={d.get('msg')}"}
        old = {}
        if TOKEN_FILE.exists():
            old = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        old.update({
            "access_token": access,
            "refresh_token": body.get("refresh_token", ""),
            "expires_in": body.get("expires_in"),
            "scope": body.get("scope", ""),
            "renewedAt": time.strftime("%Y-%m-%dT%H:%M:%S+0800"),
            "renewedBy": "headless-cookie-injection",
        })
        TOKEN_FILE.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True, "scope": body.get("scope", ""), "cookies": n_cookies}
    finally:
        server.shutdown()


def main() -> int:
    result = renew()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
