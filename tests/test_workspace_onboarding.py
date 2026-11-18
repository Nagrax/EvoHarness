import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from evoharness.api import open_api
from evoharness.application import Application
from evoharness.governance.budget_gateway import BudgetGateway
from evoharness.organization import access
from evoharness.organization import service as org_service
from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
from evoharness.work.worker import WorkWorker
from server import app as web
from server import workspace
from server import workspace_feishu


ORIGIN = "https://workspace.test"
TEST_PASSPHRASE = "correct-horse-test-only"


def test_web_and_worker_can_initialize_a_new_database_concurrently(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.sqlite'}"
    launch = Barrier(2)

    def start():
        launch.wait(timeout=5)
        app = Application.open(url)
        try:
            return set(inspect(app.engine).get_table_names())
        finally:
            app.engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        web_tables, worker_tables = list(pool.map(lambda _: start(), range(2)))
    assert web_tables == worker_tables
    assert {"workspace_accounts", "workspace_sessions", "execution_runs", "source_sync"} <= web_tables


def test_invited_organization_browser_contract(tmp_path, monkeypatch):
    api = open_api(f"sqlite:///{tmp_path / 'new.sqlite'}", artifacts_dir=tmp_path / "artifacts",
                   acceptance_policy="strict")
    monkeypatch.setenv("EVOHARNESS_SKILLS_ROOT", str(tmp_path / "skills"))
    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(tmp_path / "evolution"))
    monkeypatch.setattr(web, "_get_work_api", lambda: api)
    monkeypatch.setattr(workspace, "ORIGIN", ORIGIN)
    app = FastAPI()
    app.include_router(web.app.router)
    workspace.install(app, lambda: api, web.ROOT / "frontend")
    owner = TestClient(app, base_url=ORIGIN)
    contributor = TestClient(app, base_url=ORIGIN)
    outsider = TestClient(app, base_url=ORIGIN)

    def post(client, path, **params):
        saved = client.cookies.get(access.COOKIE)
        response = client.post(path, json=params, headers={"Origin": ORIGIN,
                               **({"Cookie": f"{access.COOKIE}={saved}"} if saved else {})})
        return response

    def get(client, path, **params):
        saved = client.cookies.get(access.COOKIE)
        return client.get(path, params=params, headers={"Cookie": f"{access.COOKIE}={saved}"} if saved else {})

    def register(client, username, invitation):
        res = post(client, "/api/access/register", username=username, password=TEST_PASSPHRASE, invitation=invitation)
        assert res.status_code == 200, res.json()
        for attr in ("HttpOnly", "Secure", "SameSite=strict", "Path=/workspace/"):
            assert attr in res.headers["set-cookie"]
        return get(client, "/api/access/me").json()["data"]

    assert owner.get("/", follow_redirects=False).headers["location"] == "/workspace/access"
    assert "登录工作区" in owner.get("/access").text
    assert owner.get("/api/work/schema").status_code == 401
    assert owner.post("/api/access/login", json={"username": "no-user", "password": TEST_PASSPHRASE}).status_code == 403
    with api._session() as db:
        invite_owner, invite_outsider = access.issue_invitation(db), access.issue_invitation(db)
    lead = register(owner, "team-lead", invite_owner)
    other = register(outsider, "other-lead", invite_outsider)
    assert lead["subject"] != other["subject"] and not lead["admin"]
    repeat = post(contributor, "/api/access/register", username="replay", password=TEST_PASSPHRASE, invitation=invite_owner)
    assert repeat.json()["reason_code"] == "invitation_invalid"

    result = post(owner, "/api/access/organization", name="物流演练组").json()
    assert result["ok"], result
    org_id = result["data"]["org_id"]
    member_invite = post(owner, "/api/access/invitation", org_id=org_id).json()["data"]["invitation"]
    member = register(contributor, "warehouse-review", member_invite)
    assert member["org_ids"] == [org_id] and not member["organizations"][0]["manage"]
    assert post(contributor, "/api/access/organization", name="未经授权").status_code == 403
    assert not post(contributor, "/api/access/invitation", org_id=org_id).json()["ok"]
    assert not get(outsider, "/api/work/org", org=org_id).json()["ok"]
    worker = post(owner, "/api/access/employee", org_id=org_id, name="配送助理").json()
    assert worker["ok"], worker
    employee = worker["data"]["employee"]
    org = get(owner, "/api/work/org", org=org_id).json()["data"]
    space_id = org["spaces"][0]["space_id"]

    def op(client, operation, **params):
        return post(client, "/api/work/op", op=operation, params=params).json()

    item = op(owner, "create_work_item", owner=lead["subject"], tenant="local", org_id=org_id,
              space_id=space_id, title="夜班异常交接", goal="整理夜班异常包裹交接方式",
              assignee_id=member["subject"], acceptance=[{"kind": "qa", "required_artifacts": ["answer", "evidence"]}])
    assert item["ok"], item
    task_id = item["data"]["id"]
    assert get(contributor, "/api/work/view", tenant="workspace", owner=member["subject"], org=org_id).json()["data"]["total"] == 1
    assert not get(outsider, "/api/work/item", task_id=task_id).json()["ok"]
    assert not op(owner, "create_work_item", owner=lead["subject"], tenant="workspace", org_id=org_id,
                  space_id=space_id, title="非允许工作流", goal="建立代码任务", acceptance=[{"kind": "code"}])["ok"]
    assert not post(contributor, "/api/access/delegate", task_id=task_id, employee=employee).json()["ok"]
    assert post(owner, "/api/access/memory", task_id=task_id, subject="夜班", value="提前写入").status_code == 409
    assert not op(owner, "start_run", task_id=task_id, actor_id=employee)["ok"]
    delegated = post(owner, "/api/access/delegate", task_id=task_id, employee=employee).json()
    assert delegated["ok"], delegated
    run_id = delegated["data"]["operation_id"]
    adapter = RestrictedAgentAdapter(provider=FakeProvider([
        {"tool_calls": [{"name": "skill", "arguments": {"skill_name": "delivery-review"}}]},
        {"content": "核对异常件编号，然后记录承接人。", "final": True},
    ]), gateway=BudgetGateway(api._app.session_factory))
    executed = WorkWorker(api._app.session_factory, adapter, api._authorize_adapter()).process_run(run_id)
    assert executed["ok"], executed
    assert op(contributor, "request_acceptance", task_id=task_id, run_id=run_id)["ok"]
    assert get(contributor, "/api/work/item", task_id=task_id).json()["data"]["runs"][0]["accepted"] is False
    evidence = get(contributor, "/api/work/evidence", task_id=task_id).json()["data"]["artifacts"]
    answer_id = next(row["artifact_id"] for row in evidence if row["kind"] == "answer")
    assert "异常件编号" in get(contributor, "/api/access/artifact", artifact_id=answer_id).json()["data"]["text"]
    assert get(outsider, "/api/access/artifact", artifact_id=answer_id).status_code == 404
    review = op(contributor, "review_acceptance", task_id=task_id, run_id=run_id, verdict="pass", notes="交接还应标注时间。")
    assert review["ok"] and review["data"]["state"] == "done", review
    item_after_review = get(contributor, "/api/work/item", task_id=task_id).json()["data"]
    assert item_after_review["runs"][0]["accepted"] is True
    learning = item_after_review["learning"]
    assert learning[0]["state"] == "pending"

    async def learn(system, prompt):
        if "Set Manager" in system:
            return json.dumps({"action": "merge", "target_skill": "delivery-review",
                               "merged_instructions": "核对任务目标和验收标准，记录交接确认时间。"})
        assert json.loads(prompt)["authorized_review"] == "交接还应标注时间。"
        return json.dumps({"skills": [{"name": "确认时间", "description": "交接验收",
                                       "instructions": "记录交接确认时间。", "evidence": "验收反馈"}]})

    evidence_id = learning[0]["evidence_id"]
    candidate = api.learning_action(task_id, evidence_id=evidence_id, action="build",
                                    actor=member["subject"], _side_query=learn)
    assert candidate["ok"], candidate
    assert candidate["data"]["result"]["proposal"]["name"] == "delivery-review"
    denied = op(contributor, "learning_action", task_id=task_id, evidence_id=evidence_id, action="adopt")
    assert denied["reason_code"] == "learning_publish_not_authorized", denied
    adopted = op(owner, "learning_action", task_id=task_id, evidence_id=evidence_id, action="adopt")
    assert adopted["ok"] and adopted["data"]["result"]["publication"]["sha256"], adopted
    memory = post(contributor, "/api/access/memory", task_id=task_id, subject="异常包裹交接", value="记录编号、承接人和时间。")
    assert memory.json()["ok"], memory.json()
    found = get(owner, "/api/work/knowledge", space=space_id, subject="异常包裹交接").json()
    assert found["ok"] and found["data"]["claims"][0]["value"] == "记录编号、承接人和时间。", found
    assert not get(outsider, "/api/work/knowledge", space=space_id, subject="异常包裹交接").json()["ok"]

    previous = contributor.cookies.get(access.COOKIE)
    assert post(contributor, "/api/access/logout").json()["ok"]
    assert contributor.get("/api/access/me", headers={"Cookie": f"{access.COOKIE}={previous}"}).status_code == 401
    assert post(contributor, "/api/access/login", username="warehouse-review", password="wrong").status_code == 401
    assert post(contributor, "/api/access/login", username="warehouse-review", password=TEST_PASSPHRASE).status_code == 200
    assert get(contributor, "/api/access/me").json()["data"]["subject"] == member["subject"]

    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test-only")
    monkeypatch.setenv("FEISHU_CHAT_WHITELIST", "oc_allowed")
    fetched = []

    def fetch(message_id, chat_id):
        fetched.append((message_id, chat_id))
        return "夜班测试待办"

    monkeypatch.setattr(workspace_feishu, "fetch_message", fetch)
    params = dict(org_id=org_id, space_id=space_id, message_id="om_0123456789", title="飞书待办", criteria="列出交接字段")
    assert post(owner, "/api/access/feishu/task", **params).status_code == 403
    assert not fetched
    with api._session() as db:
        org_service.bind_channel(db, access._actor(lead["subject"]), org_id=org_id, space_id=space_id,
                                 tenant_id=access.NAMESPACE, app_id="cli_test", channel_id="oc_allowed",
                                 read_grant=True, write_grant=False)
    assert post(contributor, "/api/access/feishu/task", **params).status_code == 403
    assert post(outsider, "/api/access/feishu/task", **params).status_code == 403
    assert not fetched
    imported = post(owner, "/api/access/feishu/task", **params).json()
    assert imported["ok"], imported
    assert fetched == [("om_0123456789", "oc_allowed")]
    assert imported["data"]["source_refs"][0].startswith("feishu-message:om_0123456789@")
