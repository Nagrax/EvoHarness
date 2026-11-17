"""共享 Harness 运行时不具备成员隔离，hosted 访问必须使用管理员身份。"""
import importlib
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient


def test_browser_token_is_only_forwarded_to_same_origin_api():
    html = (Path(__file__).resolve().parents[1] / "frontend/index.html").read_text(encoding="utf-8")
    helper = html.split("/* ═════════ 同源 API 认证 ═════════ */", 1)[1].split(
        "/* ═════════ icons ═════════ */", 1)[0]
    script = r"""
const assert=require('node:assert/strict');
const vm=require('node:vm');
let token='test-only-token';
const calls=[];
const context={Headers, URL, location:{href:'https://work.example/',origin:'https://work.example'},
  sessionStorage:{getItem:()=>token},fetch:(url,init)=>{calls.push({url,init});return Promise.resolve({ok:true})}};
vm.createContext(context);
vm.runInContext(HELPER,context);
context.apiFetch('/api/work/schema',{headers:{'Content-Type':'application/json'}});
context.apiFetch('https://external.example/api/work/schema');
context.apiFetch('/other');
token='';context.apiFetch('/api/history');
assert.equal(calls[0].init.headers.get('Authorization'),'Bearer test-only-token');
assert.equal(calls[0].init.headers.get('Content-Type'),'application/json');
for(const call of calls.slice(1))assert.equal(call.init.headers.get('Authorization'),null);
"""
    import json
    proc = subprocess.run(["node", "-e", script.replace("HELPER", json.dumps(helper))],
                          capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert proc.returncode == 0, proc.stderr
    assert "apiFetch('/api/chat'" in html
    assert "apiFetch('/api/work/op'" in html


@pytest.fixture()
def web_env(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_HOST", "0.0.0.0")
    monkeypatch.setenv("EVOHARNESS_HOSTED_TOKEN", "audit-root-token")
    import server.app as web
    web = importlib.reload(web)
    monkeypatch.setattr(web, "HOSTED_PRINCIPALS", {
        "audit-member-token": {"subject": "member", "org_ids": ["org"], "admin": False}})
    monkeypatch.setattr(web, "load_session", lambda _: {
        "openaiMessages": [{"role": "user", "content": "private-history-sentinel"}]})
    yield web
    monkeypatch.undo()
    importlib.reload(web)


@pytest.mark.parametrize("method,path,kwargs", [
    ("GET", "/api/history", {"params": {"session_id": "audit-session"}}),
    ("GET", "/api/skills", {}),
    ("GET", "/api/skills/tiers", {}),
    ("GET", "/api/skills/search", {}),
    ("GET", "/api/skills/search/detail", {"params": {"search_id": "audit"}}),
    ("GET", "/api/eval/report", {}),
    ("GET", "/api/eval/skills", {}),
    ("POST", "/api/chat", {"json": {"session_id": "audit-session", "message": "hello"}}),
    ("POST", "/api/skills/repair/suggest", {"json": {}}),
])
@pytest.mark.parametrize("token,status", [("", 401), ("audit-member-token", 403)])
def test_shared_runtime_requires_admin_before_any_data_or_model_access(
        web_env, method, path, kwargs, token, status):
    client = TestClient(web_env.app)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.request(method, path, headers=headers, **kwargs)
    assert response.status_code == status
    assert "private-history-sentinel" not in response.text
    assert response.json()["ok"] is False


def test_admin_can_read_shared_history_and_health_stays_public(web_env):
    client = TestClient(web_env.app)
    response = client.get("/api/history", params={"session_id": "audit-session"},
                          headers={"Authorization": "Bearer audit-root-token"})
    assert response.status_code == 200
    assert "private-history-sentinel" in response.text
    assert client.get("/api/health").status_code == 200


def test_local_single_user_keeps_existing_history_access(web_env, monkeypatch):
    monkeypatch.setattr(web_env, "DEPLOYMENT_MODE", "local-single-user")
    response = TestClient(web_env.app).get("/api/history", params={"session_id": "audit-session"})
    assert response.status_code == 200
    assert "private-history-sentinel" in response.text


@pytest.mark.parametrize("path", ["/api/history?session_id=audit-session", "/api/work/schema"])
def test_actual_exposed_bind_cannot_bypass_auth_with_local_configuration(web_env, monkeypatch, path):
    monkeypatch.setattr(web_env, "DEPLOYMENT_MODE", "local-single-user")
    client = TestClient(web_env.app, base_url="http://203.0.113.20")
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer audit-root-token"}).status_code == 200
