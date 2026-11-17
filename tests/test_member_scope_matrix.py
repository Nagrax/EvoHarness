"""N01 回归：普通成员作用域负例矩阵（审计复审 20261115）。

真实缺陷（修复前，独立复审探针 member_scope 复现）：
- hosted 模式普通已认证成员（admin=False）可跨组织 GET /api/work/item 读取
  他人组织的任务正文；
- 可 POST /api/work/op cancel_work_item 并在正文伪造 actor=bob 取消他人任务；
- _require_hosted_admin 接受所有已注册 token（不检查 admin=true），普通成员
  可通过管理门调用 skill 晋级 / repair / stop 等管理写端点；
- 工作详情/列表/证据/审批/成本/组织视图只检查 token 有效，取消/暂停/补充/
  请求验收/审阅等 op 仍透传正文 actor/reviewer。

修复语义（路由 × 主体 × 动作 见 docs/digital-employee/acceptance/AUDIT-FIXES/
N01-20261115.md 权限路由矩阵表）：
- hosted 模式下所有 /api/work/* 路由与 op 进入同一 Principal 服务：
  读操作按目标归属组织/当事人校验可见性；写操作按动作 grant（任务当事人或
  批准决定绑定）校验；跨组织 ID 拒绝且不泄露正文；
- actor/requester/reviewer 只从受信会话解析：正文声明与受信主体不一致即拒
  （principal_actor_mismatch）；无正文字段自动补本会话主体；
- _require_hosted_admin 只放行 admin=True 注册主体与共享 HOSTED_TOKEN；
  组织成员资格不自动产生审批代发/管理/个人资产访问权；
- 每次操作前重验目标授权与最新撤权版本（token 撤销即时生效；批准撤销/
  过期/替代后 start_run 重放被拒）。

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


def _delegated_params(*, tenant: str, owner: str, org_id: str, title: str) -> dict:
    return {"tenant": tenant, "owner": owner, "title": title,
            "goal": "synthetic-goal", "acceptance": [{"kind": "code"}],
            "workflow_kind": "delegated_delivery", "assignee_id": owner,
            "org_id": org_id}


ALICE_TASK_A = _delegated_params(tenant="tenant-A", owner="alice", org_id="org-A",
                                 title="org-A-alice-task")
TASK_B_TITLE = "org-B-private-task"


@pytest.fixture()
def member_env(monkeypatch, tmp_path):
    """hosted 模式 + 三个普通成员/一个管理员注册表 + 离线工作台数据。"""
    monkeypatch.setenv("EVOHARNESS_HOSTED_TOKEN", "tok-root")
    monkeypatch.setenv("EVOHARNESS_HOST", "0.0.0.0")
    import server.app as web

    web = importlib.reload(web)
    api = open_api("sqlite:///" + (tmp_path / "member-scope.db").as_posix(),
                   artifacts_dir=tmp_path / "artifacts")
    web._WORK_API = api
    web.HOSTED_PRINCIPALS.clear()
    web.HOSTED_PRINCIPALS.update({
        "tok-alice": {"subject": "alice", "org_ids": ["org-A"], "admin": False},
        "tok-bob": {"subject": "bob", "org_ids": ["org-A"], "admin": False},
        "tok-mallory": {"subject": "mallory", "org_ids": [], "admin": False},
        "tok-admin": {"subject": "deploy-admin", "org_ids": [], "admin": True},
    })

    # 合成数据（受信本地通道建单，等价部署期管理员/CLI 预置）：
    # taskA = org-A alice 的委派任务（附带一条 grantee=alice 的批准与排队 Run）
    # taskB = org-B bob 的委派任务（跨组织探针目标，标题作为泄露哨兵）
    # personalBob = bob 的个人任务（org_id=""，个人资产）
    created_a = api.create_work_item(**ALICE_TASK_A)
    assert created_a["ok"], created_a
    task_a = created_a["data"]["id"]
    plan = api.run_request_payload(task_a)
    assert plan["ok"], plan
    decision = api.submit_approval(org_id="org-A", actor_id="alice",
                                   target_type="work_item", target_id=str(task_a),
                                   target_revision=plan["data"]["target_revision"],
                                   payload=plan["data"]["payload"],
                                   actions=["work.execute"], grantee_id="alice")
    assert decision["ok"], decision
    run = api.start_run(task_a, actor_id="alice")
    assert run["ok"], run

    created_b = api.create_work_item(**_delegated_params(
        tenant="tenant-B", owner="bob", org_id="org-B", title=TASK_B_TITLE))
    assert created_b["ok"], created_b
    task_b = created_b["data"]["id"]

    # 组织登记（org 视图 / org_service 实体），org-B 用于跨组织负例
    from evoharness.organization.models import Organization
    with api._session() as session:
        for org_id, owner in (("org-A", "alice"), ("org-B", "bob")):
            session.add(Organization(org_id=org_id, title=f"Org {org_id}",
                                     owner_principal_id=owner))
        session.flush()

    personal = api.create_work_item(tenant="tenant-B", owner="bob",
                                    title="bob-personal-note", assignee_id="bob")
    assert personal["ok"], personal
    personal_bob = personal["data"]["id"]

    def new_task(**overrides) -> int:
        """再建一个 org-A alice 委派任务（批准生命周期负例用，互不串扰）。"""
        params = dict(ALICE_TASK_A)
        params.update(overrides)
        if params.get("title") == ALICE_TASK_A["title"]:
            params["title"] = f"org-A-alice-task-{params.get('tag', len(params))}"
        params.pop("tag", None)
        created = api.create_work_item(**params)
        assert created["ok"], created
        return created["data"]["id"]

    from fastapi.testclient import TestClient

    client = TestClient(web.app)
    yield type("Env", (), {"web": web, "api": api, "client": client,
                           "task_a": task_a, "task_b": task_b,
                           "personal_bob": personal_bob,
                           "run_a": run["data"]["operation_id"],
                           "new_task": new_task})
    # 恢复本地模式与空注册表：reload 留下的 hosted 模块级状态会污染后续测试
    web._WORK_API = None
    api._app.engine.dispose()
    web.HOSTED_PRINCIPALS.clear()
    monkeypatch.delenv("EVOHARNESS_HOSTED_TOKEN", raising=False)
    monkeypatch.delenv("EVOHARNESS_HOST", raising=False)
    importlib.reload(web)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _op(env, token: str, op: str, params: dict):
    return env.client.post("/api/work/op", headers=_auth(token),
                           json={"op": op, "params": params})


def test_same_org_member_cannot_issue_self_grant(member_env):
    env = member_env
    plan = env.api.run_request_payload(env.task_a)["data"]
    response = _op(env, "tok-bob", "submit_approval", {
        "org_id": "org-A", "target_type": "work_item",
        "target_id": str(env.task_a), "target_revision": plan["target_revision"],
        "payload": plan["payload"], "actions": ["work.execute"], "grantee_id": "bob"})
    assert response.json()["ok"] is False
    assert response.json()["reason_code"] == "principal_target_not_authorized"
    # 拒绝之后原 owner 的批准仍有效，不能由成员覆盖。
    from evoharness.governance import authorization
    from evoharness.ids import sha256_json
    with env.api._session() as session:
        decision = authorization.validate(
            session, org_id="org-A", target_type="work_item", target_id=str(env.task_a),
            target_revision=plan["target_revision"], payload_hash=sha256_json(plan["payload"]),
            action="work.execute", grantee_id="alice")
        assert decision.actor_id == "alice"


def test_same_org_member_cannot_revoke_owner_approval(member_env):
    response = _op(member_env, "tok-bob", "revoke_approval", {
        "org_id": "org-A", "target_type": "work_item", "target_id": str(member_env.task_a)})
    assert response.json()["ok"] is False
    assert response.json()["reason_code"] == "principal_target_not_authorized"


def test_assignee_cannot_mint_execution_or_send_authority(member_env):
    task_id = member_env.new_task(assignee_id="bob", tag="assignee-is-not-approver")
    plan = member_env.api.run_request_payload(task_id)["data"]
    response = _op(member_env, "tok-bob", "submit_approval", {
        "org_id": "org-A", "target_type": "work_item", "target_id": str(task_id),
        "target_revision": plan["target_revision"], "payload": plan["payload"],
        "actions": ["work.execute", "send_as_user"], "grantee_id": "bob"})
    assert response.json()["ok"] is False
    assert response.json()["reason_code"] == "principal_target_not_authorized"


def test_create_replay_key_cannot_disclose_other_owner_task(member_env):
    key = "known-client-key"
    created = member_env.api.create_work_item(
        tenant="tenant-A", owner="alice", title="private-idempotency-sentinel",
        org_id="org-A", idempotency_key=key)
    assert created["ok"]
    for token, org in (("tok-mallory", ""), ("tok-bob", "org-A")):
        response = _op(member_env, token, "create_work_item", {
            "tenant": "tenant-A", "org_id": org, "title": "other", "idempotency_key": key})
        assert response.json()["ok"] is False
        assert response.json()["reason_code"] == "idempotency_scope_conflict"
        assert "private-idempotency-sentinel" not in response.text
    replay = _op(member_env, "tok-alice", "create_work_item", {
        "tenant": "tenant-A", "org_id": "org-A", "title": "retry", "idempotency_key": key})
    assert replay.json()["ok"] is True
    assert replay.json()["data"]["id"] == created["data"]["id"]
    assert replay.json()["data"]["created"] is False


def test_create_replay_key_is_bound_to_space(member_env):
    created = member_env.api.create_work_item(
        tenant="tenant-A", owner="alice", title="space-sentinel", org_id="org-A",
        space_id="private-space", idempotency_key="same-owner-key")
    assert created["ok"]
    response = _op(member_env, "tok-alice", "create_work_item", {
        "tenant": "tenant-A", "org_id": "org-A", "space_id": "other-space",
        "title": "other", "idempotency_key": "same-owner-key"})
    assert response.json()["ok"] is False
    assert response.json()["reason_code"] == "idempotency_scope_conflict"


# ── ① 认证门：无 token / 错误 token / 撤销 token ─────────────────────────────


def test_no_token_rejected_on_every_work_route(member_env):
    env = member_env
    routes = [("/api/work/schema", {}),
              ("/api/work/view", {"tenant": "tenant-A", "owner": "alice"}),
              ("/api/work/item", {"task_id": env.task_a}),
              ("/api/work/evidence", {"task_id": env.task_a}),
              ("/api/work/approvals", {"org": "org-A"}),
              ("/api/work/cost", {"org": "org-A"}),
              ("/api/work/health", {"org": "org-A"}),
              ("/api/work/org", {"org": "org-A"})]
    for path, params in routes:
        resp = env.client.get(path, params=params)
        assert resp.status_code == 401, f"{path} 必须拒绝匿名访问"
        assert resp.json()["ok"] is False


def test_wrong_token_rejected_on_op_and_read(member_env):
    env = member_env
    read = env.client.get("/api/work/item", params={"task_id": env.task_a},
                          headers=_auth("never-issued"))
    assert read.status_code == 401
    op = _op(env, "never-issued", "cancel_work_item",
             {"task_id": env.task_a, "actor": "alice"})
    assert op.status_code == 401


def test_revoked_token_rejected_immediately_on_next_request(member_env):
    """每次操作前重验注册表：token 从注册表移除（撤权）后下一请求即 401。"""
    env = member_env
    web = env.web
    before = web.HOSTED_PRINCIPALS.get("tok-alice")
    try:
        web.HOSTED_PRINCIPALS.pop("tok-alice")
        resp = env.client.get("/api/work/item", params={"task_id": env.task_a},
                              headers=_auth("tok-alice"))
        assert resp.status_code == 401
        op = _op(env, "tok-alice", "get_work_item", {"task_id": env.task_a})
        assert op.status_code == 401
    finally:
        web.HOSTED_PRINCIPALS["tok-alice"] = before


# ── ② 有效但无权 token（mallory，无任何组织）：逐动作负例 ────────────────────


def test_unaffiliated_member_cannot_read_task_or_evidence(member_env):
    env = member_env
    for path in ("/api/work/item", "/api/work/evidence"):
        resp = env.client.get(path, params={"task_id": env.task_a},
                              headers=_auth("tok-mallory"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["reason_code"] == "principal_target_not_authorized"
        assert "org-A-alice-task" not in resp.text  # 不泄露目标正文


def test_unaffiliated_member_denied_on_every_write_action(member_env):
    """读/写/取消/完成/澄清/补充/请求验收/审阅/载荷逐动作：无权 token 一律拒绝。"""
    env = member_env
    task = env.task_a
    cases = [
        ("transition_work_item", {"task_id": task, "to_state": "ready"}),
        ("pause_work_item", {"task_id": task}),
        ("complete_work_item", {"task_id": task}),
        ("cancel_work_item", {"task_id": task}),
        ("request_clarification", {"task_id": task, "question": "syn?"}),
        ("resolve_clarification", {"task_id": task, "answer_message_id": "m-1"}),
        ("submit_supplement", {"task_id": task, "event_id": "evt-1", "note": "n"}),
        ("request_acceptance", {"task_id": task, "run_id": env.run_a}),
        ("review_acceptance", {"task_id": task, "run_id": env.run_a, "verdict": "pass"}),
        ("run_request_payload", {"task_id": task}),
    ]
    for op, params in cases:
        resp = _op(env, "tok-mallory", op, params)
        assert resp.status_code == 200, op
        body = resp.json()
        assert body["ok"] is False, op
        assert body["reason_code"] == "principal_target_not_authorized", op


def test_unaffiliated_member_cannot_pause_or_read_run(member_env):
    env = member_env
    paused = _op(env, "tok-mallory", "pause_run", {"run_id": env.run_a})
    assert paused.json()["ok"] is False
    assert paused.json()["reason_code"] == "principal_target_not_authorized"


def test_unaffiliated_member_cannot_read_approval_or_org_views(member_env):
    env = member_env
    view = env.client.get("/api/work/view",
                          params={"tenant": "tenant-A", "owner": "alice"},
                          headers=_auth("tok-mallory"))
    assert view.json()["ok"] is False
    assert view.json()["reason_code"] == "principal_actor_mismatch"
    for path, params in [("/api/work/approvals", {"org": "org-A"}),
                         ("/api/work/health", {"org": "org-A"}),
                         ("/api/work/org", {"org": "org-A"}),
                         ("/api/work/cost", {"org": "org-A"})]:
        resp = env.client.get(path, params=params, headers=_auth("tok-mallory"))
        assert resp.json()["ok"] is False, path
        assert resp.json()["reason_code"] == "principal_org_not_authorized", path


# ── ③ 同组织成员：读可见、写无 grant 即拒（动作 grant 走批准决定通道）────────


def test_same_org_member_read_allowed_but_cancel_denied(member_env):
    """组织成员资格只给读可见性：bob 可读 alice 的 org-A 任务，但不能取消。"""
    env = member_env
    read = env.client.get("/api/work/item", params={"task_id": env.task_a},
                          headers=_auth("tok-bob"))
    assert read.json()["ok"] is True
    assert read.json()["data"]["id"] == env.task_a
    cancel = _op(env, "tok-bob", "cancel_work_item", {"task_id": env.task_a})
    assert cancel.json()["ok"] is False
    assert cancel.json()["reason_code"] == "principal_target_not_authorized"
    transition = _op(env, "tok-bob", "transition_work_item",
                     {"task_id": env.task_a, "to_state": "ready"})
    assert transition.json()["reason_code"] == "principal_target_not_authorized"


def test_same_org_member_without_action_grant_cannot_start_run(member_env):
    """同组织无动作 grant：未建批准决定前 bob start_run 被拒（no_active_decision）。"""
    env = member_env
    fresh = env.new_task(tag="no-grant")
    resp = _op(env, "tok-bob", "start_run", {"task_id": fresh})
    assert resp.json()["ok"] is False
    assert resp.json()["reason_code"] == "no_active_decision"


def test_same_org_member_start_run_with_bound_grantee_allowed(member_env):
    """动作 grant 通道：alice 批准绑定 grantee=bob 后，bob（且仅 bob）可启动。"""
    env = member_env
    fresh = env.new_task(tag="grant-bob")
    plan = env.api.run_request_payload(fresh)
    assert plan["ok"], plan
    approved = _op(env, "tok-alice", "submit_approval", {
        "org_id": "org-A", "target_type": "work_item", "target_id": str(fresh),
        "target_revision": plan["data"]["target_revision"],
        "payload": plan["data"]["payload"], "actions": ["work.execute"],
        "grantee_id": "bob"})
    assert approved.json()["ok"] is True, approved.text
    started = _op(env, "tok-bob", "start_run", {"task_id": fresh})
    assert started.json()["ok"] is True, started.text
    assert started.json()["data"]["operator_id"] == "bob"
    assert started.json()["data"]["decision_id"] == approved.json()["data"]["decision_id"]


def test_bound_grantee_mismatch_rejected_for_other_member(member_env):
    """批准绑定 grantee=alice 时，同组织 bob 以自己身份 start_run 被拒（R02）。"""
    env = member_env
    fresh = env.new_task(tag="grantee-alice")
    plan = env.api.run_request_payload(fresh)
    assert plan["ok"], plan
    approved = _op(env, "tok-alice", "submit_approval", {
        "org_id": "org-A", "target_type": "work_item", "target_id": str(fresh),
        "target_revision": plan["data"]["target_revision"],
        "payload": plan["data"]["payload"], "actions": ["work.execute"],
        "grantee_id": "alice"})
    assert approved.json()["ok"] is True, approved.text
    resp = _op(env, "tok-bob", "start_run", {"task_id": fresh})
    assert resp.json()["ok"] is False
    assert resp.json()["reason_code"] == "decision_grantee_mismatch"


# ── ④ 跨组织 / 个人资产：拒绝且不泄露正文 ────────────────────────────────────


def test_cross_org_read_denied_without_content_leak(member_env):
    env = member_env
    resp = env.client.get("/api/work/item", params={"task_id": env.task_b},
                          headers=_auth("tok-alice"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason_code"] == "principal_target_not_authorized"
    assert TASK_B_TITLE not in resp.text
    assert "synthetic-goal" not in resp.text


def test_cross_org_write_and_scoped_views_denied(member_env):
    env = member_env
    cancel = _op(env, "tok-alice", "cancel_work_item", {"task_id": env.task_b})
    assert cancel.json()["reason_code"] == "principal_target_not_authorized"
    for path, params in [("/api/work/view", {"tenant": "tenant-A", "owner": "alice",
                                             "org": "org-B"}),
                         ("/api/work/approvals", {"org": "org-B"}),
                         ("/api/work/health", {"org": "org-B"}),
                         ("/api/work/org", {"org": "org-B"})]:
        resp = env.client.get(path, params=params, headers=_auth("tok-alice"))
        assert resp.json()["ok"] is False, path
        assert resp.json()["reason_code"] == "principal_org_not_authorized", path
    # 视图 owner 声明与受信主体不一致：直接拒绝（不得借他人视图越权）
    forged = env.client.get("/api/work/view",
                            params={"tenant": "tenant-B", "owner": "bob",
                                    "org": "org-B"},
                            headers=_auth("tok-alice"))
    assert forged.json()["ok"] is False
    assert forged.json()["reason_code"] == "principal_actor_mismatch"


def test_cross_org_approval_declared_org_mismatch_and_target_denial(member_env):
    env = member_env
    plan = env.api.run_request_payload(env.task_b)
    assert plan["ok"], plan
    base = {"target_type": "work_item", "target_id": str(env.task_b),
            "target_revision": plan["data"]["target_revision"],
            "payload": plan["data"]["payload"], "actions": ["work.execute"]}
    wrong_org = _op(env, "tok-alice", "submit_approval", {**base, "org_id": "org-A"})
    assert wrong_org.json()["reason_code"] == "principal_org_mismatch"
    target_org = _op(env, "tok-alice", "submit_approval", {**base, "org_id": "org-B"})
    assert target_org.json()["reason_code"] == "principal_target_not_authorized"


def test_personal_assets_not_accessible_via_org_membership(member_env):
    """个人资产（org_id=""）：组织成员资格不产生访问权，仅 owner/负责人可见。"""
    env = member_env
    for token in ("tok-alice", "tok-mallory"):
        resp = env.client.get("/api/work/item",
                              params={"task_id": env.personal_bob},
                              headers=_auth(token))
        assert resp.json()["ok"] is False, token
        assert resp.json()["reason_code"] == "principal_target_not_authorized", token
        assert "bob-personal-note" not in resp.text, token
    # owner 本人经 HTTP 可读自己的个人任务
    own = env.client.get("/api/work/item", params={"task_id": env.personal_bob},
                         headers=_auth("tok-bob"))
    assert own.json()["ok"] is True
    assert own.json()["data"]["title"] == "bob-personal-note"


# ── ⑤ 伪 actor / 伪 reviewer / 伪 requester：正文声明与受信主体不一致即拒 ────


def test_forged_actor_on_cancel_and_transition_rejected(member_env):
    env = member_env
    forged_cancel = _op(env, "tok-alice", "cancel_work_item",
                        {"task_id": env.task_b, "actor": "bob", "reason": "synthetic"})
    assert forged_cancel.json()["ok"] is False
    assert forged_cancel.json()["reason_code"] == "principal_actor_mismatch"
    forged_transition = _op(env, "tok-alice", "transition_work_item",
                            {"task_id": env.task_a, "to_state": "ready",
                             "actor": "bob"})
    assert forged_transition.json()["reason_code"] == "principal_actor_mismatch"


def test_forged_reviewer_and_requester_rejected(member_env):
    env = member_env
    cases = [
        ("tok-mallory", "request_acceptance",
         {"task_id": env.task_a, "run_id": env.run_a, "requester": "alice"}),
        ("tok-mallory", "review_acceptance",
         {"task_id": env.task_a, "run_id": env.run_a, "reviewer": "alice",
          "verdict": "pass"}),
        ("tok-bob", "review_acceptance",
         {"task_id": env.task_a, "run_id": env.run_a, "reviewer": "alice",
          "verdict": "pass"}),
        ("tok-bob", "submit_supplement",
         {"task_id": env.task_a, "event_id": "evt-2", "note": "n", "actor": "alice"}),
    ]
    for token, op, params in cases:
        resp = _op(env, token, op, params)
        assert resp.json()["ok"] is False, op
        assert resp.json()["reason_code"] == "principal_actor_mismatch", op


def test_forged_cost_principal_rejected(member_env):
    env = member_env
    resp = env.client.get("/api/work/cost", params={"org": "org-A",
                                                    "principal": "alice"},
                          headers=_auth("tok-mallory"))
    assert resp.json()["ok"] is False
    assert resp.json()["reason_code"] == "principal_actor_mismatch"


def test_actor_requester_auto_filled_from_session(member_env):
    """无正文字段自动补本会话主体：alice 建单/推进/取消自己任务全程不带 actor。"""
    env = member_env
    created = _op(env, "tok-alice", "create_work_item",
                  {"tenant": "tenant-A", "title": "auto-actor-task",
                   "goal": "g", "acceptance": [{"kind": "code"}],
                   "workflow_kind": "delegated_delivery", "org_id": "org-A"})
    assert created.json()["ok"] is True, created.text
    assert created.json()["data"]["owner"] == "alice"
    task_id = created.json()["data"]["id"]
    moved = _op(env, "tok-alice", "transition_work_item",
                {"task_id": task_id, "to_state": "ready"})
    assert moved.json()["ok"] is True, moved.text
    cancelled = _op(env, "tok-alice", "cancel_work_item",
                    {"task_id": task_id, "reason": "no longer needed"})
    assert cancelled.json()["ok"] is True, cancelled.text
    assert cancelled.json()["data"]["state"] == "cancelled"


# ── ⑥ 批准生命周期重放：撤销 / 过期 / 替代批准后 start_run 重试被拒 ───────────


def test_start_run_replay_rejected_after_approval_revoked(member_env):
    env = member_env
    fresh = env.new_task(tag="revoke")
    plan = env.api.run_request_payload(fresh)
    approved = _op(env, "tok-alice", "submit_approval", {
        "org_id": "org-A", "target_type": "work_item", "target_id": str(fresh),
        "target_revision": plan["data"]["target_revision"],
        "payload": plan["data"]["payload"], "actions": ["work.execute"],
        "grantee_id": "alice"})
    assert approved.json()["ok"] is True, approved.text
    first = _op(env, "tok-alice", "start_run", {"task_id": fresh})
    assert first.json()["ok"] is True, first.text
    revoked = _op(env, "tok-alice", "revoke_approval", {
        "org_id": "org-A", "target_type": "work_item", "target_id": str(fresh)})
    assert revoked.json()["ok"] is True, revoked.text
    replay = _op(env, "tok-alice", "start_run", {"task_id": fresh})
    assert replay.json()["ok"] is False
    assert replay.json()["reason_code"] == "no_active_decision"


def test_start_run_rejected_with_expired_approval(member_env):
    env = member_env
    fresh = env.new_task(tag="expired")
    plan = env.api.run_request_payload(fresh)
    approved = _op(env, "tok-alice", "submit_approval", {
        "org_id": "org-A", "target_type": "work_item", "target_id": str(fresh),
        "target_revision": plan["data"]["target_revision"],
        "payload": plan["data"]["payload"], "actions": ["work.execute"],
        "grantee_id": "alice", "expires_at": "2020-01-01T00:00:00Z"})
    assert approved.json()["ok"] is True, approved.text
    started = _op(env, "tok-alice", "start_run", {"task_id": fresh})
    assert started.json()["ok"] is False
    assert started.json()["reason_code"] == "decision_expired"


def test_start_run_rejected_after_approval_superseded_by_new_payload(member_env):
    env = member_env
    fresh = env.new_task(tag="superseded")
    plan = env.api.run_request_payload(fresh)
    base = {"org_id": "org-A", "target_type": "work_item", "target_id": str(fresh),
            "target_revision": plan["data"]["target_revision"],
            "actions": ["work.execute"], "grantee_id": "alice"}
    first = _op(env, "tok-alice", "submit_approval",
                {**base, "payload": plan["data"]["payload"]})
    assert first.json()["ok"] is True, first.text
    second = _op(env, "tok-alice", "submit_approval",
                 {**base, "payload": {"synthetic": "v2"}})
    assert second.json()["ok"] is True, second.text
    replay = _op(env, "tok-alice", "start_run", {"task_id": fresh})
    assert replay.json()["ok"] is False
    assert replay.json()["reason_code"] == "decision_payload_changed"


# ── ⑦ 管理门：管理 token 与普通成员 token 分开 ───────────────────────────────


def test_admin_gate_rejects_regular_member_on_every_admin_endpoint(member_env):
    env = member_env
    web = env.web
    from starlette.requests import Request

    cases = [
        ("post", "/api/skills/tiers/action", {"skill": "s1", "action": "core"}),
        ("post", "/api/skills/repair/apply", {"file": "x.md", "event": "corrupt"}),
        ("post", "/api/skills/search/action", {"search_id": "s", "action": "pause"}),
        ("post", "/api/stop", {"session_id": "someone-elses-session"}),
    ]
    for method, path, payload in cases:
        resp = env.client.request(method, path, json=payload,
                                  headers=_auth("tok-alice"))
        assert resp.status_code == 403, path
        body = resp.json()
        assert body["ok"] is False
        assert body["reason_code"] == "admin_required", path
    # 直接调用门函数：普通成员主体必须拿到拒绝响应（复审探针 #3 同款）
    request = Request({"type": "http",
                       "headers": [(b"authorization", b"Bearer tok-alice")]})
    denial = web._require_hosted_admin(request)
    assert denial is not None
    assert denial["reason_code"] == "admin_required"


def test_admin_gate_accepts_registered_admin_and_shared_token(member_env):
    env = member_env
    web = env.web
    from starlette.requests import Request

    for token in ("tok-admin", "tok-root"):
        resp = env.client.post("/api/skills/tiers/action",
                               json={"skill": "no-such-skill", "action": "core"},
                               headers=_auth(token))
        assert resp.status_code == 200, token  # 认证通过，进入业务逻辑
        body = resp.json()
        assert body["ok"] is False  # unknown skill 是业务层响应，不是 401/403
        assert "401" not in str(body.get("error", "")) and "403" not in str(body.get("error", ""))
        request = Request({"type": "http",
                           "headers": [(b"authorization",
                                        f"Bearer {token}".encode())]})
        assert web._require_hosted_admin(request) is None, token


def test_admin_principal_bypasses_target_scoping(member_env):
    """管理员主体可跨组织读/取消（部署运维身份）；成员不行。"""
    env = member_env
    read = env.client.get("/api/work/item", params={"task_id": env.task_b},
                          headers=_auth("tok-admin"))
    assert read.json()["ok"] is True
    assert read.json()["data"]["title"] == TASK_B_TITLE
    cancelled = _op(env, "tok-admin", "cancel_work_item",
                    {"task_id": env.task_b, "reason": "ops cleanup"})
    assert cancelled.json()["ok"] is True, cancelled.text


# ── ⑧ 组织范围内读路由正例：成员可见本组织视图，成本收敛到本人口径 ───────────


def test_org_scoped_read_routes_positive_for_member(member_env):
    env = member_env
    ok_cases = [("/api/work/approvals", {"org": "org-A"}),
                ("/api/work/health", {"org": "org-A"}),
                ("/api/work/org", {"org": "org-A"}),
                ("/api/work/schema", {}),
                ("/api/work/item", {"task_id": env.task_a})]
    for path, params in ok_cases:
        resp = env.client.get(path, params=params, headers=_auth("tok-bob"))
        assert resp.status_code == 200, path
        assert resp.json()["ok"] is True, path


def test_member_cost_view_forced_to_own_scope(member_env):
    env = member_env
    own = env.client.get("/api/work/cost", params={"org": "org-A"},
                         headers=_auth("tok-alice"))
    assert own.json()["ok"] is True
    # 组织成员看组织内成本时口径收敛为本人（principal_id 自动补受信主体）
    assert own.json()["data"]["unknown"]["price_basis"]
    schema = env.client.get("/api/work/schema", headers=_auth("tok-mallory"))
    assert schema.json()["ok"] is True
