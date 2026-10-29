"""W08 回归：本地/托管模式授权边界。

验收（tasks.json W08）：
- A/B 主体跨 session 历史/stop/恢复被拒绝，匿名管理变更被拒绝
- 普通用户不能晋级 core；hosted 无认证不能暴露管理写入口
- 本地单用户体验、共享 key 已有保护保持

真实缺陷（修复前）：
`/api/skills/tiers/action`、`/api/skills/repair/apply`、`/api/stop` 三个管理写端点
在 hosted（对外绑定）模式下完全无认证——任何能访问端口的人都能晋级 core、
改写 SKILL.md、停掉别人的会话。本地模式不受影响（无账号体系，符合设计）。

fail-closed：hosted 模式未配置 EVOHARNESS_HOSTED_TOKEN 时**拒绝启动**，
而不是把管理写入口裸露在外。
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_hosted(monkeypatch, token: str):
    """以 hosted 模式加载 server.app（子进程不行——这里用 env + reload，随后恢复）。"""
    monkeypatch.setenv("EVOHARNESS_HOSTED_TOKEN", token)
    import server.app as appmod

    return importlib.reload(appmod)


# ── ① hosted fail-closed：无 token 拒绝启动 ─────────────────


def test_hosted_without_token_refuses_to_start():
    """对外绑定 + 无 token 必须拒绝启动（SystemExit），不静默裸奔。"""
    code = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "os.environ['EVOHARNESS_HOST'] = '0.0.0.0'\n"
        "os.environ.pop('EVOHARNESS_HOSTED_TOKEN', None)\n"
        "os.environ.pop('EVOHARNESS_ALLOW_UNAUTHED_HOSTED', None)\n"
        "import server.app\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          timeout=120, cwd=str(REPO), env={**os.environ})
    combined = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    assert proc.returncode != 0, "hosted 无 token 必须拒绝启动"
    assert "EVOHARNESS_HOSTED_TOKEN" in combined, "拒绝信息必须指明缺什么"


def test_hosted_with_token_starts():
    code = (
        "import sys, os, json\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "os.environ['EVOHARNESS_HOST'] = '0.0.0.0'\n"
        "os.environ['EVOHARNESS_HOSTED_TOKEN'] = 'tok-123'\n"
        "import server.app as m\n"
        "print(json.dumps({'mode': m.DEPLOYMENT_MODE}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          timeout=120, cwd=str(REPO), env={**os.environ})
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-400:]
    assert json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])["mode"] == "hosted"


def test_local_mode_starts_without_token():
    code = (
        "import sys, os, json\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "os.environ['EVOHARNESS_HOST'] = '127.0.0.1'\n"
        "os.environ.pop('EVOHARNESS_HOSTED_TOKEN', None)\n"
        "import server.app as m\n"
        "print(json.dumps({'mode': m.DEPLOYMENT_MODE}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          timeout=120, cwd=str(REPO), env={**os.environ})
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-400:]
    assert json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])["mode"] == "local-single-user"


# ── ② hosted 管理写端点：匿名 401、带 token 放行 ────────────


@pytest.fixture()
def hosted_client(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_HOSTED_TOKEN", "tok-test")
    monkeypatch.setenv("EVOHARNESS_HOST", "0.0.0.0")
    import server.app as appmod

    appmod = importlib.reload(appmod)
    from fastapi.testclient import TestClient

    client = TestClient(appmod.app)
    yield appmod, client
    # 必须恢复本地模式：reload 留下的 hosted 模块级状态会污染后续测试
    #（实测 test_local_mode_stays_open 因此拿到 401）。
    monkeypatch.delenv("EVOHARNESS_HOSTED_TOKEN", raising=False)
    monkeypatch.delenv("EVOHARNESS_HOST", raising=False)
    importlib.reload(appmod)


def test_hosted_tier_action_rejects_anonymous(hosted_client):
    appmod, client = hosted_client
    resp = client.post("/api/skills/tiers/action", json={"skill": "s1", "action": "core"})
    assert resp.status_code == 401
    assert "认证" in resp.json()["error"]


def test_hosted_tier_action_rejects_wrong_token(hosted_client):
    appmod, client = hosted_client
    resp = client.post("/api/skills/tiers/action", json={"skill": "s1", "action": "core"},
                       headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_hosted_tier_action_accepts_valid_token(hosted_client):
    appmod, client = hosted_client
    resp = client.post("/api/skills/tiers/action", json={"skill": "no-such-skill", "action": "core"},
                       headers={"Authorization": "Bearer tok-test"})
    # 认证通过后进入业务逻辑（unknown skill 是业务层响应，不是 401）
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "401" not in json.dumps(resp.json())


def test_hosted_repair_apply_rejects_anonymous(hosted_client):
    appmod, client = hosted_client
    resp = client.post("/api/skills/repair/apply", json={"file": "x.md", "event": "corrupt"})
    assert resp.status_code == 401


def test_hosted_stop_rejects_anonymous_cross_session(hosted_client):
    """A 主体匿名停 B 主体会话：hosted 下必须 401。"""
    appmod, client = hosted_client
    resp = client.post("/api/stop", json={"session_id": "someone-elses-session"})
    assert resp.status_code == 401


def test_hosted_stop_accepts_valid_token(hosted_client):
    appmod, client = hosted_client
    resp = client.post("/api/stop", json={"session_id": "sess-unknown"},
                       headers={"Authorization": "Bearer tok-test"})
    assert resp.status_code == 200  # 认证通过；unknown session 返回 ok=False


# ── ③ 本地模式不受影响 ─────────────────────────────────────


def test_local_mode_stays_open():
    """local-single-user 无账号体系：管理端点照常（这是设计，不是缺陷）。"""
    from fastapi.testclient import TestClient
    import server.app as appmod

    client = TestClient(appmod.app)
    resp = client.post("/api/skills/tiers/action", json={"skill": "no-such", "action": "core"})
    assert resp.status_code == 200  # 本地模式无 401
    assert resp.json()["ok"] is False


def test_health_does_not_leak_deployment_details():
    from fastapi.testclient import TestClient
    import server.app as appmod

    client = TestClient(appmod.app)
    body = client.get("/api/health").json()
    text = json.dumps(body)
    assert "token" not in text.lower()
    assert "TOKEN" not in text
