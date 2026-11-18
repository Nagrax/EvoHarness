import pytest
from fastapi.testclient import TestClient

import server.app as web
from evoharness.__main__ import seed_organization
from evoharness.api import open_api


def test_anonymous_demo_serves_the_seeded_organization(tmp_path, monkeypatch):
    api = open_api("sqlite:///" + (tmp_path / "demo.db").as_posix())
    try:
        with api._app.transaction() as session:
            seed_organization(session)
        monkeypatch.setattr(web, "_get_work_api", lambda: api)
        monkeypatch.setattr(web, "PUBLIC_DEMO", True)
        with TestClient(web.app) as client:
            health = client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["public_demo"] is True
            work = client.get("/api/work/view?tenant=local&owner=founder&org=org-demo")
            assert work.json()["ok"] and len(work.json()["data"]["buckets"]["pending"]) == 2
            assert client.get("/").status_code == 200
    finally:
        api._app.engine.dispose()


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/api/chat"),
    ("POST", "/api/work/op"),
    ("POST", "/api/stop"),
    ("POST", "/api/skills/repair/apply"),
    ("GET", "/api/history?session_id=private"),
    ("GET", "/docs"),
])
def test_public_demo_denies_mutations_and_shared_state(monkeypatch, method, path):
    monkeypatch.setattr(web, "PUBLIC_DEMO", True)
    with TestClient(web.app) as client:
        response = client.request(method, path, headers={"Authorization": "Bearer untrusted"})
        assert response.status_code == 403
        assert response.json()["reason_code"] == "public_demo_read_only"
