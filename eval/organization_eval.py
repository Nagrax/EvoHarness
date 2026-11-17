#!/usr/bin/env python3
"""EvoHarness 最终组织验收编排器（DE-44，指南 §21 八个核心场景）。

定位（与 eval/digital_employee_gate.py 分工）：
- 门禁（digital_employee_gate.py）回答"回归是否守住"；本脚本回答"指南 §21 的
  组织场景在当前树上离线可验证到什么程度"。它组合真实执行最小行为（临时
  SQLite、无网络、无飞书）与既有回归证据引用，逐场景输出可复核结论。
- 诚实口径：离线可验证 → verified_offline；场景含本机不可执行部分 → 在
  not_run_live 单列，绝不合并成"通过"；本脚本退出 0 不代表产品发布、远端
  仓库可读或 live 试点完成（指南 §22/§23/§24）。
- R03（2026-11-15）：S1/S4 交付执行段改为真实链——显式批准（grantee 绑定）→
  start_run → 生产 Worker 认领（预算预留）→ 受限 Agent 适配器在隔离工作区
  执行（FakeProvider 脚本化回复：工具真实读写、检查真实运行、预算逐笔
  预留+结算）→ R07 产物完整性验收 → done。场景数据仍合成，但执行链真实；
  合成文件推进与 _always_allow 授权替代已移除。

场景映射（docs/DIGITAL-EMPLOYEE-EXECUTION-GUIDE.md §21 的 8 条最终核心场景）：
  S1 无飞书根入口初始化合成组织；一个目标跨两单元三岗位协作交付（DE-43 种子
     + DE-28 契约层 + DE-39 里程碑证据）。
  S2 离任/撤权围栏：旧 Run 不再新增动作、在途回执对账、接任不继承个人 token
     （DE-38 任职围栏 + DE-41 统一接管）。
  S3 工作依赖、共享资源、取消与失败恢复、重复消息不多建任务（DE-39 计划依赖
     + DE-40 资源租约 + DE-14 补充幂等 + DE-11 状态机恢复路径）。
  S4 目标只由验收证据推进；聊天完成不能批量关闭；关键成果失效可定位影响
     （DE-13 唯一 done 入口 + DE-39 里程碑证据/失效）。
  S5 跨单元共享尊重来源权限；投影不覆盖已采用 SOP；能力升级要真实证据
     （DE-16 知识裁剪 + DE-42 能力证据/投影护栏）。
  S6 根 Web/CLI/飞书同一对象一致；核心无飞书 SDK 反向依赖；无第二写库
     （DE-37 单写者 + DE-43 四入口一致）。
  S7 费用/等待/失败/人工决定有归属；预算耗尽停止新调用；接管可恢复
     （DE-15 预算 + DE-21 等待 + DE-41 接管）。
  S8 clean checkout 样例与 CI/发布证据（结构化报告 + 三态交付表；真实
     runner/发布 not_run_live）。

用法：
  python eval/organization_eval.py                     # 全场景，打印 JSON
  python eval/organization_eval.py --only S1,S6        # 选场景
  python eval/organization_eval.py --output report.json --markdown report.md
  python eval/organization_eval.py --clean-checkout-report cc.json
                                                       # 并入 clean checkout 实测

退出码：0 全部所选场景离线验证通过（not_run_live 单列不算失败）；
1 存在场景 failed（真实行为断言未通过或结构检查未通过）；4 用法/内部错误。
"""
from __future__ import annotations

import argparse
import ast
import datetime as _dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

GUIDE_REF = "README.md#从目标到交付"
FINAL_ACCEPTANCE_REF = "eval/acceptance_check.py"

# Offline inventory cannot establish whether a git push or release occurred.
REMOTE_STATE = "远端状态未知；未推送或已推送均须在线核验（离线工具不执行 git push）"

DELIVERABLE_PATHS = [
    "README.md", "docs/OPERATIONS.md", "docs/AGENT-HANDOFF.md",
    "docs/digital-employee/contracts.schema.json",
    "docs/digital-employee/search-design-fixtures.json",
    "evoharness/__main__.py", "evoharness/api.py",
    "eval/digital_employee_gate.py", "eval/organization_eval.py",
    "eval/acceptance_check.py", "tests/test_organization_acceptance.py",
    "tests/delivery_fixtures.py", "tests/fixtures/loop-backlog.json",
    ".github/workflows/ci.yml",
]

# clean checkout 失败归类（S8）：可分发缺口应补 fixture；live 依赖单列不冒充。
FAILURE_CLASSES = ("missing_evidence_artifact", "live_dependency",
                   "distributable_fixture_gap", "regression")

CLEAN_CHECKOUT_SUITES = ("core", "feishu", "integration")

