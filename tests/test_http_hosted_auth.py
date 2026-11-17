"""R01 回归：hosted 模式 HTTP 入口统一鉴权与受信 Principal（审计 20261114）。

真实缺陷（修复前，独立审核探针 http_hosted_auth 全部复现）：
- /api/work/*（op/view/knowledge/org…）在 hosted（对外绑定）模式下完全无认证；
- GET /api/work/knowledge 把查询参数 owner_scope/include_private 当可见性依据，
  任何人可指定 owner_scope=alice&include_private=true 读取 alice 的私有知识；
- POST /api/work/op submit_approval 直接用正文 actor_id 作为批准方（身份伪造）。

修复语义：
- hosted 模式下所有 /api/work/* 端点先过统一 token 门（_work_gate）；
- 操作者身份（Principal）只来自受信 token→主体注册表（HOSTED_PRINCIPALS，
  可由 env EVOHARNESS_HOSTED_PRINCIPALS 或测试注入配置）；共享 HOSTED_TOKEN
  回落为管理员主体；
- 正文 actor_id 只作声明被校验（与受信主体不一致 → principal_actor_mismatch）；
- 知识可见集从 Principal 派生，owner_scope/include_private 参数不再作为
  可见性依据；私有 scope 只对授权主体可见；
- submit_approval 对目标做组织匹配 + owner/负责人/组织成员/管理员复核，
  跨组织/无成员关系一律拒绝。

全部离线（临时 SQLite + TestClient，不触网、不加载真实凭据）。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from evoharness.api import open_api  # noqa: E402
from evoharness.knowledge import service as knowledge  # noqa: E402

ALICE_CREATE_PARAMS = {
    "tenant": "tenant-A", "owner": "alice", "title": "synthetic-task",
    "goal": "synthetic-goal", "acceptance": [{"kind": "code"}],
    "workflow_kind": "delegated_delivery", "assignee_id": "alice",
    "org_id": "org-A",
}
PRIVATE_KNOWLEDGE_PARAMS = {"space": "space-A", "subject": "private-test",
                            "owner_scope": "alice", "include_private": "true"}


@pytest.fixture()
def hosted_env(monkeypatch, tmp_path):
    """hosted 模式 server.app + 受信 token 注册表 + 离线工作台数据。"""
    monkeypatch.setenv("EVOHARNESS_HOSTED_TOKEN", "tok-root")
    monkeypatch.setenv("EVOHARNESS_HOST", "0.0.0.0")
    import server.app as web

    web = importlib.reload(web)
    api = open_api("sqlite:///" + (tmp_path / "hosted-auth.db").as_posix(),
                   artifacts_dir=tmp_path / "artifacts")
    web._WORK_API = api
    web.HOSTED_PRINCIPALS.clear()
    web.HOSTED_PRINCIPALS.update({
        "tok-alice": {"subject": "alice", "org_ids": ["org-A"], "admin": False},
        "tok-bob": {"subject": "bob", "org_ids": ["org-A"], "admin": False},
        "tok-mallory": {"subject": "mallory", "org_ids": [], "admin": False},
        "tok-root2": {"subject": "root2", "org_ids": [], "admin": True},
    })
    with api._session() as session:
        knowledge.record_claim(session, org_id="org-A", space_id="space-A",
                               owner_scope="alice", subject="private-test",
                               value="synthetic-private-answer",
                               source_refs=["fixture://private"])
        knowledge.record_claim(session, org_id="org-A", space_id="space-A",
                               owner_scope="public", subject="private-test",
                               value="public-answer",
                               source_refs=["fixture://public"])
    from fastapi.testclient import TestClient

    client = TestClient(web.app)
    yield web, api, client
    # 恢复本地模式与空注册表：reload 留下的 hosted 模块级状态会污染后续测试
    web._WORK_API = None
    api._app.engine.dispose()
    web.HOSTED_PRINCIPALS.clear()
    monkeypatch.delenv("EVOHARNESS_HOSTED_TOKEN", raising=False)
    monkeypatch.delenv("EVOHARNESS_HOST", raising=False)
    importlib.reload(web)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _alice_decision_params(api, task_id: int, *, actor: str = "alice") -> dict:
    plan = api.run_request_payload(task_id)
    assert plan["ok"], plan
    return {"org_id": "org-A", "actor_id": actor, "target_type": "work_item",
            "target_id": str(task_id), "target_revision": plan["data"]["target_revision"],
            "payload": plan["data"]["payload"], "actions": ["work.execute"],
            "grantee_id": "alice"}


def _forged_approval_params(*, actor: str = "forged-owner") -> dict:
    """探针同款合成批准载荷（不依赖真实任务，负例走 401/主体校验即可）。"""
    return {"org_id": "org-A", "actor_id": actor, "target_type": "work_item",
            "target_id": "1", "target_revision": 1, "payload": {"synthetic": True},
            "actions": ["work.execute"], "grantee_id": "alice"}


# ── ① 探针 1 负例：hosted + 无 Authorization 头一律拒绝 ─────────────────────


def test_unauthenticated_create_work_item_rejected(hosted_env):
    web, api, client = hosted_env
    resp = client.post("/api/work/op", json={"op": "create_work_item",
                                             "params": ALICE_CREATE_PARAMS})
    assert resp.status_code == 401
    body = resp.json()
    assert body["ok"] is False
    assert body["deploymentMode"] == "hosted"


def test_unauthenticated_private_knowledge_read_rejected(hosted_env):
    web, api, client = hosted_env
    resp = client.get("/api/work/knowledge", params=PRIVATE_KNOWLEDGE_PARAMS)
    assert resp.status_code == 401
    assert resp.json()["ok"] is False
    # 拒绝响应里不得泄漏私有知识正文
    assert "synthetic-private-answer" not in resp.text


def test_unauthenticated_forged_actor_approval_rejected(hosted_env):
    web, api, client = hosted_env
    params = _forged_approval_params(actor="forged-owner")
    resp = client.post("/api/work/op", json={"op": "submit_approval", "params": params})
    assert resp.status_code == 401
    assert resp.json()["ok"] is False


def test_unauthenticated_read_view_and_org_endpoints_rejected(hosted_env):
    web, api, client = hosted_env
    for path, params in [("/api/work/view", {"tenant": "tenant-A", "owner": "alice"}),
                         ("/api/work/item", {"task_id": 1}),
                         ("/api/work/evidence", {"task_id": 1}),
                         ("/api/work/approvals", {"org": "org-A"}),
                         ("/api/work/cost", {"org": "org-A"}),
                         ("/api/work/health", {"org": "org-A"}),
                         ("/api/work/org", {"org": "org-A"}),
                         ("/api/work/schema", {})]:
        resp = client.get(path, params=params)
        assert resp.status_code == 401, f"{path} 必须拒绝匿名访问"
        assert resp.json()["ok"] is False


def test_wrong_token_rejected_on_op_knowledge_and_approval(hosted_env):
    web, api, client = hosted_env
    headers = _auth("not-supplied-by-client")
    created = client.post("/api/work/op", json={"op": "create_work_item",
                                                "params": ALICE_CREATE_PARAMS},
                          headers=headers)
    assert created.status_code == 401
    private = client.get("/api/work/knowledge", params=PRIVATE_KNOWLEDGE_PARAMS,
                         headers=headers)
    assert private.status_code == 401
    params = _forged_approval_params(actor="forged-owner")
    forged = client.post("/api/work/op", json={"op": "submit_approval", "params": params},
                         headers=headers)
    assert forged.status_code == 401


# ── ② 正确 token 正常路径（受信 Principal 驱动）──────────────────────────────


def test_registered_principal_positive_path(hosted_env):
    web, api, client = hosted_env
    headers = _auth("tok-alice")
    created = client.post("/api/work/op", json={"op": "create_work_item",
                                                "params": ALICE_CREATE_PARAMS},
                          headers=headers)
    assert created.status_code == 200, created.text
    assert created.json()["ok"] is True
    task_id = created.json()["data"]["id"]
    assert created.json()["data"]["owner"] == "alice"

    knowledge_resp = client.get("/api/work/knowledge", params=PRIVATE_KNOWLEDGE_PARAMS,
                                headers=headers)
    assert knowledge_resp.status_code == 200
    values = [c["value"] for c in knowledge_resp.json()["data"]["claims"]]
    assert "synthetic-private-answer" in values   # alice 本人可见自己的私有主张
    assert "public-answer" in values

    params = _alice_decision_params(api, task_id, actor="alice")
    approved = client.post("/api/work/op", json={"op": "submit_approval", "params": params},
                           headers=headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["ok"] is True
    assert approved.json()["data"]["actor_id"] == "alice"  # actor 来自受信主体

    started = client.post("/api/work/op", json={"op": "start_run", "params": {
        "task_id": task_id, "actor_id": "alice"}}, headers=headers)
    assert started.status_code == 200, started.text
    assert started.json()["ok"] is True
    assert started.json()["data"]["operator_id"] == "alice"
    assert started.json()["data"]["decision_id"] == approved.json()["data"]["decision_id"]


def test_approval_actor_defaults_to_principal_subject(hosted_env):
    web, api, client = hosted_env
    headers = _auth("tok-alice")
    created = client.post("/api/work/op", json={"op": "create_work_item",
                                                "params": ALICE_CREATE_PARAMS},
                          headers=headers)
    task_id = created.json()["data"]["id"]
    params = _alice_decision_params(api, task_id, actor="alice")
    del params["actor_id"]  # 正文不带 actor：批准方只能来自受信主体
    approved = client.post("/api/work/op", json={"op": "submit_approval", "params": params},
                           headers=headers)
    assert approved.json()["ok"] is True
    assert approved.json()["data"]["actor_id"] == "alice"


# ── ③ 伪造 actor / 横向读取 / 跨组织 ─────────────────────────────────────────


def test_forged_actor_claim_rejected_for_authenticated_principal(hosted_env):
    web, api, client = hosted_env
    headers = _auth("tok-mallory")
    params = _forged_approval_params(actor="alice")
    resp = client.post("/api/work/op", json={"op": "submit_approval", "params": params},
                       headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason_code"] == "principal_actor_mismatch"


def test_horizontal_private_read_filtered_for_other_subject(hosted_env):
    """非 alice 主体读 alice 私有：响应可用，但私有主张不可见（横向读取被裁剪）。"""
    web, api, client = hosted_env
    resp = client.get("/api/work/knowledge", params=PRIVATE_KNOWLEDGE_PARAMS,
                      headers=_auth("tok-bob"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    values = [c["value"] for c in body["data"]["claims"]]
    assert "synthetic-private-answer" not in values
    assert "public-answer" in values  # 公开主张不受影响

    mallory = client.get("/api/work/knowledge", params=PRIVATE_KNOWLEDGE_PARAMS,
                         headers=_auth("tok-mallory"))
    assert mallory.status_code == 200
    assert "synthetic-private-answer" not in mallory.text


def test_cross_org_approval_rejected(hosted_env):
    """跨组织审批：决定的 org 与目标任务的 org 不一致 → 拒绝。"""
    web, api, client = hosted_env
    headers = _auth("tok-alice")
    created = client.post("/api/work/op", json={"op": "create_work_item",
                                                "params": ALICE_CREATE_PARAMS},
                          headers=headers)
    task_id = created.json()["data"]["id"]
    params = _alice_decision_params(api, task_id, actor="alice")
    params["org_id"] = "org-B"  # 决定落在 org-B，目标任务属于 org-A
    resp = client.post("/api/work/op", json={"op": "submit_approval", "params": params},
                       headers=headers)
    assert resp.json()["ok"] is False
    assert resp.json()["reason_code"] == "principal_org_mismatch"


def test_no_membership_principal_cannot_approve(hosted_env):
    """无组织成员关系的主体（mallory）对组织内目标无批准权限。"""
    web, api, client = hosted_env
    created = client.post("/api/work/op", json={"op": "create_work_item",
                                                "params": ALICE_CREATE_PARAMS},
                          headers=_auth("tok-alice"))
    task_id = created.json()["data"]["id"]
    params = _alice_decision_params(api, task_id, actor="mallory")
    del params["actor_id"]  # mallory 以自己（注册主体）身份批准别人组织的任务
    resp = client.post("/api/work/op", json={"op": "submit_approval", "params": params},
                       headers=_auth("tok-mallory"))
    assert resp.json()["ok"] is False
    assert resp.json()["reason_code"] == "principal_target_not_authorized"


def test_org_member_principal_can_approve_owner_task(hosted_env):
    """保留原成员审批场景，按后续 N01 契约加强：成员资格不产生审批权。"""
    web, api, client = hosted_env
    created = client.post("/api/work/op", json={"op": "create_work_item",
                                                "params": ALICE_CREATE_PARAMS},
                          headers=_auth("tok-alice"))
    task_id = created.json()["data"]["id"]
    params = _alice_decision_params(api, task_id, actor="bob")
    del params["actor_id"]
    resp = client.post("/api/work/op", json={"op": "submit_approval", "params": params},
                       headers=_auth("tok-bob"))
    assert resp.json()["ok"] is False, resp.text
    assert resp.json()["reason_code"] == "principal_target_not_authorized"


def test_create_under_other_owner_rejected_for_non_admin(hosted_env):
    """非管理员主体不能替别人建单（owner 声明与受信主体不符 → 拒绝）。"""
    web, api, client = hosted_env
    params = dict(ALICE_CREATE_PARAMS)
    resp = client.post("/api/work/op", json={"op": "create_work_item", "params": params},
                       headers=_auth("tok-mallory"))
    assert resp.json()["ok"] is False
    assert resp.json()["reason_code"] == "principal_actor_mismatch"


# ── ④ 共享 token 回落管理员主体；知识可见性仍按主体派生 ──────────────────────


def test_shared_token_falls_back_to_admin_principal(hosted_env):
    web, api, client = hosted_env
    headers = _auth("tok-root")
    created = client.post("/api/work/op", json={"op": "create_work_item",
                                                "params": ALICE_CREATE_PARAMS},
                          headers=headers)
    assert created.json()["ok"] is True  # 管理员主体可代 declare owner
    assert created.json()["data"]["owner"] == "alice"
    knowledge_resp = client.get("/api/work/knowledge", params=PRIVATE_KNOWLEDGE_PARAMS,
                                headers=headers)
    assert knowledge_resp.json()["ok"] is True
    # 管理员主体 subject=hosted-admin：alice 的私有主张仍不可见（私有只对授权主体可见）
    assert "synthetic-private-answer" not in knowledge_resp.text
    assert "public-answer" in knowledge_resp.text


def test_registered_admin_token_allowed(hosted_env):
    web, api, client = hosted_env
    resp = client.get("/api/work/schema", headers=_auth("tok-root2"))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── ⑤ 本地模式不受影响：不带头也能走（既有契约）──────────────────────────────


def test_local_mode_work_endpoints_stay_open(monkeypatch, tmp_path):
    monkeypatch.delenv("EVOHARNESS_HOSTED_TOKEN", raising=False)
    monkeypatch.delenv("EVOHARNESS_HOST", raising=False)
    import server.app as web

    web = importlib.reload(web)
    api = open_api("sqlite:///" + (tmp_path / "local-auth.db").as_posix(),
                   artifacts_dir=tmp_path / "artifacts")
    web._WORK_API = api
    try:
        from fastapi.testclient import TestClient

        client = TestClient(web.app)
        resp = client.get("/api/work/schema")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        created = client.post("/api/work/op", json={"op": "create_work_item",
                                                    "params": ALICE_CREATE_PARAMS})
        assert created.json()["ok"] is True  # local-single-user 无账号体系（设计如此）
    finally:
        web._WORK_API = None
        api._app.engine.dispose()
        importlib.reload(web)
