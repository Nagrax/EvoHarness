"""DE-28：工作台与统一 API/CLI 的三方体验回归。

覆盖（离线，不启真实浏览器/不触网）：
1. 同一场景从 CLI / API / server 侧派发得到一致权限与状态；
2. dryrun / 等待授权 / 执行结束待验收 / 回执未知 / 已完成 / 已取消 各自独立
   reason_code，UI 数据源不把等待类显示为完成；
3. 错误路径：无授权返回可重试+next_action；未知对象返回可读错误码；
   长输出落产物回引用；
4. 批准绑定目标版本与载荷；撤回后旧批准失效、排队发送意图取消；
5. 前端静态资源与 api.py 契约字段一致（字段存在性断言）。
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from evoharness.api import (  # noqa: E402
    ENVELOPE_KEYS,
    NON_FINAL_CODES,
    WorkApi,
)
from evoharness.application import Application  # noqa: E402
from evoharness.ids import sha256_json  # noqa: E402
from evoharness.models import Outbox, OutboxStatus  # noqa: E402
from evoharness.work import execution, service as work_service  # noqa: E402
from evoharness.work.models import TaskSupplement  # noqa: E402


def _allow(session, context, action):
    return True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_url = "sqlite:///" + (tmp_path / "workbench.db").as_posix()
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL", db_url)
    application = Application.open(db_url)
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    api_open = WorkApi(application, artifacts_dir=tmp_path / "artifacts", authorize=_allow)
    return type("Env", (), {"tmp": tmp_path, "db_url": db_url, "app": application,
                            "api": api, "api_open": api_open,
                            "artifacts": tmp_path / "artifacts"})


def _delegated(api, *, org_id="org1", title="修复登录超时缺陷", assignee="bob"):
    r = api.create_work_item(
        tenant="t", owner="alice", title=title, action="fix", chat_id="chat-1",
        goal="修复登录超时并补回归",
        acceptance=[{"kind": "code", "criteria": "回归通过",
                     "required_artifacts": ["diff", "test_report"]}],
        assignee_id=assignee, workflow_kind="delegated_delivery", org_id=org_id)
    assert r["ok"], r
    return r["data"]


def _approve_run(api, task_id, *, org_id="org1", actor="alice", brief=None):
    plan = api.run_request_payload(task_id, brief)
    assert plan["ok"], plan
    return api.submit_approval(
        org_id=org_id, actor_id=actor, target_type="work_item",
        target_id=str(task_id), target_revision=plan["data"]["target_revision"],
        payload=plan["data"]["payload"], actions=["work.execute"])


# ── 1. 信封契约 ──────────────────────────────────────────────────────────────

def test_envelope_contract_on_all_ops(env):
    api = env.api
    results = [
        api.schema(),
        api.work_view(tenant="t", owner="alice"),
        api.get_work_item(9999),
        api.search_knowledge(space_id="s", subject="x"),
        api.view_cost(),
        api.view_health(),
        api.approvals_view("org1"),
    ]
    for result in results:
        assert set(ENVELOPE_KEYS) <= set(result), result
        assert result["reason_code"], result
        assert isinstance(result["evidence_refs"], list)


def test_schema_documents_required_operations():
    import evoharness.api as api_module
    schema = api_module.API_SCHEMA
    ops = schema["operations"]
    required = {"create_work_item", "get_work_item", "work_view", "start_run",
                "pause_run", "submit_approval", "revoke_approval", "search_knowledge",
                "view_evidence", "view_cost", "view_health", "submit_supplement",
                "review_acceptance"}
    assert required <= set(ops)
    codes = set(schema["display"]["reason_codes"])
    # 六种状态各有独立 reason_code，等待类不在完成类里
    for code in ("dryrun_not_live", "waiting_authorization", "awaiting_acceptance",
                 "receipt_unknown", "delivered", "cancelled"):
        assert code in codes
    assert NON_FINAL_CODES & {"delivered", "cancelled"} == set()
    assert {"dryrun_not_live", "waiting_authorization", "awaiting_acceptance",
            "receipt_unknown"} <= NON_FINAL_CODES


# ── 2. 创建/获取：稳定 ID 与幂等 ─────────────────────────────────────────────

def test_create_get_work_item_stable_id_and_revision(env):
    api = env.api
    first = api.create_work_item(tenant="t", owner="alice", title="整理周报",
                                 assignee_id="bob", idempotency_key="weekly-1")
    assert first["ok"] and first["data"]["created"] is True
    task_id = first["data"]["id"]
    again = api.create_work_item(tenant="t", owner="alice", title="整理周报",
                                 idempotency_key="weekly-1")
    assert again["data"]["created"] is False
    assert again["data"]["id"] == task_id
    detail = api.get_work_item(task_id)
    assert detail["ok"]
    assert detail["data"]["revision"] == 1
    assert detail["data"]["display"]["reason_code"] == "ready_to_execute" or \
        detail["data"]["state"] == "open"
    missing = api.get_work_item(99999)
    assert missing["ok"] is False
    assert missing["reason_code"] == "task_not_found"
    assert missing["retryable"] is False
    assert missing["next_action"] == "check_object_id"


# ── 3. 三入口一致：API / CLI / server 派发 ───────────────────────────────────

def test_three_entries_agree_on_authorization_and_state(env, monkeypatch):
    import server.app as server_app

    api = env.api
    task = _delegated(api)
    task_id = task["id"]
    # 未批准：三条入口同一 reason_code，等待授权可重试
    no_auth_api = api.start_run(task_id, actor_id="bob", output_root=str(env.tmp / "r1"))
    assert no_auth_api["ok"] is False
    assert no_auth_api["reason_code"] == "no_active_decision"
    assert no_auth_api["retryable"] is True
    assert no_auth_api["next_action"] == "submit_approval"

    monkeypatch.setattr(server_app, "_WORK_API", None)
    no_auth_server = server_app.work_dispatch("start_run", {"task_id": task_id, "actor_id": "bob"})
    assert no_auth_server["reason_code"] == no_auth_api["reason_code"]
    assert no_auth_server["retryable"] == no_auth_api["retryable"]

    from evoharness.cli import main as cli_main

    out = io.StringIO()
    exit_code = cli_main(
        ["--db", env.db_url, "--json", "run", "start", "--task", str(task_id),
         "--actor", "bob"], stdout=out)
    payload = json.loads(out.getvalue())
    assert exit_code == 2
    assert payload["reason_code"] == "no_active_decision"

    # 批准后：CLI 启动成功，API 视图与 server 派发视图给出同一状态
    approved = _approve_run(api, task_id)
    assert approved["ok"], approved
    out = io.StringIO()
    exit_code = cli_main(
        ["--db", env.db_url, "--json", "run", "start", "--task", str(task_id),
         "--actor", "bob", "--output-root", str(env.tmp / "r2")], stdout=out)
    payload = json.loads(out.getvalue())
    assert exit_code == 0, payload
    assert payload["data"]["state"] == "queued"
    assert payload["data"]["task_state"] == "queued"

    view_api = api.work_view(tenant="t", owner="alice", org_id="org1")
    monkeypatch.setattr(server_app, "_WORK_API", None)
    view_server = server_app.work_dispatch("work_view", {"tenant": "t", "owner": "alice",
                                                         "org_id": "org1"})
    assert view_server["ok"] is True
    row_api = next(r for r in view_api["data"]["buckets"]["in_progress"] if r["id"] == task_id)
    row_server = next(r for r in view_server["data"]["buckets"]["in_progress"]
                      if r["id"] == task_id)
    assert row_api["display"]["reason_code"] == row_server["display"]["reason_code"] == "run_queued"
    assert row_server["display"]["bucket"] == "in_progress"


# ── 4. 状态区分：等待类绝不显示为完成 ────────────────────────────────────────

def _session(env):
    return env.app.transaction()


def test_state_distinction_not_shown_as_done(env):
    api = env.api_open  # 宽松授权：只测状态映射
    codes = {}
    # dryrun：Run 模式记录为 dryrun → 不得显示为已发送/执行完成
    t1 = _delegated(api, title="dryrun 工单")["id"]
    r = api.start_run(t1, actor_id="bob", mode="dryrun")
    assert r["ok"], r
    codes["dryrun"] = api.get_work_item(t1)["data"]["display"]["reason_code"]
    assert codes["dryrun"] == "dryrun_not_live"

    # 等待授权（等待类必须带原因）
    t2 = _delegated(api, title="等待授权工单")["id"]
    moved = api.transition_work_item(t2, to_state="queued", actor="alice")
    assert moved["ok"], moved
    moved = api.transition_work_item(t2, to_state="running", actor="bob")
    assert moved["ok"], moved
    moved = api.transition_work_item(t2, to_state="waiting_authorization", actor="bob",
                                     reason="缺少 work.execute 批准")
    assert moved["ok"], moved
    codes["waiting_authorization"] = api.get_work_item(t2)["data"]["display"]["reason_code"]

    # 执行结束待验收
    t3 = _delegated(api, title="待验收工单")["id"]
    api.start_run(t3, actor_id="bob")
    api.transition_work_item(t3, to_state="running", actor="bob")
    api.transition_work_item(t3, to_state="waiting_acceptance", actor="bob")
    codes["awaiting_acceptance"] = api.get_work_item(t3)["data"]["display"]["reason_code"]

    # 回执未知：outbox 停在 delivery_unknown
    t4 = _delegated(api, title="回执未知工单")["id"]
    api.transition_work_item(t4, to_state="queued", actor="alice")
    api.transition_work_item(t4, to_state="running", actor="bob")
    with _session(env) as session:
        session.add(Outbox(intent_key="ik-unknown", tenant="t", kind="reply",
                           target_type="work_item", target_id=str(t4),
                           payload_json="{}", status=OutboxStatus.DELIVERY_UNKNOWN))
    codes["receipt_unknown"] = api.get_work_item(t4)["data"]["display"]["reason_code"]

    # 已完成（普通待办走既有完成行为；委派交付不走此入口）
    plain = api.create_work_item(tenant="t", owner="alice", title="普通待办",
                                 assignee_id="bob", org_id="org1")
    done = api.complete_work_item(plain["data"]["id"], actor="alice")
    assert done["ok"], done
    codes["delivered"] = api.get_work_item(plain["data"]["id"])["data"]["display"]["reason_code"]

    # 已取消
    t6 = _delegated(api, title="取消工单")["id"]
    api.cancel_work_item(t6, actor="alice", reason="重复工单")
    codes["cancelled"] = api.get_work_item(t6)["data"]["display"]["reason_code"]

    distinct = set(codes.values())
    assert len(distinct) == 6, codes
    assert codes["receipt_unknown"] != "delivered"
    assert codes["dryrun"] != "delivered"
    assert codes["waiting_authorization"] != "delivered"

    # 视图分桶：等待授权与回执未知进"待我处理"，不进"已交付"
    view = api.work_view(tenant="t", owner="alice", org_id="org1")
    buckets = view["data"]["buckets"]
    assert any(row["id"] == t2 for row in buckets["awaiting_me"])
    assert any(row["id"] == t4 for row in buckets["awaiting_me"])
    assert any(row["id"] == t1 for row in buckets["in_progress"])
    assert all(row["id"] != t4 for row in buckets["delivered"])
    assert any(row["id"] == plain["data"]["id"] for row in buckets["delivered"])
    assert any(row["id"] == t6 for row in buckets["cancelled"])


# ── 5. 批准绑定版本与载荷；撤回失效 ─────────────────────────────────────────

def test_approval_binds_revision_and_payload(env):
    api = env.api
    task = _delegated(api)
    task_id = task["id"]
    plan = api.run_request_payload(task_id)
    brief = plan["data"]["payload"]
    assert plan["data"]["payload_hash"] == sha256_json(brief)
    approved = api.submit_approval(org_id="org1", actor_id="alice", target_type="work_item",
                                   target_id=str(task_id), target_revision=1,
                                   payload=brief, actions=["work.execute"])
    assert approved["ok"]
    # 正文变化：同版本不同载荷 → 旧批准拒绝
    changed = api.start_run(task_id, actor_id="bob", brief={**brief, "goal": "改了正文"})
    assert changed["ok"] is False
    assert changed["reason_code"] == "decision_payload_changed"
    assert changed["retryable"] is False
    assert changed["next_action"] == "refresh_target_and_reapprove"
    # 原载荷可启动
    ok_run = api.start_run(task_id, actor_id="bob")
    assert ok_run["ok"], ok_run

    # 版本变化：修订合同后旧 revision 的批准拒绝
    with _session(env) as session:
        work_service.revise_contract(session, task_id, actor="alice", goal="修订后的目标",
                                     expected_revision=1, reason="范围变更")
    stale = api.start_run(task_id, actor_id="bob")
    assert stale["ok"] is False
    assert stale["reason_code"] == "decision_target_revision_changed"


def test_revoke_invalidates_approval_and_cancels_queued_intents(env):
    api = env.api
    task = _delegated(api)
    task_id = task["id"]
    with _session(env) as session:
        session.add(Outbox(intent_key="ik-send-1", tenant="t", kind="reply",
                           target_type="work_item", target_id=str(task_id),
                           payload_json="{}", payload_hash="h1"))
    approved = _approve_run(api, task_id)
    assert approved["ok"]
    revoked = api.revoke_approval(org_id="org1", actor_id="alice",
                                  target_type="work_item", target_id=str(task_id))
    assert revoked["ok"], revoked
    assert revoked["data"]["count"] == 1
    assert "ik-send-1" in revoked["data"]["cancelled_intents"]
    view = api.get_approval(approved["data"]["decision_id"])
    assert view["ok"] and view["data"]["revoked"]
    blocked = api.start_run(task_id, actor_id="bob")
    assert blocked["ok"] is False
    assert blocked["reason_code"] == "no_active_decision"
    with _session(env) as session:
        row = session.query(Outbox).filter(
            Outbox.intent_key == "ik-send-1").first()
        assert row.status == OutboxStatus.CANCELLED
        assert "decision_revoked" in row.last_error


def test_approval_view_shows_target_identity_scope_expiry(env):
    api = env.api
    task = _delegated(api)
    approved = api.submit_approval(
        org_id="org1", actor_id="alice", target_type="work_item",
        target_id=str(task["id"]), target_revision=1,
        payload={"text": "批准发送这段回复"}, actions=["send_as_user"],
        grantee_id="twin-1", expires_at="2026-12-01T00:00:00")
    assert approved["ok"]
    view = api.get_approval(approved["data"]["decision_id"])
    data = view["data"]
    # 审批显示目标会话 / 发言身份 / 影响范围 / 过期时间 / 正文
    assert data["target_id"] == str(task["id"])
    assert data["identity"] == "user"
    assert "send_as_user" in data["actions"]
    assert data["expires_at"].startswith("2026-12-01")
    assert data["grantee_id"] == "twin-1"
    assert data["payload_hash"] == sha256_json({"text": "批准发送这段回复"})
    listing = api.approvals_view("org1")
    assert any(a["decision_id"] == approved["data"]["decision_id"]
               for a in listing["data"]["approvals"])


# ── 6. 错误路径与长输出落产物 ────────────────────────────────────────────────

def test_unknown_objects_return_readable_codes(env):
    api = env.api
    assert api.pause_run("missing-run")["reason_code"] == "run_not_found"
    assert api.get_approval("missing-decision")["reason_code"] == "decision_not_found"
    assert api.get_work_item("not-a-number")["reason_code"] == "task_not_found"
    missing_run = api.pause_run("missing-run")
    assert missing_run["retryable"] is False


def test_long_output_spilled_to_artifact_with_refs(env):
    api = env.api
    with _session(env) as session:
        from evoharness.knowledge import service as ks
        ks.record_claim(session, space_id="s", subject="部署口径", owner_scope="public",
                        value="步骤很长：" + "细节" * 5000,
                        source_refs=["doc://runbook@v3"])
    result = api.search_knowledge(space_id="s", subject="部署口径")
    assert result["ok"]
    data = result["data"]
    assert data.get("truncated") is True
    ref = data["artifact"]
    path = Path(ref["path"])
    assert path.is_file()
    raw = path.read_text(encoding="utf-8")
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == ref["sha256"]
    assert len(ref["preview"]) < len(raw)
    # 内联只剩截断预览（全文只落产物），Agent 按 ref 精确读取
    assert "细节" * 1000 not in json.dumps(data["claims"], ensure_ascii=False)
    assert "细节" * 1000 in raw
    # evidence_refs 引用来源，Agent 可按 ref 精确读取
    assert any("doc://runbook@v3" == r for r in result["evidence_refs"])


def test_work_view_spills_when_many_items(env):
    api = env.api
    for index in range(30):
        created = api.create_work_item(tenant="t", owner="alice", title=f"待办 {index}",
                                       assignee_id="bob",
                                       idempotency_key=f"bulk-{index}")
        assert created["ok"]
    view = api.work_view(tenant="t", owner="alice")
    assert view["data"]["truncated"] is True
    ref = view["data"]["artifact"]
    raw = Path(ref["path"]).read_text(encoding="utf-8")
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == ref["sha256"]
    assert view["data"]["counts"]["pending"] == 30


# ── 7. 追加说明去重与在途交付 ────────────────────────────────────────────────

def test_supplement_dedup_and_inflight_delivery(env):
    api = env.api_open
    task = _delegated(api)
    task_id = task["id"]
    first = api.submit_supplement(task_id, event_id="m-100", note="补充：影响移动端",
                                  actor="alice")
    assert first["ok"]
    assert first["data"]["dedup"] is False
    assert first["data"]["delivery"] == "recorded_no_active_run"
    assert first["data"]["revision"] == 1
    dup = api.submit_supplement(task_id, event_id="m-100", note="重复消息", actor="alice")
    assert dup["data"]["dedup"] is True
    assert dup["data"]["delivery"] == "duplicate_ignored"
    assert dup["data"]["revision"] == 1
    # 在途 Run：说明进入持久补充队列，等待执行器在安全边界读取
    api.transition_work_item(task_id, to_state="queued", actor="alice")
    api.start_run(task_id, actor_id="bob")
    inflight = api.submit_supplement(task_id, event_id="m-101", note="再补一条", actor="alice")
    assert inflight["data"]["delivery"] == "queued_for_inflight_run"
    with _session(env) as session:
        rows = list(session.query(TaskSupplement).filter(
            TaskSupplement.task_id == task_id))
        assert {r.event_id for r in rows} == {"m-101"}


# ── 8. 暂停 / 取消传播 ──────────────────────────────────────────────────────

def test_pause_run_returns_task_to_pending(env):
    api = env.api_open
    task = _delegated(api)
    task_id = task["id"]
    started = api.start_run(task_id, actor_id="bob")
    run_id = started["data"]["operation_id"]
    paused = api.pause_run(run_id)
    assert paused["ok"] and paused["data"]["state"] == "paused"
    detail = api.get_work_item(task_id)
    # 排队暂停 = 退回待处理，不伪装进行中
    assert detail["data"]["state"] == "ready"
    assert detail["data"]["display"]["bucket"] == "pending"
    resumed = api.start_run(task_id, actor_id="bob")
    assert resumed["ok"], resumed
    assert resumed["data"]["operation_id"] == run_id
    assert resumed["data"]["state"] == "queued"


def test_pause_active_run_requires_worker_lease(env):
    api = env.api_open
    task = _delegated(api)
    task_id = task["id"]
    started = api.start_run(task_id, actor_id="bob")
    run_id = started["data"]["operation_id"]
    with _session(env) as session:
        claimed = execution.claim(session, run_id, worker="w1", validate_context=_allow)
    assert claimed is not None
    blocked = api.pause_run(run_id)
    assert blocked["ok"] is False
    assert blocked["reason_code"] == "run_lease_active"
    assert blocked["next_action"] == "worker_must_release_lease"


def test_cancel_propagates_and_fences_active_worker(env):
    api = env.api_open
    task = _delegated(api)
    task_id = task["id"]
    started = api.start_run(task_id, actor_id="bob")
    run_id = started["data"]["operation_id"]
    with _session(env) as session:
        claimed = execution.claim(session, run_id, worker="w1", validate_context=_allow)
        execution.transition(session, run_id, worker="w1",
                             generation=claimed.generation, state="running",
                             validate_context=_allow)
    cancelled = api.cancel_work_item(task_id, actor="alice", reason="需求撤销")
    assert cancelled["ok"], cancelled
    assert cancelled["data"]["fenced_runs"] == [run_id]
    with _session(env) as session:
        # worker 继续推进被版本围栏拒绝：取消及时可见
        with pytest.raises(ValueError, match="work_revision_changed"):
            execution.transition(session, run_id, worker="w1",
                                 generation=claimed.generation, state="failed",
                                 validate_context=_allow)
        assert session.query(TaskSupplement).filter(
            TaskSupplement.task_id == task_id,
            TaskSupplement.event_kind == "cancellation").count() == 1
    detail = api.get_work_item(task_id)
    assert detail["data"]["state"] == "cancelled"
    assert detail["data"]["display"]["reason_code"] == "cancelled"


# ── 9. 成本 / 健康分项 ──────────────────────────────────────────────────────

def test_cost_and_health_are_itemized_not_bare_ok(env):
    api = env.api
    cost = api.view_cost(org_id="org1")
    assert cost["ok"]
    assert cost["data"]["unknown"]["limits_configured"] is False
    assert cost["data"]["limits"] == []
    assert cost["data"]["at"]

    # 未配置限额：健康分项如实列出未知项，不假装有预算口径
    health = api.view_health("org1")
    assert health["ok"]
    data = health["data"]
    for key in ("database", "runs", "receipts", "waiting", "budget", "knowledge"):
        assert key in data
    assert data["database"]["reachable"] is True
    assert data["at"]
    assert "budget_limits" in data["unknown"]

    from evoharness.governance import budget as budget_mod
    with _session(env) as session:
        budget_mod.set_limit(session, scope_kind="org", scope_id="org1", limit_tokens=10000)
        reservation = budget_mod.reserve(session, org_id="org1", category="chat", tokens=1200)
        budget_mod.settle(session, reservation.reservation_id, used_tokens=900)
        budget_mod.reserve(session, org_id="org1", category="embedding", tokens=300)
    cost2 = api.view_cost(org_id="org1")
    assert cost2["data"]["used_total"] == 900
    assert cost2["data"]["reserved_open"] == 300
    assert cost2["data"]["by_category"] == {"chat": 900, "embedding": 0}
    assert cost2["data"]["failed_settled"] == 0
    assert cost2["data"]["unknown"]["limits_configured"] is True

    health2 = api.view_health("org1")
    assert health2["data"]["unknown"] == []
    assert health2["data"]["budget"]["limits_configured"] is True


def test_health_counts_waiting_and_unknown_receipts(env):
    api = env.api_open
    t = _delegated(api)["id"]
    api.transition_work_item(t, to_state="queued", actor="alice")
    api.transition_work_item(t, to_state="running", actor="bob")
    moved = api.transition_work_item(t, to_state="waiting_authorization", actor="bob",
                                     reason="缺批准")
    assert moved["ok"], moved
    with _session(env) as session:
        session.add(Outbox(intent_key="ik-health", tenant="t", kind="reply",
                           target_type="work_item", target_id=str(t),
                           payload_json="{}", status=OutboxStatus.DELIVERY_UNKNOWN))
    health = api.view_health()
    assert health["data"]["waiting"].get("waiting_authorization") == 1
    assert health["data"]["receipts"]["delivery_unknown"] == 1


# ── 10. CLI：JSON 输出与可读错误 ────────────────────────────────────────────

def _cli_main(args):
    from evoharness.cli import main as cli_main
    out = io.StringIO()
    code = cli_main(args, stdout=out)
    return code, out.getvalue()


def test_cli_json_output_and_exit_codes(env):
    code, out = _cli_main(["--db", env.db_url, "--json", "view", "--tenant", "t",
                           "--owner", "nobody"])
    payload = json.loads(out)
    assert code == 0
    assert payload["ok"] is True
    assert set(ENVELOPE_KEYS) <= set(payload)
    assert payload["data"]["counts"] == {"pending": 0, "in_progress": 0, "awaiting_me": 0,
                                         "delivered": 0, "cancelled": 0}


def test_cli_missing_db_is_readable_not_traceback(env, monkeypatch):
    monkeypatch.delenv("EVOHARNESS_DATABASE_URL", raising=False)
    code, out = _cli_main(["--json", "view", "--tenant", "t", "--owner", "a"])
    assert code == 2
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["reason_code"] == "database_not_configured"


def test_cli_readable_error_includes_retry_and_next_action(env):
    code, out = _cli_main(["--db", env.db_url, "item", "get", "--id", "424242"])
    assert code == 2
    assert "task_not_found" in out
    assert "可重试: 否" in out
    assert "下一步: check_object_id" in out


def test_cli_create_and_view_roundtrip(env):
    code, out = _cli_main([
        "--db", env.db_url, "--json", "item", "create",
        "--tenant", "t", "--owner", "alice", "--title", "CLI 建的工作项",
        "--goal", "目标", "--acceptance-json", '[{"kind":"code"}]',
        "--assignee-id", "bob", "--kind", "delegated_delivery", "--org", "org1",
    ])
    assert code == 0, out
    payload = json.loads(out)
    task_id = payload["data"]["id"]
    code, out = _cli_main(["--db", env.db_url, "--json", "item", "get", "--id", str(task_id)])
    detail = json.loads(out)
    assert detail["data"]["id"] == task_id
    assert detail["data"]["workflow_kind"] == "delegated_delivery"


def test_cli_approval_lifecycle(env):
    api = env.api
    task_id = _delegated(api)["id"]
    plan = api.run_request_payload(task_id)
    brief_json = json.dumps(plan["data"]["payload"], ensure_ascii=False)
    code, out = _cli_main([
        "--db", env.db_url, "--json", "approval", "submit", "--org", "org1",
        "--actor", "alice", "--target-type", "work_item", "--target-id", str(task_id),
        "--revision", "1", "--payload-json", brief_json, "--actions", "work.execute",
    ])
    assert code == 0, out
    decision = json.loads(out)["data"]
    code, out = _cli_main(["--db", env.db_url, "--json", "approval", "get",
                           "--decision-id", decision["decision_id"]])
    view = json.loads(out)["data"]
    assert view["identity"] == "runner"
    assert view["target_revision"] == 1
    code, out = _cli_main(["--db", env.db_url, "approval", "list", "--org", "org1"])
    assert "活跃批准 1 项" in out
    code, out = _cli_main(["--db", env.db_url, "--json", "approval", "revoke", "--org",
                           "org1", "--actor", "alice", "--target-type", "work_item",
                           "--target-id", str(task_id)])
    assert code == 0
    revoke = json.loads(out)["data"]
    assert revoke["revoked_decisions"] == 1
    # 没有排队发送意图 → 取消数为 0，如实回报而不是编造
    assert revoke["count"] == 0


# ── 11. server 侧派发：未配置库与未知操作 ───────────────────────────────────

def test_server_dispatch_degrades_without_database(monkeypatch):
    import server.app as server_app

    monkeypatch.setattr(server_app, "_WORK_API", None)
    monkeypatch.delenv("EVOHARNESS_DATABASE_URL", raising=False)
    result = server_app.work_dispatch("work_view", {"tenant": "t", "owner": "a"})
    assert result["ok"] is False
    assert result["reason_code"] == "database_not_configured"
    assert result["next_action"] == "configure_evo_harness_database_url"
    unknown = server_app.work_dispatch("no_such_op", {})
    assert unknown["reason_code"] == "unknown_operation"


def test_server_dispatch_rejects_params_outside_whitelist(env, monkeypatch):
    import server.app as server_app

    monkeypatch.setattr(server_app, "_WORK_API", None)
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL", env.db_url)
    # 白名单外的参数被丢弃：不会透传任意 kwargs
    result = server_app.work_dispatch("get_work_item", {"task_id": 99999,
                                                        "evil_param": "x"})
    assert result["ok"] is False
    assert result["reason_code"] == "task_not_found"


# ── 12. 前端静态资源一致性 ──────────────────────────────────────────────────

FRONTEND = REPO / "frontend" / "index.html"
SERVER_APP = REPO / "server" / "app.py"


def test_frontend_work_view_references_real_routes():
    import re
    text = FRONTEND.read_text(encoding="utf-8")
    refs = set(re.findall(r'["\'`](/api/work/[A-Za-z0-9/_-]*)["\'`]', text))
    server_text = SERVER_APP.read_text(encoding="utf-8")
    routes = {m for m in re.findall(r'@app\.(?:get|post)\("(/api/work/[^"]+)"\)', server_text)}
    refs = {r.rstrip("/") for r in refs}
    routes = {r.rstrip("/") for r in routes}
    assert refs, "工作视图应引用 /api/work/* 数据源"
    missing = sorted(refs - routes)
    assert not missing, f"前端引用了不存在的路由：{missing}"


def test_frontend_fields_exist_in_api_contract():
    text = FRONTEND.read_text(encoding="utf-8")
    import evoharness.api as api_module
    api_text = (REPO / "evoharness" / "api.py").read_text(encoding="utf-8")
    # 前端消费的契约字段必须在 api.py 中真实存在（不是前端单方面想象）
    for field in ("reason_code", "next_action", "retryable", "buckets", "awaiting_me",
                  "in_progress", "delivered", "cancelled", "payload_diff", "identity",
                  "expires_at", "artifacts", "receipts", "delivery_unknown",
                  "dryrun_not_live", "waiting_authorization", "awaiting_acceptance",
                  "receipt_unknown"):
        assert field in text, f"前端应使用字段 {field}"
        assert field in api_text, f"API 契约缺少前端引用的字段 {field}"


def test_panel_work_summary_uses_same_contract(env, monkeypatch):
    import panel.panel as panel_module

    monkeypatch.setattr(panel_module, "_WORK_API", None)
    monkeypatch.delenv("EVOHARNESS_DATABASE_URL", raising=False)
    degraded = panel_module.work_summary()
    assert degraded["ok"] is False
    assert degraded["reason_code"] == "database_not_configured"
    # 面板与工作台/CLI 读同一契约层
    monkeypatch.setattr(panel_module, "REPO", REPO)
    monkeypatch.setattr(panel_module, "_WORK_API", None)
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL", env.db_url)
    summary = panel_module.work_summary()
    assert summary["ok"] is True
    for key in ("runs", "receipts", "waiting", "budget"):
        assert key in summary["data"]


def test_panel_page_renders_work_summary_without_fake_data():
    panel_text = (REPO / "panel" / "panel.py").read_text(encoding="utf-8")
    assert "/api/work" in panel_text
    assert "database_not_configured" in panel_text
    # 不可用时显示原因码，不用空数据假装正常
    assert "不可用" in panel_text