# 核心包不得反向依赖的飞书 SDK 顶层模块名（指南 §21 场景 6）。
FEISHU_SDK_MODULES = ("feishu", "lark_oapi", "lark")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(root: Path, *args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(["git", "-C", str(root), *args],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30,
                                check=False)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


# ---------------------------------------------------------------------------
# 隔离执行环境：临时 SQLite + 根服务层（无飞书、无网络）。


class ScenarioEnv:
    """一个场景一个临时库；全部走仓库内真实服务实现。"""

    def __init__(self, workdir: Path):
        from evoharness.api import WorkApi
        from evoharness.application import Application
        self.tmp = workdir
        self.db_url = "sqlite:///" + (workdir / "org-eval.db").as_posix()
        self.application = Application.open(self.db_url)
        # 注意：work_resource_leases 表未注册进 evoharness/migrations.py 注册清单
        # （见 collect_findings 的 F-N1），生产 Application.open 不建该表；
        # 验收库按测试同一路径补齐（模块导入 → create_all），缺口在报告单列。
        import evoharness.work.scheduling  # noqa: F401
        from evoharness.models import Base
        Base.metadata.create_all(self.application.engine)
        self.api = WorkApi(self.application, artifacts_dir=workdir / "artifacts")
        # R03：真实执行链装配——Worker + 受限 Agent 适配器 + 预算网关
        # （场景数据仍合成，但执行链是真实的：FakeProvider 脚本化回复，
        #  工具真实读写、检查真实运行、预算逐笔预留+结算）
        from evoharness.governance import budget_gateway
        from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
        from evoharness.work.worker import WorkWorker, WorkerConfig
        from evoharness.workflows.code_fix import ScriptTestRunner
        self.provider = FakeProvider(_provider_script)
        self.gateway = budget_gateway.BudgetGateway(self.application.session_factory)
        self.adapter = RestrictedAgentAdapter(
            provider=self.provider, gateway=self.gateway,
            runner=ScriptTestRunner([sys.executable, "-c",
                                     "import pathlib, sys; "
                                     "sys.exit(0 if pathlib.Path('src/gateway_timeout.py')"
                                     ".is_file() else 1)"]),
            checker="org-eval")
        self.worker = WorkWorker(self.application.session_factory, self.adapter,
                                 self.api._authorize_adapter(),
                                 config=WorkerConfig(worker_id="w-org-eval"))

    def session(self):
        return self.application.transaction()

    def close(self):
        self.application.close()


def _seed(env: ScenarioEnv) -> dict:
    from evoharness.__main__ import seed_organization
    with env.session() as session:
        return seed_organization(session)


def _owner_principal():
    from evoharness.organization.service import AuthenticatedPrincipal
    return AuthenticatedPrincipal(
        principal_id="founder", tenant_id="local", app_id="root", authenticated=True,
        authorities=frozenset({"organization.bootstrap", "organization.manage",
                               "work.execute", "knowledge.read",
                               "objective.manage", "plan.confirm"}))


def _run_authorizer(env: ScenarioEnv):
    """Run 链授权适配器：WorkApi 默认适配器（R02：批准决定 × grantee×组织×版本×载荷绑定）。

    R03：不再用 _always_allow 绕过授权——认领/状态转换全部经真实授权重验。
    """
    return env.api._authorize_adapter()


def _ensure_budget(env: ScenarioEnv) -> None:
    """R03：真实执行链需要预算配置（未配置的生产空间不自动付费，fail-closed）。"""
    from evoharness.governance import budget as budget_svc
    with env.session() as session:
        budget_svc.set_limit(session, scope_kind="org", scope_id="org-demo",
                             limit_tokens=100000)
        budget_svc.set_limit(session, scope_kind="space", scope_id="space-support",
                             limit_tokens=50000)
        budget_svc.set_limit(session, scope_kind="space", scope_id="space-internal",
                             limit_tokens=50000)


def _provider_script(context) -> list:
    """R03 离线场景剧本：按交付声明的 acceptance kind 选择受限 Agent 行为。

    场景数据（任务目标/验收）仍由种子合成；执行链真实——脚本经预算网关逐幕
    回复，工具真实读写隔离工作区，检查真实运行，产物带 R07 检查回执。
    """
    brief = context.get("brief") or {}
    kinds = {str(item.get("kind")) for item in (brief.get("acceptance") or [])
             if isinstance(item, dict)}
    if "code" in kinds:
        return [
            {"tool_calls": [{"name": "write_file", "arguments": {
                "path": "src/gateway_timeout.py",
                "content": "TIMEOUT_SECONDS = 5  # 修复：登录网关超时从 30s 收敛为 5s\n"}}],
             "content": "最小修复已写入隔离工作区", "usage_tokens": 150},
            {"content": "修复完成，等待真实门禁", "final": True, "usage_tokens": 90},
        ]
    return [
        {"tool_calls": [{"name": "list_files", "arguments": {}}],
         "content": "查阅知识口径", "usage_tokens": 100},
        {"content": "定位结论：登录超时为网关超时配置问题，建议收敛超时并补回归。",
         "final": True, "usage_tokens": 80},
    ]


def _approve_task(env: ScenarioEnv, task_id, *, grantee: str = "ai-support") -> dict:
    """根入口 work approve 的同一流程：run_request_payload → submit_approval。

    R03：显式批准绑定 grantee（R02 语义）——执行主体与批准主体绑定，
    授权重验按 grantee×组织×目标×版本×载荷×动作核对。
    """
    payload = env.api.run_request_payload(task_id)
    assert payload.get("ok") is True, payload
    approved = env.api.submit_approval(
        org_id="org-demo", actor_id="founder", target_type="work_item",
        target_id=str(task_id), target_revision=payload["data"]["target_revision"],
        payload=payload["data"]["payload"], actions=["work.execute"],
        grantee_id=grantee)
    assert approved.get("ok") is True, approved
    return approved


def _run_to_waiting_acceptance(env: ScenarioEnv, task_id, *, actor: str,
                               out_dir: Path) -> str:
    """R03 真实执行链：start_run（queued）→ Worker 认领（预算预留）→
    受限 Agent 在隔离工作区执行（FakeProvider，工具受限、产出真实产物 +
    R07 检查回执）→ waiting_acceptance。不再合成文件推进。"""
    started = env.api.start_run(task_id, actor_id=actor, output_root=str(out_dir))
    assert started.get("ok") is True, started
    run_id = started["data"]["operation_id"]
    result = env.worker.process_run(run_id)
    assert result.get("ok") is True and result.get("run_state") == "waiting_acceptance", \
        result
    return run_id


def _full_delivery(env: ScenarioEnv, task_id, *, out_dir: Path, actor: str,
                   reviewer: str) -> dict:
    """一次"批准(grantee 绑定)→Run→Worker 真实执行→产物→独立验收→done"的真实接力。

    R03：执行段换为真实 Worker + 受限 Agent 适配器（FakeProvider 离线），
    预算逐笔预留+结算；reviewer ≠ executor 的独立验收语义不变。
    """
    requested = None
    _ensure_budget(env)
    _approve_task(env, task_id)
    run_id = _run_to_waiting_acceptance(env, task_id, actor=actor, out_dir=out_dir)
    requested = env.api.request_acceptance(task_id, run_id=run_id, requester=actor)
    assert requested.get("ok") is True, requested
    reviewed = env.api.review_acceptance(task_id, run_id=run_id, reviewer=reviewer,
                                         verdict="pass", notes="组织验收编排器复核")
    assert reviewed.get("ok") is True and reviewed["data"]["state"] == "done", reviewed
    from evoharness.work import objectives
    with env.session() as session:
        # 验收后回写任务覆盖的里程碑证据（DE-39 语义：只回写覆盖项，幂等）
        objectives.record_task_evidence(session, task_id=task_id, actor_id=reviewer)
    return {"task_id": str(task_id), "run_id": run_id, "reviewer": reviewer,
            "state": reviewed["data"]["state"]}


def _seed_task_ids(env: ScenarioEnv) -> list:
    from evoharness.models import Task
    with env.session() as session:
        rows = session.query(Task).filter_by(org_id="org-demo").order_by(Task.id).all()
        return [row.id for row in rows]


# ---------------------------------------------------------------------------
# S1：无飞书根入口初始化合成组织；一个目标跨两单元三岗位协作交付。


def check_s1(env: ScenarioEnv) -> dict:
    from evoharness.work import objectives
    from evoharness.work.plans import plan_overview

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    summary = _seed(env)
    check("seed_without_feishu_credentials",
          summary["created"] is True and len(summary["units"]) == 2
          and len(summary["positions"]) == 3,
          f"units={summary['units']} positions={summary['positions']}")
    check("human_and_ai_memberships", summary["membership_kinds"] == ["ai_service", "human"],
          str(summary["membership_kinds"]))
    with env.session() as session:
        overview = plan_overview(session, plan_id=summary["plan_id"])
    check("plan_dependency_dag_confirmed", overview["status"] == "confirmed"
          and any(n["depends_on"] for n in overview["nodes"]),
          f"status={overview['status']}")
    task_ids = _seed_task_ids(env)
    first = _full_delivery(env, task_ids[0], out_dir=env.tmp / "out-s1-a",
                           actor="ai-support", reviewer="founder")
    second = _full_delivery(env, task_ids[1], out_dir=env.tmp / "out-s1-b",
                            actor="ai-support", reviewer="founder")
    check("two_unit_three_position_delivery",
          first["state"] == "done" and second["state"] == "done"
          and first["reviewer"] == "founder",
          f"triage={first['task_id']} fix={second['task_id']} reviewer={first['reviewer']}")
    with env.session() as session:
        progress = objectives.objective_progress(session, objective_id="obj-demo")
    check("objective_milestones_evidenced_by_acceptance",
          progress["counts"].get("evidenced") == 2, json.dumps(progress["counts"]))
    return {"checks": checks}


# ---------------------------------------------------------------------------
# S2：离任/撤权围栏 + 接任不继承个人 token。


def check_s2(env: ScenarioEnv) -> dict:
    import json as _json

    from evoharness.governance import decisions
    from evoharness.organization import service as org
    from evoharness.organization.models import (Assignment, EmployeeProfile,
                                                Membership, TenureReceipt)
    from evoharness.organization.service import AuthenticatedPrincipal
    from evoharness.work import execution, supplements
    from evoharness.work.service import transition_to

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    def _principal(pid: str) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(principal_id=pid, tenant_id="local",
                                      app_id="root", authenticated=True,
                                      authorities=frozenset())

    def _org_validator(principal: AuthenticatedPrincipal):
        """work.execution 的授权适配器：组织授权上下文失效即拒绝。"""

        def validate(session, context, action):
            try:
                org.validate_context(session, org.ResolvedContext(context, principal),
                                     action)
                return True
            except org.OrganizationError:
                return False

        return validate

    _seed(env)
    owner = _owner_principal()
    triage_id = _seed_task_ids(env)[0]
    ai_support = _principal("ai-support")
    with env.session() as session:
        from evoharness.models import Task
        task = session.get(Task, triage_id)
        transition_to(session, triage_id, to_state="queued", actor="founder")
        context = org.resolve_context(session, ai_support, org_id="org-demo",
                                      space_id=task.space_id,
                                      assignment_id="asg-support").values
        run1 = execution.request_run(
            session, task_id=triage_id, context=dict(context),
            actor_id="ai-support", output_root=str(env.tmp / "out-s2"),
            idempotency_key="org-eval-s2-run1",
            validate_context=_org_validator(ai_support))
        run2 = execution.request_run(
            session, task_id=triage_id, context=dict(context),
            actor_id="ai-support", output_root=str(env.tmp / "out-s2"),
            idempotency_key="org-eval-s2-run2",
            validate_context=_org_validator(ai_support))
        claimed = execution.claim(session, run1.id, worker="w-alice",
                                  validate_context=_org_validator(ai_support))
        execution.transition(session, run1.id, worker="w-alice",
                             generation=claimed.generation, state="running",
                             validate_context=_org_validator(ai_support))
        stale_generation = claimed.generation
        # 离任/撤权：组织级撤销 ai-support 任职（围栏 + 取消传播 + 回执对账）
        member = session.get(Membership, "m-ai-support")
        _, event = org.transition(
            session, owner, org_id="org-demo", kind="membership",
            target_id="m-ai-support", status="revoked",
            expected_revision=member.revision,
            lifecycle_effects=org.default_offboard_effects)
        fenced = set(_json.loads(event.detail_json).get("fenced_run_ids", []))
        check("revocation_fences_running_run", run1.id in fenced, f"fenced={sorted(fenced)}")
        try:
            execution.transition(session, run1.id, worker="w-alice",
                                 generation=stale_generation, state="failed",
                                 validate_context=_org_validator(ai_support))
            fenced_after = "no_error_raised"
        except ValueError as exc:
            fenced_after = str(exc)
        check("fenced_run_cannot_continue",
              "authorization_invalid" in fenced_after, fenced_after)
        try:
            execution.claim(session, run2.id, worker="w-alice",
                            validate_context=_org_validator(ai_support))
            claim_after = "no_error_raised"
        except ValueError as exc:
            claim_after = str(exc)
        check("fenced_run_cannot_be_claimed",
              "authorization_invalid" in claim_after, claim_after)
        receipt_rows = [r for r in session.query(TenureReceipt).all() if r.status == "open"]
        check("in_flight_receipt_reconciliation_open",
              any(r.target_id == run1.id for r in receipt_rows),
              str([(r.target_id, r.status) for r in receipt_rows]))
        assignment_rows = session.query(Assignment).filter(
            Assignment.employee_id == "emp-support").all()
        check("offboard_ends_assignments",
              bool(assignment_rows)
              and all(a.status == "ended" for a in assignment_rows),
              str([(a.assignment_id, a.status) for a in assignment_rows]))
        # 接任：新成员/新任职接同一岗位；档案不含任何凭据列（个人 token 不迁移）
        org.add_member(session, owner, org_id="org-demo", membership_id="m-successor",
                       principal_id="successor-ai", tenant_id="local", app_id="root",
                       grants={"work.execute", "knowledge.read"},
                       principal_kind="ai_service")
        org.create_employee(session, owner, org_id="org-demo", employee_id="emp-successor",
                            membership_id="m-successor", display_name="接任·数字员工")
        org.assign(session, owner, org_id="org-demo", assignment_id="asg-successor",
                   position_id="pos-support", employee_id="emp-successor",
                   space_ids={"space-support"},
                   actions={"work.execute", "knowledge.read"}, budget_limit=100)
        profile = session.get(EmployeeProfile, "emp-successor")
        columns = {c for c in profile.__table__.columns.keys()}
        tokenish = [c for c in columns if any(word in c for word in
                                              ("token", "secret", "credential", "key"))]
        check("successor_has_no_personal_token_columns", not tokenish,
              f"profile_columns={sorted(columns)} credential_like={tokenish}")
        # 统一接管：在途 Run 围栏 + 开对账单（未知回执不写成功）
        takeover = decisions.takeover_run(session, owner, org_id="org-demo",
                                          run_id=run1.id,
                                          reason="负责人接管离任在途工作")
        check("takeover_moves_run_to_waiting_recovery",
              takeover["state"] == "waiting_recovery" and takeover["generation"] >= 2,
              str(takeover))
        receipt = session.get(TenureReceipt, takeover["receipt_id"])
        check("takeover_receipt_open_not_fake_success",
              receipt is not None and receipt.status == "open",
              str(receipt and receipt.status))
        cancellations = [row for row in supplements.pending(session, triage_id)
                         if row.event_kind == "cancellation"]
        check("cancellation_propagated_on_offboard_or_takeover",
              len(cancellations) >= 1, f"cancellations={len(cancellations)}")
        # 接任者经合法任职在同一任务上开新 Run（组织产物可接手）
        successor = _principal("successor-ai")
        fresh = org.resolve_context(session, successor, org_id="org-demo",
                                    space_id=task.space_id,
                                    assignment_id="asg-successor").values
        run3 = execution.request_run(
            session, task_id=triage_id, context=dict(fresh),
            actor_id="successor-ai", output_root=str(env.tmp / "out-s2-successor"),
            idempotency_key="org-eval-s2-run3",
            validate_context=_org_validator(successor))
        claimed3 = execution.claim(session, run3.id, worker="w-new",
                                   validate_context=_org_validator(successor))
        check("successor_can_start_new_run", claimed3 is not None,
              f"run3={run3.id} claimed={bool(claimed3)}")
    return {"checks": checks}


# ---------------------------------------------------------------------------
# S3：依赖/共享资源/取消/失败恢复/重复消息幂等。


def check_s3(env: ScenarioEnv) -> dict:
    from evoharness.models import Task
    from evoharness.work import scheduling, supplements
    from evoharness.work.execution import ExecutionRun
    from evoharness.work.state import TRANSITIONS

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    _seed(env)
    task_ids = _seed_task_ids(env)
    triage_id, fix_id = task_ids[0], task_ids[1]
    with env.session() as session:
        # ① 工作依赖：修复任务带目标与验收（受理→修复依赖由计划 DAG 表达，S1 断言）
        task_fix = session.get(Task, fix_id)
        check("fix_task_has_goal_and_acceptance",
              bool(task_fix.goal) and json.loads(task_fix.acceptance_json),
              f"goal={task_fix.goal[:24]}")
        # ② 重复消息不多建任务：同 (task, event) 幂等，返回同一行
        first = supplements.enqueue(session, triage_id, event_id="msg-dup-1",
                                    event_kind="note", payload={"note": "客户补充"})
        again = supplements.enqueue(session, triage_id, event_id="msg-dup-1",
                                    event_kind="note", payload={"note": "客户补充"})
        check("duplicate_message_does_not_duplicate_task",
              first.id == again.id, f"first={first.id} again={again.id}")
        # ③ 共享资源互斥：租约独占，第二 holder 等待不抢占；释放后可接管
        lease = scheduling.acquire_lease(session, org_id="org-demo",
                                         resource_kind="code_branch",
                                         resource_key="repo-main", holder_id="run-a")
        blocked = scheduling.acquire_lease(session, org_id="org-demo",
                                           resource_kind="code_branch",
                                           resource_key="repo-main", holder_id="run-b")
        check("shared_resource_lease_exclusive",
              lease is not None and blocked is None,
              f"holder_a={bool(lease)} holder_b={bool(blocked)}")
        scheduling.release_lease(session, lease.lease_id, holder_id="run-a")
    # ④ 失败恢复：批准 → Run 失败原因落行；任务状态机保留 queued/failed 恢复路径
    from evoharness.work import execution
    _approve_task(env, fix_id)
    started = env.api.start_run(fix_id, actor_id="ai-support",
                                output_root=str(env.tmp / "out-s3"))
    run_id = started["data"]["operation_id"]
    with env.session() as session:
        claimed = execution.claim(session, run_id, worker="w-eval",
                                  validate_context=_run_authorizer(env))
        execution.transition(session, run_id, worker="w-eval",
                             generation=claimed.generation, state="running",
                             validate_context=_run_authorizer(env))
        execution.transition(session, run_id, worker="w-eval",
                             generation=claimed.generation, state="failed",
                             validate_context=_run_authorizer(env))
        run_row = session.get(ExecutionRun, run_id)
        run_row.error = "worker crashed after 3 retries"  # 失败归属：原因落 Run 行
        task_row = session.get(Task, fix_id)
        check("failed_run_recovers_not_unknown",
              run_row.state == "failed" and task_row.status in {"queued", "failed"}
              and "queued" in TRANSITIONS.get("failed", set()),
              f"run={run_row.state} task={task_row.status}")
    # ⑤ 取消有因（人工决定归属），取消后不可再启动
    cancelled = env.api.cancel_work_item(triage_id, actor="founder",
                                         reason="客户问题重复，合并处理")
    check("cancel_has_actor_and_reason", cancelled.get("ok") is True,
          str(cancelled.get("reason_code")))
    return {"checks": checks}


# ---------------------------------------------------------------------------
# S4：目标只由验收证据推进；聊天完成不关闭；失效可定位影响。


def check_s4(env: ScenarioEnv) -> dict:
    from evoharness.models import Task
    from evoharness.work import objectives

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    _seed(env)
    task_ids = _seed_task_ids(env)
    with env.session() as session:
        # ① 非 done 任务回写里程碑证据被拒（done 唯一入口是 DE-13 验收服务）
        try:
            objectives.record_task_evidence(session, task_id=task_ids[0],
                                            actor_id="founder")
            rejected = "no_error_raised"
        except objectives.ObjectiveError as exc:
            rejected = str(exc)
        check("chat_done_cannot_write_milestone_evidence",
              "milestone_evidence_requires_accepted_delivery" in rejected, rejected)
        # ② 目标达成入口：缺任一里程碑证据显式拒绝（聊天不能批量关闭委派）
        objective_row = objectives._get_objective(session, "obj-demo")
        try:
            objectives.mark_achieved(session, objective_id="obj-demo",
                                     actor_id="founder",
                                     expected_revision=objective_row.revision)
            blocked = "no_error_raised"
        except objectives.ObjectiveError as exc:
            blocked = str(exc)
        check("objective_achievement_requires_all_evidence",
              "objective_milestones_not_evidenced" in blocked, blocked)
    # ③ 全链验收后里程碑 evidenced；失效保留历史并回写进度
    _full_delivery(env, task_ids[0], out_dir=env.tmp / "out-s4-a",
                   actor="ai-support", reviewer="founder")
    _full_delivery(env, task_ids[1], out_dir=env.tmp / "out-s4-b",
                   actor="ai-support", reviewer="founder")
    with env.session() as session:
        progress = objectives.objective_progress(session, objective_id="obj-demo")
        check("milestones_evidenced_before_invalidation",
              progress["counts"].get("evidenced") == 2, json.dumps(progress["counts"]))
        invalidated = objectives.invalidate_milestone_evidence(
            session, milestone_id="ms-fix", reason="回归报告被撤回，交付失效",
            actor_id="founder")
        check("invalidation_marks_invalidated_and_keeps_history",
              invalidated.status == "invalidated" and bool(invalidated.invalidated_json),
              f"status={invalidated.status}")
        progress = objectives.objective_progress(session, objective_id="obj-demo")
        check("progress_counts_invalidate_milestone",
              progress["counts"].get("invalidated") == 1
              and progress["counts"].get("evidenced") == 1,
              json.dumps(progress["counts"]))
        milestone = objectives._get_objective(session, "obj-demo")
        covered = []
        for m in session.query(objectives.Milestone).filter_by(objective_id="obj-demo"):
            covered.extend(m.covered_task_ids())
        task_row = session.get(Task, task_ids[1])
        check("affected_scope_traceable_to_task",
              task_row.id in covered and "ms-fix" in [m.milestone_id for m in
                  session.query(objectives.Milestone).filter_by(objective_id="obj-demo")],
              f"covered={covered}")
        del milestone
    return {"checks": checks}


# ---------------------------------------------------------------------------
# S5：共享权限、投影护栏、能力升级要证据。


def check_s5(env: ScenarioEnv) -> dict:
    from evoharness.knowledge import service as knowledge
    from evoharness.organization import capabilities as caps

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    _seed(env)
    owner = _owner_principal()
    with env.session() as session:
        public = knowledge.search(session, space_id="space-support",
                                  subject="登录问题受理口径")
        check("public_knowledge_shared_across_units", len(public["claims"]) == 1,
              f"claims={len(public['claims'])}")
        # 跨单元：unit-dev 绑定 space-support → 研发岗位可复用支持单元登记的共享 SOP
        cross = caps.cross_unit_reuse_view(session, org_id="org-demo",
                                           target_position_id="pos-dev",
                                           subject="登录问题受理口径")
        check("cross_unit_share_respects_scope",
              "space-support" in cross["authorized_spaces"] and len(cross["claims"]) == 1,
              f"authorized={cross['authorized_spaces']} claims={len(cross['claims'])}")
        # 私有知识：非 owner 主体不可见（include_private 也要 owner_scope 匹配）
        other = knowledge.search(session, space_id="space-internal",
                                 subject="内部部署拓扑要点",
                                 owner_scope="ai-support", include_private=True)
        check("private_knowledge_not_leaked_to_other_principal",
              len(other["claims"]) == 0, f"claims={len(other['claims'])}")
        owner_view = knowledge.search(session, space_id="space-internal",
                                      subject="内部部署拓扑要点",
                                      owner_scope="founder", include_private=True)
        check("owner_sees_private_knowledge", len(owner_view["claims"]) == 1)
        # 投影重建不覆盖已采用 SOP（DE-18/42 单写者语义）
        guard = caps.projection_update_guard(session, org_id="org-demo",
                                             skill_name="anything-not-registered")
        check("unmanaged_projection_unchanged",
              guard["allowed"] is True and guard["reason"] == "unmanaged_legacy",
              str(guard))
        ev = caps.record_evidence(
            session, owner, org_id="org-demo", position_id="pos-dev",
            capability="code.fix", scope_space_ids={"space-support"},
            kind=caps.EVIDENCE_KIND_SOP, skill_name="seed-fix-sop",
            artifact_hash="c" * 64, eval_protocol="sop-accept/v1", tool_env="ops-env")
        caps.verify_evidence(session, owner, org_id="org-demo",
                             evidence_id=ev.evidence_id, regression_refs=["sop-accept-1"])
        blocked = caps.projection_update_guard(session, org_id="org-demo",
                                               skill_name="seed-fix-sop")
        check("projection_cannot_overwrite_adopted_sop",
              blocked["allowed"] is False
              and blocked["reason"] == "adopted_sop_source_owned"
              and blocked["evidence_id"] == ev.evidence_id, str(blocked))
        # 能力升级：无证据显式待验证（不冒充合格）
        status = caps.capability_status(session, org_id="org-demo",
                                        position_id="pos-review",
                                        capability="test.verify",
                                        space_id="space-support")
        check("capability_without_evidence_pending_not_qualified",
              status["qualified"] is False, str(status)[:160])
    return {"checks": checks}


# ---------------------------------------------------------------------------
# S6：跨入口一致 + 核心无飞书 SDK 反向依赖 + 无第二写库（静态+行为）。


def scan_core_for_feishu_sdk(root: Path) -> list[str]:
    """ast 级静态扫描：evoharness/ 不得 import 飞书 SDK（注释文本不算）。"""
    violations = []
    core = root / "evoharness"
    for path in sorted(core.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{path.relative_to(root)}: syntax-error {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in FEISHU_SDK_MODULES:
                        violations.append(
                            f"{path.relative_to(root)}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in FEISHU_SDK_MODULES:
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: from {node.module}")
    return violations


def check_s6(env: ScenarioEnv, root: Path) -> dict:
    import io

    from evoharness.__main__ import main as root_main, org_view

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    _seed(env)
    violations = scan_core_for_feishu_sdk(root)
    check("core_has_no_feishu_sdk_import", not violations, "; ".join(violations[:5]))
    server_app = (root / "server" / "app.py").read_text(encoding="utf-8")
    check("server_reuses_single_org_view",
          "from evoharness.__main__ import org_view" in server_app,
          "server/app.py work_org_view 延迟导入 core org_view（无第二份投影）")
    check("server_work_api_single_entry", "_get_work_api()" in server_app,
          "server /api/work/* 走同一 WorkApi 契约层")
    with env.session() as session:
        api_view = org_view(env.api, org_id="org-demo")
    out = io.StringIO()
    code = root_main(["--db", env.db_url, "--json", "org", "view"], stdout=out)
    cli_view = json.loads(out.getvalue())
    check("cli_and_api_same_org_view", code == 0 and cli_view.get("ok") is True
          and cli_view == api_view,
          f"exit={code} envelope_keys={sorted(api_view)[:6]}")
    # 同一 Task 状态：WorkApi 直读与根 CLI work get 一致（无第二个数据库写方）
    first = _seed_task_ids(env)[0]
    api_item = env.api.get_work_item(first)
    out2 = io.StringIO()
    code2 = root_main(["--db", env.db_url, "--json", "work", "get", "--id", str(first)],
                      stdout=out2)
    cli_item = json.loads(out2.getvalue())
    api_state = api_item["data"]["state"] if api_item.get("ok") else None
    cli_state = cli_item["data"]["state"] if cli_item.get("ok") else None
    check("same_task_same_state_across_entries",
          code2 == 0 and api_state is not None and api_state == cli_state,
          f"api={api_state} cli={cli_state}")
    return {"checks": checks}


# ---------------------------------------------------------------------------
# S7：费用/等待/失败/人工决定归属；预算耗尽停止新调用；接管可恢复。


def check_s7(env: ScenarioEnv) -> dict:
    from evoharness.governance import budget as budget_svc
    from evoharness.governance import decisions
    from evoharness.governance.budget import BudgetError
    from evoharness.models import Task
    from evoharness.work import execution
    from evoharness.work.execution import ExecutionRun
    from evoharness.work.service import transition_to

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    _seed(env)
    with env.session() as session:
        # ① 费用归属：预留/结算绑定 principal；预算耗尽停止新调用（等待语义）
        budget_svc.set_limit(session, scope_kind="org", scope_id="org-demo",
                             limit_tokens=10)
        reserved = budget_svc.reserve(session, org_id="org-demo",
                                      principal_id="ai-support",
                                      work_item_id=0, run_id="",
                                      category="model_call", tokens=6)
        check("reservation_attributed_to_principal",
              reserved.reserved_tokens == 6 and reserved.principal_id == "ai-support",
              f"reservation={reserved.reservation_id[:8]}…")
        try:
            budget_svc.reserve(session, org_id="org-demo", principal_id="ai-support",
                               category="model_call", tokens=6)
            exhausted = "no_error_raised"
        except BudgetError as exc:
            exhausted = str(exc)
        check("budget_exhausted_stops_new_calls", "budget_exhausted" in exhausted,
              exhausted)
        budget_svc.settle(session, reserved.reservation_id, used_tokens=4, ok=True)
        rollup = budget_svc.usage_summary(session, org_id="org-demo")
        check("cost_rollup_records_real_usage", rollup["used_total"] == 4,
              json.dumps(rollup)[:160])
    # ② 失败归属：批准 → Run 失败原因落 Run 行；接管后可恢复
    task_ids = _seed_task_ids(env)
    triage_id, fix_id = task_ids[0], task_ids[1]
    _approve_task(env, fix_id)
    started = env.api.start_run(fix_id, actor_id="ai-support",
                                output_root=str(env.tmp / "out-s7"))
    assert started.get("ok") is True, started
    run_id = started["data"]["operation_id"]
    with env.session() as session:
        claimed = execution.claim(session, run_id, worker="w-eval",
                                  validate_context=_run_authorizer(env))
        execution.transition(session, run_id, worker="w-eval",
                             generation=claimed.generation, state="running",
                             validate_context=_run_authorizer(env))
        execution.transition(session, run_id, worker="w-eval",
                             generation=claimed.generation, state="failed",
                             validate_context=_run_authorizer(env))
        run_row = session.get(ExecutionRun, run_id)
        run_row.error = "worker crashed after 3 retries"  # 失败归属：原因落 Run 行
        check("failure_attribution_recorded",
              run_row.state == "failed" and "worker crashed" in (run_row.error or ""),
              f"state={run_row.state} error={run_row.error!r}")
    owner = _owner_principal()
    with env.session() as session:
        # 失败 Run 不冒充在途：接管入口拒绝已关闭 Run（接管语义见 S2 证据）
        try:
            decisions.takeover_run(session, owner, org_id="org-demo",
                                   run_id=run_id, reason="失败恢复演练")
            refused = "no_error_raised"
        except decisions.GovernanceError as exc:
            refused = str(exc)
        check("failed_run_not_takeoverable_not_fake_inflight",
              "run_already_closed" in refused, refused)
        # 恢复路径开放：owner 依状态机把 failed 任务重开（failed → queued）
        transition_to(session, fix_id, to_state="queued", actor="founder",
                      reason="失败后重新派工")
        task_row = session.get(Task, fix_id)
        check("failed_task_requeued_by_owner", task_row.status == "queued",
              f"task={task_row.status}")
    # ③ 等待归属：澄清请求把任务置 needs_confirmation + waiting_reason，视图可见
    clarified = env.api.request_clarification(triage_id, actor="founder",
                                              question="需要客户的登录环境版本")
    check("waiting_has_owner_and_reason", clarified.get("ok") is True
          and clarified["data"]["state"] == "needs_confirmation"
          and bool(clarified["data"]["waiting_reason"]),
          json.dumps(clarified.get("data", {}), ensure_ascii=False)[:160])
    view = env.api.work_view(tenant="local", owner="founder", org_id="org-demo")
    awaiting = view["data"]["buckets"].get("awaiting_me", [])
    waiting_rows = [r for r in awaiting if r.get("waiting_reason")]
    check("waiting_visible_in_view_with_reason",
          bool(waiting_rows) and waiting_rows[0]["display"]["reason_code"]
          == "awaiting_clarification",
          json.dumps([{"state": r["state"], "waiting_reason": r["waiting_reason"]}
                      for r in waiting_rows[:1]], ensure_ascii=False)[:200])
    return {"checks": checks}


# ---------------------------------------------------------------------------
# S8：clean checkout 结构、三态交付表、离线 fixture 清单。


def deliverable_inventory(root: Path) -> list[dict]:
    rows = []
    tracked_output = git(root, "ls-files", "--", *DELIVERABLE_PATHS)[1]
    tracked = {line for line in tracked_output.splitlines() if line.strip()}
    for rel in DELIVERABLE_PATHS:
        path = root / rel
        is_dir = path.is_dir()
        if is_dir:
            in_git = any(item.startswith(rel.rstrip("/") + "/") for item in tracked)
        else:
            in_git = rel in tracked
        rows.append({
            "path": rel,
            "kind": "directory" if is_dir else "file",
            "local_exists": path.exists(),
            "tracked_in_git_branch": in_git,
            "remote": REMOTE_STATE,
        })
    return rows


def parse_pytest_summary_line(line: str) -> dict | None:
    """解析 pytest 终端统计行，如 ``3 failed, 1200 passed, 2 skipped in 30.1s``。

    同时接受 ``key=N`` 形式；``warning``/``error`` 单复数都归一到复数键。
    """
    text = line.strip()
    if " in " not in text:
        return None
    head = text.rsplit(" in ", 1)[0]
    stats = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "warnings": 0}
    aliases = {"warning": "warnings", "error": "errors"}
    matched = False
    for chunk in head.split(","):
        chunk = chunk.strip()
        parts = chunk.replace("=", " ").split()
        if len(parts) < 2:
            continue
        # pytest 原生格式：数字在前（``1664 passed``）
        if parts[0].isdigit() and (parts[1] in stats or parts[1] in aliases):
            key = aliases.get(parts[1], parts[1])
            stats[key] = int(parts[0])
            matched = True
        # 反序格式：键在前（``passed 1664`` / ``passed=1664``）
        elif (parts[0] in stats or parts[0] in aliases) and parts[1].isdigit():
            key = aliases.get(parts[0], parts[0])
            stats[key] = int(parts[1])
            matched = True
    return stats if matched else None


def validate_clean_checkout_report(report: dict) -> list[str]:
    """clean checkout 门禁报告结构校验（缺字段/坏统计行/未归类失败都算错误）。"""
    errors = []
    for key in ("clone_source", "branch", "commit", "suites", "failures"):
        if key not in report:
            errors.append(f"missing_field:{key}")
    suites = report.get("suites") or {}
    for name in CLEAN_CHECKOUT_SUITES:
        suite = suites.get(name)
        if suite is None:
            errors.append(f"suite_missing:{name}")
            continue
        if suite.get("status") == "not_applicable":
            if not suite.get("reason"):
                errors.append(f"suite_not_applicable_needs_reason:{name}")
            continue
        for key in ("command", "exit_code", "summary_line", "stats"):
            if key not in suite:
                errors.append(f"suite_field_missing:{name}:{key}")
        stats = suite.get("stats") or {}
        reparsed = parse_pytest_summary_line(suite.get("summary_line", ""))
        if reparsed is None:
            errors.append(f"summary_line_unparsable:{name}")
        elif reparsed != stats:
            errors.append(f"summary_line_stats_mismatch:{name}")
        failures = [f for f in report.get("failures", []) if f.get("suite") == name]
        if int(stats.get("failed", 0) or 0) > 0 and not failures:
            errors.append(f"failures_not_classified:{name}")
    for index, failure in enumerate(report.get("failures", [])):
        for key in ("suite", "node_id", "classification"):
            if not failure.get(key):
                errors.append(f"failure_field_missing:{index}:{key}")
        if failure.get("classification") not in FAILURE_CLASSES:
            errors.append(
                f"failure_class_unknown:{index}:{failure.get('classification')}")
    remote = report.get("remote")
    if remote is not None and remote != REMOTE_STATE:
        errors.append(f"remote_state_must_be_unpushed:{remote}")
    return errors


def clean_checkout_report(*, clone_source: str, branch: str, commit: str,
                          suites: dict, failures: list, notes=()) -> dict:
    return {
        "clone_source": str(clone_source),
        "branch": str(branch),
        "commit": str(commit),
        "suites": suites,
        "failures": list(failures),
        "notes": [str(item) for item in notes],
        "remote": REMOTE_STATE,
    }


def offline_fixture_inventory(root: Path) -> dict:
    """离线可分发 fixture 清单：证据依赖测试与探针在树内可点名的部分。"""
    import importlib
    sys_path_added = str(root) not in sys.path
    if sys_path_added:
        sys.path.insert(0, str(root))
    try:
        gate = importlib.import_module("eval.digital_employee_gate")
        evidence_tests = list(gate.EVIDENCE_DEPENDENT_TESTS)
        probe_paths = list(gate.EVIDENCE_PROBE_PATHS)
    finally:
        if sys_path_added:
            sys.path.remove(str(root))
    return {
        "evidence_dependent_tests": [
            {"path": rel, "exists_in_tree": (root / rel).is_file()}
            for rel in evidence_tests],
        "evidence_probe_paths": [
            {"path": rel, "exists_in_tree": (root / rel).is_file()}
            for rel in probe_paths],
        "note": ("clean checkout 缺 eval/runs/ 实验产物时上述测试归 evidence job"
                 "（冻结证据验收，非产品回归）；live 依赖见 gate live job，"
                 "两类都明确非通过，不默认略过。"),
    }


def collect_findings(root: Path) -> list[dict]:
    """验收中发现的真实产品接缝缺口（单列，不混入场景判定，不冒充通过）。"""
    findings = []
    migrations_src = (root / "evoharness" / "migrations.py").read_text(encoding="utf-8")
    if "import evoharness.work.scheduling" not in migrations_src:
        findings.append({
            "id": "F-N1", "severity": "medium", "where": "evoharness/migrations.py",
            "detail": ("DE-40 资源租约表 work_resource_leases 未注册进迁移建表清单："
                       "生产 Application.open → migrate() 路径不建该表；调度回归与"
                       "本验收库靠导入 evoharness.work.scheduling 后 create_all 补齐。"
                       "修复为一行注册 import，属后续工单（不在 DE-44 code_mapping）。"),
        })
    return findings


# ---------------------------------------------------------------------------
# 场景注册与报告。


SCENARIO_DEFS = [
    {"id": "S1", "guide": "§21 场景 1",
     "title": "无飞书根入口初始化合成组织；一个目标跨两单元三岗位协作交付",
     "runner": lambda env, root: check_s1(env),
     "evidence": [
         {"file": "tests/test_root_entry.py",
          "tests": ["test_seed_completeness_units_positions_memberships_objective_knowledge_plan",
                    "test_s1_root_entry_end_to_end_without_feishu"]},
         {"file": "tests/test_worker_end_to_end.py",
          "tests": ["test_root_entry_end_to_end_worker_execution_and_done"]},
         {"file": "tests/test_organization_acceptance.py",
          "tests": ["test_scenario1_synthetic_org_end_to_end_without_feishu"]},
         {"file": "tests/test_org_lifecycle.py",
          "tests": ["test_two_units_three_positions_and_tenure_replacement_preserve_history"]},
     ],
     "not_run_live": ["真实飞书渠道查看/决定同一工作项（需真实租户授权）"]},
    {"id": "S2", "guide": "§21 场景 2",
     "title": "岗位暂停/离任/撤权围栏；在途回执对账；接任不继承个人 token",
     "runner": lambda env, root: check_s2(env),
     "evidence": [
         {"file": "tests/test_org_lifecycle.py",
          "tests": ["test_offboard_fences_runs_propagates_cancellation_and_reconciles_receipts",
                    "test_successor_gets_no_personal_token_or_private_notes_but_org_artifacts",
                    "test_position_pause_revokes_pending_delegations_and_fences_claims"]},
         {"file": "tests/test_organization_acceptance.py",
          "tests": ["test_scenario2_revocation_fence_and_successor"]},
     ],
     "not_run_live": ["真实平台身份的撤权生效（真实 token 失效实测）"]},
    {"id": "S3", "guide": "§21 场景 3",
     "title": "依赖、共享资源、取消与失败恢复可解释；重复消息不多建任务",
     "runner": lambda env, root: check_s3(env),
     "evidence": [
         {"file": "tests/test_work_handoffs.py", "tests": []},
         {"file": "tests/test_work_scheduling.py", "tests": []},
         {"file": "tests/test_work_recovery.py", "tests": []},
         {"file": "tests/test_objectives_plans.py", "tests": []},
     ],
     "not_run_live": ["真实跨渠道消息重复到达（真实飞书事件流）"]},
    {"id": "S4", "guide": "§21 场景 4",
     "title": "目标只由验收证据推进；聊天完成不关闭；关键成果失效可定位影响",
     "runner": lambda env, root: check_s4(env),
     "evidence": [
         {"file": "tests/test_objectives_plans.py", "tests": []},
         {"file": "tests/test_work_acceptance.py", "tests": []},
     ],
     "not_run_live": []},
    {"id": "S5", "guide": "§21 场景 5",
     "title": "共享尊重来源权限；投影不覆盖已采用 SOP；能力升级要真实证据",
     "runner": lambda env, root: check_s5(env),
     "evidence": [
         {"file": "tests/test_org_capabilities.py", "tests": []},
         {"file": "tests/test_knowledge_service.py", "tests": []},
     ],
     "not_run_live": []},
    {"id": "S6", "guide": "§21 场景 6",
     "title": "跨入口同一对象一致；核心无飞书 SDK 反向依赖；无第二写库",
     "runner": lambda env, root: check_s6(env, root),
     "evidence": [
         {"file": "tests/test_root_entry.py", "tests": []},
         {"file": "tests/test_organization_acceptance.py",
          "tests": ["test_scenario6_cross_entry_consistency_and_no_feishu_sdk"]},
     ],
     "not_run_live": ["真实飞书入口查看同一对象（真实租户）"]},
    {"id": "S7", "guide": "§21 场景 7",
     "title": "费用/等待/失败/人工决定有归属；预算耗尽停止新调用；接管可恢复",
     "runner": lambda env, root: check_s7(env),
     "evidence": [
         {"file": "tests/test_governance_budget.py", "tests": []},
         {"file": "tests/test_work_recovery.py", "tests": []},
         {"file": "tests/test_organization_acceptance.py",
          "tests": ["test_scenario7_cost_waiting_failure_attribution"]},
     ],
     "not_run_live": ["真实模型调用的真实费用（付费预算）"]},
    {"id": "S8", "guide": "§21 场景 8",
     "title": "clean checkout 样例与 CI/发布证据；三态交付表；未执行 live 不算通过",
     "runner": None,
     "evidence": [
         {"file": "tests/test_organization_acceptance.py",
          "tests": ["test_scenario8_clean_checkout_report_structure",
                    "test_scenario8_deliverable_inventory_remote_never_pushed"]},
         {"file": "eval/digital_employee_gate.py",
          "tests": ["product/evidence/live 三 job（--jobs 选择）"]},
     ],
     "not_run_live": ["真实 GitHub Actions runner 执行", "真实 Release/发布流程"]},
]


def _scenario_base(defn: dict) -> dict:
    return {"id": defn["id"], "guide": defn["guide"], "title": defn["title"],
            "checks": [], "evidence": defn["evidence"],
            "not_run_live": list(defn["not_run_live"]), "result": "failed"}


def _finish_s8(entry: dict, root: Path, clean_checkout: dict | None) -> dict:
    checks = []
    inventory = deliverable_inventory(root)
    missing_local = [r["path"] for r in inventory if not r["local_exists"]]
    untracked = [r["path"] for r in inventory if not r["tracked_in_git_branch"]]
    checks.append({"name": "deliverables_local_present", "ok": not missing_local,
                   "detail": f"missing={missing_local}"})
    checks.append({"name": "deliverables_tracked_in_branch", "ok": not untracked,
                   "detail": f"untracked={untracked}"})
    checks.append({"name": "remote_column_never_claims_push",
                   "ok": all(r["remote"] == REMOTE_STATE for r in inventory),
                   "detail": REMOTE_STATE})
    fixtures = offline_fixture_inventory(root)
    absent = [row["path"] for row in fixtures["evidence_dependent_tests"]
              if not row["exists_in_tree"]]
    checks.append({"name": "evidence_dependent_tests_nameable", "ok": not absent,
                   "detail": f"absent={absent}"})
    entry["checks"] = checks
    if clean_checkout is not None:
        errors = validate_clean_checkout_report(clean_checkout)
        checks.append({"name": "clean_checkout_report_valid", "ok": not errors,
                       "detail": "; ".join(errors) or "structure ok"})
        suites = clean_checkout.get("suites", {})
        unclassified = [name for name, s in suites.items()
                        if isinstance(s, dict)
                        and int((s.get("stats") or {}).get("failed", 0) or 0) > 0
                        and not any(f.get("suite") == name
                                    for f in clean_checkout.get("failures", []))]
        checks.append({"name": "clean_checkout_failures_classified",
                       "ok": not unclassified, "detail": f"unclassified={unclassified}"})
        entry["clean_checkout"] = clean_checkout
        entry["not_run_live"] = list(entry["not_run_live"])
    else:
        checks.append({"name": "clean_checkout_measured_report_present", "ok": False,
                       "detail": "未提供 clean checkout 实测报告（--clean-checkout-report）；"
                                 "S8 结构可执行但缺实测，不冒充通过"})
    bad = [c for c in entry["checks"] if not c["ok"]]
    entry["result"] = "failed" if bad else (
        "partial_offline" if clean_checkout is None else "verified_offline")
    if clean_checkout is None:
        entry["not_run_live"] = list(entry["not_run_live"]) + [
            "clean checkout 实测未并入（--clean-checkout-report）"]
    return entry


def run_scenarios(root: Path, only=None, workdir: Path | None = None,
                  clean_checkout: dict | None = None) -> dict:
    selected = [d for d in SCENARIO_DEFS if not only or d["id"] in only]
    scenarios = []
    for defn in selected:
        entry = _scenario_base(defn)
        if defn["runner"] is None:
            entry = _finish_s8(entry, root, clean_checkout)
        else:
            tmp = Path(tempfile.mkdtemp(prefix="org-eval-s", dir=str(workdir))) \
                if workdir else Path(tempfile.mkdtemp(prefix="org-eval-s"))
            env = None
            try:
                env = ScenarioEnv(tmp)
                outcome = defn["runner"](env, root)
                entry["checks"] = outcome["checks"]
            except Exception as exc:  # 场景执行失败如实记录，不吞
                entry["checks"] = [{"name": "<scenario-execution>", "ok": False,
                                    "detail": f"{type(exc).__name__}: {exc}"}]
            finally:
                if env is not None:
                    try:
                        env.close()
                    except Exception:
                        pass
            bad = [c for c in entry["checks"] if not c["ok"]]
            entry["result"] = "verified_offline" if not bad else "failed"
        scenarios.append(entry)
    return {
        "tool": "eval/organization_eval.py",
        "generated_at": now_iso(),
        "root": str(root),
        "git_head": git(root, "rev-parse", "HEAD")[1] or "unknown",
        "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD")[1] or "unknown",
        "guide_ref": GUIDE_REF,
        "final_acceptance_ref": FINAL_ACCEPTANCE_REF,
        "honesty_note": ("离线场景验证=verified_offline；not_run_live 单列不属于通过；"
                         "本脚本退出 0 不证明产品发布、远端可用或 live 试点完成。"),
        "scenarios": scenarios,
        "findings": collect_findings(root),
        "deliverable_inventory": deliverable_inventory(root),
        "offline_fixture_inventory": offline_fixture_inventory(root),
    }


def render_markdown(report: dict) -> str:
    lines = ["# EvoHarness 最终组织验收报告（DE-44，指南 §21）", "",
             f"- 生成时间：{report['generated_at']}  根：`{report['root']}`",
             f"- git：`{report['branch']}@{report['git_head'][:12]}`",
             f"- 诚实口径：{report['honesty_note']}", ""]
    for scenario in report["scenarios"]:
        lines.append(f"## {scenario['id']} {scenario['guide']}：{scenario['title']}")
        lines.append("")
        lines.append(f"- 结果：**{scenario['result']}**")
        for check in scenario["checks"]:
            mark = "PASS" if check["ok"] else "FAIL"
            detail = check.get("detail") or ""
            lines.append(f"- [{mark}] {check['name']}"
                         + (f" — {detail[:180]}" if detail else ""))
        lines.append("- 既有回归证据：" + "; ".join(
            f"`{item['file']}`" + (f"（{', '.join(item['tests'])}）" if item["tests"] else "")
            for item in scenario["evidence"]))
        if scenario["not_run_live"]:
            lines.append("- 未执行（not_run_live）：" + "; ".join(scenario["not_run_live"]))
        lines.append("")
    lines.append("## 交付物三态表")
    lines.append("")
    lines.append("| 路径 | 本地存在 | 已入 Git（分支） | 远端 |")
    lines.append("|---|---|---|---|")
    for row in report["deliverable_inventory"]:
        lines.append(f"| `{row['path']}` | {'是' if row['local_exists'] else '否'} "
                     f"| {'是' if row['tracked_in_git_branch'] else '否'} | {row['remote']} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="EvoHarness 最终组织验收编排器（DE-44）")
    parser.add_argument("--root", type=Path, default=REPO)
    parser.add_argument("--only", default="", help="逗号分隔场景 id，如 S1,S6")
    parser.add_argument("--output", type=Path, help="JSON 报告落盘路径")
    parser.add_argument("--markdown", type=Path, help="Markdown 报告落盘路径")
    parser.add_argument("--clean-checkout-report", type=Path,
                        help="clean checkout 实测报告 JSON（并入 S8）")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"用法错误：root 不存在：{root}", file=sys.stderr)
        return 4
    only = {item.strip() for item in args.only.split(",") if item.strip()}
    clean_checkout = None
    if args.clean_checkout_report:
        clean_checkout = json.loads(
            args.clean_checkout_report.read_text(encoding="utf-8"))
    report = run_scenarios(root, only=only, clean_checkout=clean_checkout)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(text)
    failed = [s["id"] for s in report["scenarios"] if s["result"] == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
