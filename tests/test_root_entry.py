"""DE-43 回归：EvoHarness 统一组织工作台与产品入口（根入口）。

覆盖 tasks.json DE-43 的 3 条验收场景 + 合成种子完整性（全部离线，
内存外均为临时 SQLite，无网络、无外部服务、无飞书配置）：

1. S1 无飞书也能在根入口初始化合成组织、派工、验收与接管；原个人会话仍可用；
2. S2 根 Web / CLI（evoharness.cli 与 python -m evoharness）/ api 对同一 Task
   一致权限、状态与真实回执（逐字段断言一致，不生成两份状态）；
3. S3 目标/责任人/等待原因/产物可定位，开发者和 Agent 可按 ID 查证据；
4. 合成种子完整性：2 单元 / 3 岗位 / 人+AI 任职 / 目标 / 岗位权限 /
   共享+私有知识 / 工作依赖与验收；无凭据类内容。

接线说明：根入口 evoharness/__main__.py 只做参数翻译，全部判定走
evoharness.api.WorkApi → 各领域服务；组织视图是只读投影，任务展示态
复用 WorkApi._view_row（唯一展示态映射），server /api/work/org 与根 CLI
共用同一 org_view 实现。
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from evoharness.__main__ import main as root_main  # noqa: E402
from evoharness.api import ENVELOPE_KEYS, WorkApi  # noqa: E402
from evoharness.application import Application  # noqa: E402
from evoharness.models import Outbox, OutboxStatus  # noqa: E402
from evoharness.work import acceptance, execution, objectives  # noqa: E402
from evoharness.work.models import TaskSupplement  # noqa: E402
from evoharness.work.service import transition_to  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # S1 前提：无飞书配置（相关环境变量一律不存在），业务库为临时 SQLite
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("EVOHARNESS_DATABASE_URL", raising=False)
    db_url = "sqlite:///" + (tmp_path / "root.db").as_posix()
    application = Application.open(db_url)
    api = WorkApi(application, artifacts_dir=tmp_path / "artifacts")
    return type("Env", (), {"tmp": tmp_path, "db_url": db_url, "app": application,
                            "api": api, "artifacts": tmp_path / "artifacts"})


def _allow(session, context, action):
    return True


def _root(env, *args):
    out = io.StringIO()
    code = root_main(["--db", env.db_url, "--json", *args], stdout=out)
    return code, json.loads(out.getvalue())


def _seed(env):
    code, result = _root(env, "init")
    assert code == 0, result
    assert result["ok"] is True
    assert set(ENVELOPE_KEYS) <= set(result)
    return result["data"]


def _session(env):
    return env.app.transaction()


# ── 合成种子完整性 ───────────────────────────────────────────────────────────

def test_seed_completeness_units_positions_memberships_objective_knowledge_plan(env):
    from evoharness.knowledge.models import KnowledgeClaim
    from evoharness.organization.models import (Assignment, EmployeeProfile,
                                                Membership, Position)
    from evoharness.work.plans import plan_overview

    summary = _seed(env)
    assert summary["created"] is True
    assert len(summary["units"]) == 2
    assert len(summary["positions"]) == 3
    assert summary["membership_kinds"] == ["ai_service", "human"]
    assert summary["assignments"] == 3
    assert summary["objective_ids"] == ["obj-demo"]
    assert sorted(summary["milestones"]) == ["ms-fix", "ms-triage"]
    assert len(summary["task_ids"]) == 2

    with _session(env) as session:
        # 人 / AI 任职并存，且都有在任指派（岗位不空缺）
        kinds = {m.principal_kind for m in session.query(Membership).all()}
        assert kinds == {"human", "ai_service"}
        assert session.query(EmployeeProfile).count() == 3
        assignments = session.query(Assignment).all()
        assert all(a.status == "active" for a in assignments)
        # 岗位权限：三类岗位各有动作上限与能力声明（岗位权限即显式授权交集）
        positions = session.query(Position).all()
        for p in positions:
            assert {"work.execute", "knowledge.read"} <= set(json.loads(p.action_ceiling_json))
        capabilities = set()
        for p in positions:
            capabilities |= set(json.loads(p.capability_refs_json))
        assert {"support.triage", "code.fix", "test.verify"} <= capabilities
        # 共享与私有知识各自有 scope；来源引用显式
        claims = session.query(KnowledgeClaim).all()
        scopes = {c.owner_scope for c in claims}
        assert "public" in scopes and "founder" in scopes
        assert all(c.source_refs_json for c in claims)
        # 工作依赖与验收：任务带目标与验收条件，计划表达 受理→修复 依赖
        tasks = {t.task_key: t for t in summary_task_rows(session)}
        assert all(t.goal.strip() and json.loads(t.acceptance_json) for t in tasks.values())
        overview = plan_overview(session, plan_id=summary["plan_id"])
        assert overview["status"] == "confirmed"
        node_by_task = {n["task_id"]: n for n in overview["nodes"]}
        fix_task = next(t for t in tasks.values() if "修复" in t.title)
        triage_task = next(t for t in tasks.values() if "受理" in t.title)
        assert node_by_task[fix_task.id]["depends_on"] == [triage_task.id]


def summary_task_rows(session):
    from evoharness.models import Task

    return list(session.query(Task).filter(Task.org_id == "org-demo").all())


def test_seed_has_no_credential_like_content_and_is_idempotent(env):
    _seed(env)
    forbidden = ("sk-", "api_key", "apikey", "app_secret", "password",
                 "token_key", "bearer ")
    with _session(env) as session:
        texts: list[str] = []
        for task in summary_task_rows(session):
            texts += [task.title, task.goal, task.acceptance_json]
        from evoharness.knowledge.models import KnowledgeClaim
        from evoharness.organization.models import Position
        texts += [p.mission for p in session.query(Position).all()]
        texts += [c.value for c in session.query(KnowledgeClaim).all()]
        for text in texts:
            assert not any(word in text.lower() for word in forbidden), text
    # 幂等：第二次 init 不重复写入（created=False，计数不变）
    first = _seed(env)
    again_code, again = _root(env, "init")
    assert again_code == 0 and again["data"]["created"] is False
    assert again["data"]["task_ids"] == first["task_ids"]


# ── S1：无飞书的根入口端到端（init → 派工 → 验收 → 接管）──────────────────────

def test_s1_root_entry_end_to_end_without_feishu(env):
    summary = _seed(env)
    triage_task, fix_task = summary["task_ids"]

    # ── 派工：批准 → 启动 Run（根入口 work 命令，走同一契约层）──
    code, approved = _root(env, "work", "approve", "--id", str(triage_task),
                           "--actor", "founder")
    assert code == 0, approved
    assert approved["data"]["actions"] == ["work.execute"]
    code, started = _root(env, "work", "start-run", "--id", str(triage_task),
                          "--actor", "ai-support",
                          "--output-root", str(env.tmp / "out-a"))
    assert code == 0, started
    run_a = started["data"]["operation_id"]
    assert started["data"]["task_state"] == "queued"

    with _session(env) as session:
        claimed = execution.claim(session, run_a, worker="w1", validate_context=_allow)
        execution.transition(session, run_a, worker="w1",
                             generation=claimed.generation, state="running",
                             validate_context=_allow)
        stale_generation = claimed.generation

    # ── 接管：有权用户从根入口接管在途 Run（围栏 + 在途回执对账）──
    code, takeover = _root(env, "work", "takeover", "--run-id", run_a,
                           "--actor", "founder", "--reason", "负责人接管在途工作")
    assert code == 0, takeover
    assert takeover["data"]["state"] == "waiting_recovery"
    assert takeover["data"]["takeover_by"] == "founder"
    assert takeover["data"]["receipt_id"]
    with _session(env) as session:
        # 旧 worker 世代/租约失效：不再新增动作
        with pytest.raises(ValueError):
            execution.transition(session, run_a, worker="w1",
                                 generation=stale_generation, state="failed",
                                 validate_context=_allow)
        from evoharness.organization.models import TenureReceipt
        receipt = session.get(TenureReceipt, takeover["data"]["receipt_id"])
        assert receipt is not None and receipt.status == "open"
        assert session.query(TaskSupplement).filter(
            TaskSupplement.task_id == triage_task,
            TaskSupplement.event_kind == "cancellation").count() == 1

    # ── 验收：修复任务走 Run → 独立验收 → done（done 唯一合法入口）──
    code, approved = _root(env, "work", "approve", "--id", str(fix_task),
                           "--actor", "founder")
    assert code == 0, approved
    code, started = _root(env, "work", "start-run", "--id", str(fix_task),
                          "--actor", "ai-support",
                          "--output-root", str(env.tmp / "out-b"))
    assert code == 0, started
    run_b = started["data"]["operation_id"]
    out_root = env.tmp / "out-b"
    (out_root / "diff.patch").write_text("patch", encoding="utf-8")
    (out_root / "test_report.txt").write_text("all pass", encoding="utf-8")
    with _session(env) as session:
        claimed = execution.claim(session, run_b, worker="w1", validate_context=_allow)
        transition_to(session, fix_task, to_state="running", actor="ai-support")
        execution.transition(session, run_b, worker="w1",
                             generation=claimed.generation, state="running",
                             validate_context=_allow)
        execution.transition(session, run_b, worker="w1",
                             generation=claimed.generation,
                             state="waiting_acceptance", validate_context=_allow)
        run_row = session.get(execution.ExecutionRun, run_b)
        acceptance.register_artifact(session, run_row, kind="diff",
                                     path=str(out_root / "diff.patch"))
        acceptance.register_artifact(session, run_row, kind="test_report",
                                     path=str(out_root / "test_report.txt"))
    code, requested = _root(env, "work", "request-acceptance", "--id", str(fix_task),
                            "--run-id", run_b, "--requester", "ai-support")
    assert code == 0, requested
    assert requested["data"]["state"] == "in_review"
    code, reviewed = _root(env, "work", "review", "--id", str(fix_task),
                           "--run-id", run_b, "--reviewer", "founder",
                           "--verdict", "pass", "--notes", "补丁与回归齐备")
    assert code == 0, reviewed
    assert reviewed["data"]["state"] == "done"
    # 结果返回目标：里程碑证据只绑定已验收交付
    with _session(env) as session:
        updated = objectives.record_task_evidence(session, task_id=fix_task,
                                                  actor_id="founder")
        assert [(m.milestone_id, m.status) for m in updated] == [("ms-fix", "evidenced")]
        progress = objectives.objective_progress(session, objective_id="obj-demo")
    assert progress["counts"]["evidenced"] == 1
    assert progress["percent_complete"] is None

    # ── 原个人会话路径仍可用：统一 CLI 的 legacy 待办与无组织工作项照常工作 ──
    from evoharness.cli import main as cli_main
    out = io.StringIO()
    code = cli_main(["--db", env.db_url, "--json", "item", "create",
                     "--tenant", "t", "--owner", "alice", "--title", "个人待办",
                     "--kind", "legacy_todo", "--assignee-id", "bob"], stdout=out)
    assert code == 0, out.getvalue()
    legacy_id = json.loads(out.getvalue())["data"]["id"]
    out = io.StringIO()
    code = cli_main(["--db", env.db_url, "--json", "item", "complete",
                     "--id", str(legacy_id), "--actor", "alice"], stdout=out)
    assert code == 0, out.getvalue()
    assert json.loads(out.getvalue())["data"]["state"] == "done"


def _open_session(env):
    return env.app.transaction()




def test_s2_same_task_consistent_across_root_web_cli_and_api(env, monkeypatch):
    import server.app as server_app
    from evoharness.cli import main as cli_main

    summary = _seed(env)
    task_id = summary["task_ids"][0]
    # 真实回执：一条停在 delivery_unknown 的发送意图（未知不是成功）
    with _session(env) as session:
        session.add(Outbox(intent_key="ik-de43", tenant="local", kind="reply",
                           target_type="work_item", target_id=str(task_id),
                           payload_json="{}", status=OutboxStatus.DELIVERY_UNKNOWN))

    api = env.api
    monkeypatch.setattr(server_app, "_WORK_API", api)

    def _entries():
        server_result = server_app.work_dispatch("get_work_item", {"task_id": task_id})
        out = io.StringIO()
        exit_code = cli_main(["--db", env.db_url, "--json", "item", "get",
                              "--id", str(task_id)], stdout=out)
        cli_result = json.loads(out.getvalue())
        out2 = io.StringIO()
        root_exit = root_main(["--db", env.db_url, "--json", "work", "get",
                               "--id", str(task_id)], stdout=out2)
        root_result = json.loads(out2.getvalue())
        return (exit_code, server_result, cli_result, root_exit, root_result)

    # 未批准：等待授权在三条入口同码、同可重试性、同下一步
    no_auth = api.start_run(task_id, actor_id="ai-support",
                            output_root=str(env.tmp / "r1"))
    assert no_auth["ok"] is False
    exit_code, server_result, cli_result, root_exit, root_result = _entries()
    for result in (server_result, cli_result, root_result):
        assert set(ENVELOPE_KEYS) <= set(result)
        assert result["ok"] is True
        assert result["data"]["state"] == "open"
    assert server_result["data"] == cli_result["data"] == root_result["data"] == \
        api.get_work_item(task_id)["data"]
    assert server_result["retryable"] == cli_result["retryable"]
    assert root_exit == exit_code == 0

    # 批准并启动：三条入口读到同一 queued 状态与同一展示态
    payload = api.run_request_payload(task_id)["data"]
    approved = api.submit_approval(org_id="org-demo", actor_id="founder",
                                   target_type="work_item", target_id=str(task_id),
                                   target_revision=payload["target_revision"],
                                   payload=payload["payload"],
                                   actions=["work.execute"])
    assert approved["ok"], approved
    started = api.start_run(task_id, actor_id="ai-support",
                            output_root=str(env.tmp / "r2"))
    assert started["ok"], started
    exit_code, server_result, cli_result, root_exit, root_result = _entries()
    base = api.get_work_item(task_id)
    assert server_result["data"] == cli_result["data"] == root_result["data"] == base["data"]
    for result in (server_result, cli_result, root_result, base):
        assert result["data"]["state"] == "queued"
        # 真实回执逐字段一致：未知回执在各入口都显示为回执未知（等待类绝不显示为完成）
        assert result["data"]["receipts"][0]["status"] == "delivery_unknown"
        assert result["data"]["display"]["reason_code"] == "receipt_unknown"
        assert result["data"]["display"]["bucket"] == "awaiting_me"
        assert result["data"]["display"]["next_action"] == \
            base["data"]["display"]["next_action"]
    assert root_exit == exit_code == 0

    # 工作视图分桶：api / CLI / server 三入口同一行数据
    view_api = api.work_view(tenant="local", owner="founder", org_id="org-demo")
    view_server = server_app.work_dispatch("work_view", {"tenant": "local",
                                                         "owner": "founder",
                                                         "org_id": "org-demo"})
    out = io.StringIO()
    cli_main(["--db", env.db_url, "--json", "view", "--tenant", "local",
              "--owner", "founder", "--org", "org-demo"], stdout=out)
    view_cli = json.loads(out.getvalue())
    def _row(view):
        return next(r for r in view["data"]["buckets"]["awaiting_me"]
                    if r["id"] == task_id)
    assert _row(view_server) == _row(view_cli) == _row(view_api)

    # 组织视图：server /api/work/org 与根 CLI org view 共用同一实现，状态一致
    route_result = asyncio.run(server_app.work_org_view(None, org="org-demo"))
    out = io.StringIO()
    root_main(["--db", env.db_url, "--json", "org", "view", "--org", "org-demo"],
              stdout=out)
    root_org = json.loads(out.getvalue())
    assert route_result == root_org
    org_task = next(r for r in route_result["data"]["tasks"] if r["id"] == task_id)
    assert org_task["display"] == base["data"]["display"]
    assert org_task["state"] == base["data"]["state"]


# ── S3：目标/责任人/等待原因/产物可定位，按 ID 查证据 ─────────────────────────

def test_s3_objective_owner_waiting_reason_artifacts_locatable(env, monkeypatch):
    import server.app as server_app

    summary = _seed(env)
    triage_task, fix_task = summary["task_ids"]
    api = env.api
    monkeypatch.setattr(server_app, "_WORK_API", api)

    # 等待原因：支持任务显式进入等待并记录原因（open→queued→running→waiting_external）
    assert api.transition_work_item(triage_task, to_state="queued",
                                    actor="founder")["ok"]
    assert api.transition_work_item(triage_task, to_state="running",
                                    actor="founder")["ok"]
    moved = api.transition_work_item(triage_task, to_state="waiting_external",
                                     actor="founder",
                                     reason="等待客户提供复现环境")
    assert moved["ok"], moved
    # 产物：修复任务走验收并登记产物
    payload = api.run_request_payload(fix_task)["data"]
    api.submit_approval(org_id="org-demo", actor_id="founder",
                        target_type="work_item", target_id=str(fix_task),
                        target_revision=payload["target_revision"],
                        payload=payload["payload"], actions=["work.execute"])
    run_b = api.start_run(fix_task, actor_id="ai-support",
                          output_root=str(env.tmp / "out-s3"))["data"]["operation_id"]
    out_root = env.tmp / "out-s3"
    (out_root / "diff.patch").write_text("patch-s3", encoding="utf-8")
    (out_root / "test_report.txt").write_text("pass", encoding="utf-8")
    with _session(env) as session:
        claimed = execution.claim(session, run_b, worker="w1", validate_context=_allow)
        transition_to(session, fix_task, to_state="running", actor="ai-support")
        execution.transition(session, run_b, worker="w1",
                             generation=claimed.generation, state="running",
                             validate_context=_allow)
        execution.transition(session, run_b, worker="w1",
                             generation=claimed.generation,
                             state="waiting_acceptance", validate_context=_allow)
        run_row = session.get(execution.ExecutionRun, run_b)
        acceptance.register_artifact(session, run_row, kind="diff",
                                     path=str(out_root / "diff.patch"))
        acceptance.register_artifact(session, run_row, kind="test_report",
                                     path=str(out_root / "test_report.txt"))
    api.request_acceptance(fix_task, run_id=run_b, requester="ai-support")
    assert api.review_acceptance(fix_task, run_id=run_b, reviewer="founder",
                                 verdict="pass")["ok"]
    # 结果返回目标：里程碑证据由验收后的交付回写（聊天完成不能替代）
    with _session(env) as session:
        objectives.record_task_evidence(session, task_id=fix_task, actor_id="founder")

    # 组织视图里目标/责任人/等待原因/产物都可定位（用户能找到）
    view = asyncio.run(server_app.work_org_view(None, org="org-demo"))
    assert view["ok"], view
    data = view["data"]
    objective = data["objectives"][0]
    assert objective["owner"] == "founder"
    assert objective["milestones"] and objective["counts"]["evidenced"] == 1
    rows = {row["id"]: row for row in data["tasks"]}
    waiting_row = rows[triage_task]
    assert waiting_row["waiting_reason"] == "等待客户提供复现环境"
    assert waiting_row["display"]["reason_code"] == "waiting_external"
    assert waiting_row["goal"] and waiting_row["owner"] == "founder"
    delivered_row = rows[fix_task]
    artifacts = delivered_row["artifacts"]
    assert {a["kind"] for a in artifacts} == {"diff", "test_report"}
    assert all(a["sha256"] and a["path"] for a in artifacts)

    # 开发者 / Agent 按 ID 查证据：view_evidence 返回可复核引用
    evidence = api.view_evidence(fix_task)
    assert evidence["ok"], evidence
    assert {a["kind"] for a in evidence["data"]["artifacts"]} == {"diff", "test_report"}
    assert evidence["data"]["source_refs"] == delivered_row["source_refs"]
    assert any(e["event"] == "acceptance_passed" for e in evidence["data"]["events"])
    raw = Path(artifacts[0]["path"]).read_bytes()
    import hashlib
    assert hashlib.sha256(raw).hexdigest() == artifacts[0]["sha256"]
    assert str(triage_task) in view["evidence_refs"]
    # CLI 同一证据入口（Agent 可用）
    out = io.StringIO()
    root_main(["--db", env.db_url, "--json", "work", "evidence", "--id",
               str(fix_task)], stdout=out)
    cli_evidence = json.loads(out.getvalue())
    assert cli_evidence["data"]["artifacts"] == evidence["data"]["artifacts"]


# ── doctor：健康与配置检查 ────────────────────────────────────────────────────

def test_doctor_reports_itemized_checks_without_feishu(env, monkeypatch):
    _seed(env)
    code, result = _root(env, "doctor")
    assert code == 0, result
    assert set(ENVELOPE_KEYS) <= set(result)
    checks = {c["name"]: c for c in result["data"]["checks"]}
    assert checks["database"]["status"] == "ok"
    assert checks["organization"]["status"] == "seeded"
    assert checks["receipts"]["status"] == "ok"
    # 根入口流程不依赖飞书：doctor 不因缺飞书配置而失败
    assert result["data"]["channel_feishu_required_for_root_flow"] is False
    # 未配置业务库：可读错误而非 traceback
    monkeypatch.delenv("EVOHARNESS_DATABASE_URL", raising=False)
    out = io.StringIO()
    code = root_main(["--json", "doctor"], stdout=out)
    assert code == 2
    assert json.loads(out.getvalue())["reason_code"] == "database_not_configured"


# ── serve：根 Web 服务组装 ────────────────────────────────────────────────────

def test_serve_wires_database_and_starts_web_app(env, monkeypatch):
    import uvicorn

    calls = {}

    def fake_uvicorn_run(app, host=None, port=None):
        calls["host"], calls["port"], calls["app"] = host, port, app

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    from server.app import app as web_app
    code, result = _root(env, "serve", "--host", "127.0.0.1", "--port", "8123")
    assert code == 0
    assert calls == {"host": "127.0.0.1", "port": 8123, "app": web_app}


def test_serve_without_database_is_readable(env, monkeypatch):
    monkeypatch.delenv("EVOHARNESS_DATABASE_URL", raising=False)
    out = io.StringIO()
    code = root_main(["serve"], stdout=out)
    assert code == 2
    assert "database_not_configured" in out.getvalue()


# ── 旧入口兼容转发与个人会话不受影响 ─────────────────────────────────────────

def test_agents_cli_forwards_root_commands_and_keeps_prompt_mode(monkeypatch, capsys):
    from agents import main as agents_main

    monkeypatch.setattr(sys, "argv", ["evoharness", "fix the bug"])
    args = agents_main.parse_args()
    assert args.root is None
    assert args.prompt == ["fix the bug"]

    monkeypatch.setenv("EVOHARNESS_DATABASE_URL", "sqlite:///:memory:")
    code = agents_main._forward_to_root(["doctor", "--json"])
    captured = capsys.readouterr().out
    assert code == 0
    assert "[forward]" in captured
    assert '"ok": true' in captured
    assert "checks" in captured


def test_agents_cli_forward_uses_root_entry_for_org_view(env, monkeypatch, capsys):
    from agents import main as agents_main

    _seed(env)
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL", env.db_url)
    code = agents_main._forward_to_root(["org", "view", "--org", "org-demo",
                                         "--json"])
    captured = capsys.readouterr().out
    assert code == 0
    payload = json.loads(captured[captured.index("{"):])
    assert payload["ok"] is True
    assert payload["data"]["counts"]["positions"] == 3


# ── 前端组织导航：四类导航存在且引用真实路由 ─────────────────────────────────

def test_frontend_org_navigation_references_real_routes():
    import re

    frontend_text = (REPO / "frontend" / "index.html").read_text(encoding="utf-8")
    for view in ("org", "goals", "knowledge"):
        assert f'data-view="{view}"' in frontend_text, view
    for route in ("/api/work/org", "/api/work/knowledge", "/api/work/view"):
        assert route in frontend_text, route
    server_text = (REPO / "server" / "app.py").read_text(encoding="utf-8")
    routes = set(re.findall(r'@app\.(?:get|post)\("(/api/work/[^"]+)"\)', server_text))
    refs = {m for m in re.findall(r'["\'`](/api/work/[A-Za-z0-9/_-]*)["\'`]',
                                  frontend_text)}
    missing = sorted({r.rstrip("/") for r in refs}
                     - {r.rstrip("/") for r in routes})
    assert not missing, f"前端引用了不存在的路由：{missing}"
    # 组织/目标/知识视图与工作视图同属一个产品页面，不另建数字员工后台
    assert "数字员工后台" not in frontend_text


def test_server_org_route_degrades_without_database(monkeypatch):
    import server.app as server_app

    monkeypatch.setattr(server_app, "_WORK_API", None)
    monkeypatch.delenv("EVOHARNESS_DATABASE_URL", raising=False)
    result = asyncio.run(server_app.work_org_view(None, org="org-demo"))
    assert result["ok"] is False
    assert result["reason_code"] == "database_not_configured"
    missing = asyncio.run(server_app.work_org_view(None, org=""))
    assert missing["reason_code"] == "org_id_required"


def test_serve_uses_explicit_database_for_http_requests(tmp_path, monkeypatch):
    import uvicorn
    from fastapi.testclient import TestClient
    from server import app as web
    target = tmp_path / "fresh" / "data" / "org.db"
    database_url = "sqlite:///" + target.as_posix()
    out = io.StringIO()
    assert root_main(["--db", database_url, "init"], stdout=out) == 0
    previous = web._WORK_API
    monkeypatch.delenv("EVOHARNESS_DATABASE_URL", raising=False)

    def run(app, host, port):
        assert Path(web._get_work_api()._app.engine.url.database).resolve() == target.resolve()
        with TestClient(app) as client:
            response = client.get("/api/work/org?org=org-demo")
        assert response.status_code == 200
        assert response.json()["data"]["org_id"] == "org-demo"

    monkeypatch.setattr(uvicorn, "run", run)
    assert root_main(["--db", database_url, "serve"], stdout=out) == 0
    assert web._WORK_API is previous
