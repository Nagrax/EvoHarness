"""EvoHarness 根入口（DE-43）：``python -m evoharness``。

统一组织工作台的产品入口，提供最小命令集：

- ``init``   初始化合成组织种子（两个业务单元、三类岗位、人/AI 任职、目标、
             岗位权限、共享与私有知识、工作依赖与验收；无个人 token/私聊/
             公司真实资料），不启动飞书也能全流程（验收场景 1）；
- ``serve``  启动根 Web 工作台（server.app），业务库来自 --db 或
             EVOHARNESS_DATABASE_URL；
- ``org``    组织视图查询（单元/岗位/任职/目标/工作引用与 reason/next_action）；
- ``work``   工作项操作（创建/批准/派工/验收/接管/证据），与 API、Web 工作台
             走同一 evoharness.api 契约层，不生成第二份状态（验收场景 2）；
- ``worker`` Run 执行侧（BP-02）：``--once`` 消费一个 queued Run 后退出；
             ``--daemon`` 常驻轮询（间隔可配、优雅停机）；``--health`` 队列
             年龄/租约/最近错误。profile=fake 离线回归 / live 委派原生
             agents.Agent 主线（复用现有双协议模型配置）；
- ``doctor`` 健康与配置检查（分项事实，根入口流程不依赖飞书）。

本文件只实现真实存在的命令；页面/命令不宣称 schema 之外的能力（指南 §11）。
模块导入无副作用：server.app 延迟导入 org_view 时可安全复用同一实现。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

from sqlalchemy import select

from evoharness.api import WorkApi
from evoharness.cli import _build_api, _readable
from evoharness.governance import authorization as gov
from evoharness.governance.decisions import AuthenticatedPrincipal, takeover_run
from evoharness.ids import sha256_json, stable_id
from evoharness.knowledge import service as knowledge_service
from evoharness.models import Task, TaskEvent, TaskStatus
from evoharness.organization import service as org
from evoharness.organization.models import (Assignment, BusinessSpace, EmployeeProfile,
                                            Membership, Organization,
                                            OrganizationUnit, Position,
                                            UnitSpaceBinding)
from evoharness.organization.service import OrganizationError
from evoharness.work import objectives, plans
from evoharness.work.objectives import Milestone, OrgObjective

# 合成种子默认标识：全部为演示数据，不含任何凭据、私聊或公司真实资料
SEED_ORG_ID = "org-demo"
SEED_TENANT = "local"
SEED_APP = "root"
SEED_OWNER = "founder"


# ── 合成组织种子 ─────────────────────────────────────────────────────────────

def _owner_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        principal_id=SEED_OWNER, tenant_id=SEED_TENANT, app_id=SEED_APP,
        authenticated=True,
        authorities=frozenset({"organization.bootstrap", "organization.manage",
                               "work.execute", "knowledge.read",
                               "objective.manage", "plan.confirm"}))


def _seed_task(session, *, task_key: str, title: str, action: str, goal: str,
               acceptance: list, org_id: str, space_id: str,
               assigned_position: str) -> Task:
    """种子任务：与 WorkApi.create_work_item 同一 Task 形态（单表、revision=1）。"""
    task = Task(
        task_key=stable_id("seed", SEED_TENANT, task_key), tenant=SEED_TENANT,
        owner=SEED_OWNER, chat_id="", thread_id="", title=title, action=action,
        assignee_id="", assignee_name="", due_text="",
        status=TaskStatus.OPEN, needs_confirmation=False,
        workflow_kind="delegated_delivery", org_id=org_id, space_id=space_id,
        assigned_position=assigned_position, revision=1, goal=goal,
        acceptance_json=json.dumps(acceptance, ensure_ascii=False),
        source_message_ids=json.dumps([], ensure_ascii=False))
    session.add(task)
    session.flush()
    session.add(TaskEvent(task_id=task.id, event_type="created", actor=SEED_OWNER,
                          detail="via root entry seed"))
    session.flush()
    return task


def seed_organization(session, *, org_id: str = SEED_ORG_ID) -> dict:
    """合成组织种子（指南 §20 的开源样例要求，全部离线合成数据）。

    - 两个业务单元 + 两个业务空间；三类岗位（支持/研发/独立验收）；
    - 人与 AI 任职并存，岗位权限来自显式授权交集；
    - 一个组织目标 + 两个里程碑（引用原 Task，不造第二任务库）；
    - 一份已确认工作计划表达工作依赖（受理 → 修复）；
    - 共享（public）与私有（founder 个人范围）知识各一条；
    - 幂等：组织已存在时返回 created=False，不重复写入。
    """
    if session.get(Organization, org_id) is not None:
        return _seed_summary(session, org_id=org_id, created=False)

    actor = _owner_principal()
    org.bootstrap_organization(session, actor, org_id=org_id,
                               title="EvoHarness 合成组织",
                               membership_id="m-founder", budget_limit=1000)
    org.create_space(session, actor, org_id=org_id, space_id="space-support",
                     title="客户支持空间")
    org.create_space(session, actor, org_id=org_id, space_id="space-internal",
                     title="内部研发空间")
    org.create_unit(session, actor, org_id=org_id, unit_id="unit-support",
                    title="客户支持单元")
    org.create_unit(session, actor, org_id=org_id, unit_id="unit-dev",
                    title="产品研发单元")
    org.bind_space(session, actor, org_id=org_id, unit_id="unit-support",
                   space_id="space-support")
    org.bind_space(session, actor, org_id=org_id, unit_id="unit-dev",
                   space_id="space-support")
    org.bind_space(session, actor, org_id=org_id, unit_id="unit-dev",
                   space_id="space-internal")
    org.create_position(session, actor, org_id=org_id, unit_id="unit-support",
                        position_id="pos-support", title="支持工程师",
                        mission="受理客户问题，形成带证据的工单",
                        actions={"work.execute", "knowledge.read"}, budget_limit=200,
                        capability_refs=["support.triage"])
    org.create_position(session, actor, org_id=org_id, unit_id="unit-dev",
                        position_id="pos-dev", title="研发工程师",
                        mission="修复缺陷并补回归",
                        actions={"work.execute", "knowledge.read"}, budget_limit=200,
                        capability_refs=["code.fix"])
    org.create_position(session, actor, org_id=org_id, unit_id="unit-dev",
                        position_id="pos-review", title="独立验收",
                        mission="独立核对交付证据",
                        actions={"work.execute", "knowledge.read"}, budget_limit=200,
                        capability_refs=["test.verify"])
    org.add_member(session, actor, org_id=org_id, membership_id="m-ai-support",
                   principal_id="ai-support", tenant_id=SEED_TENANT, app_id=SEED_APP,
                   grants={"work.execute", "knowledge.read"}, principal_kind="ai_service")
    org.add_member(session, actor, org_id=org_id, membership_id="m-dev-li",
                   principal_id="dev-li", tenant_id=SEED_TENANT, app_id=SEED_APP,
                   grants={"work.execute", "knowledge.read"}, principal_kind="human")
    org.add_member(session, actor, org_id=org_id, membership_id="m-ai-review",
                   principal_id="ai-review", tenant_id=SEED_TENANT, app_id=SEED_APP,
                   grants={"work.execute", "knowledge.read"}, principal_kind="ai_service")
    org.create_employee(session, actor, org_id=org_id, employee_id="emp-support",
                        membership_id="m-ai-support", display_name="数字员工·支持")
    org.create_employee(session, actor, org_id=org_id, employee_id="emp-dev",
                        membership_id="m-dev-li", display_name="研发·李工")
    org.create_employee(session, actor, org_id=org_id, employee_id="emp-review",
                        membership_id="m-ai-review", display_name="数字员工·验收")
    org.assign(session, actor, org_id=org_id, assignment_id="asg-support",
               position_id="pos-support", employee_id="emp-support",
               space_ids={"space-support"},
               actions={"work.execute", "knowledge.read"}, budget_limit=100)
    org.assign(session, actor, org_id=org_id, assignment_id="asg-dev",
               position_id="pos-dev", employee_id="emp-dev",
               space_ids={"space-support", "space-internal"},
               actions={"work.execute", "knowledge.read"}, budget_limit=100)
    org.assign(session, actor, org_id=org_id, assignment_id="asg-review",
               position_id="pos-review", employee_id="emp-review",
               space_ids={"space-support", "space-internal"},
               actions={"work.execute", "knowledge.read"}, budget_limit=100)

    task_a = _seed_task(
        session, task_key="seed-support-triage", title="受理客户登录超时问题并定位",
        action="triage", goal="记录工单并给出定位结论",
        acceptance=[{"kind": "qa", "criteria": "工单含复现步骤与定位结论",
                     "capability": "support.triage",
                     "required_artifacts": ["answer", "evidence"]}],
        org_id=org_id, space_id="space-support", assigned_position="pos-support")
    task_b = _seed_task(
        session, task_key="seed-dev-fix", title="修复登录超时缺陷并补回归",
        action="fix", goal="交付补丁且回归全部通过",
        acceptance=[{"kind": "code", "criteria": "回归全部通过",
                     "capability": "code.fix",
                     "required_artifacts": ["diff", "test_report"]}],
        org_id=org_id, space_id="space-support", assigned_position="pos-dev")

    objective_id = "obj-demo"
    objective_acceptance = [{"criteria": "问题受理、缺陷修复与独立验收全链路留有证据"}]
    creation = objectives.creation_payload(
        title="客户问题闭环交付", acceptance=objective_acceptance,
        owner=SEED_OWNER, unit_id="", parent_id="")
    gov.approve(session, SEED_OWNER, org_id=org_id, target_type="objective",
                target_id=objective_id, target_revision=1,
                payload_hash=sha256_json(creation),
                actions={"objective.manage"})
    objectives.create_objective(session, org_id=org_id, objective_id=objective_id,
                                title="客户问题闭环交付",
                                acceptance=objective_acceptance,
                                owner=SEED_OWNER, actor_id=SEED_OWNER)
    objectives.add_milestone(session, objective_id=objective_id, milestone_id="ms-triage",
                             title="问题受理与定位",
                             deliverable_condition="工单记录含复现步骤与定位结论",
                             actor_id=SEED_OWNER, covered_by_task_ids=[task_a.id])
    objectives.add_milestone(session, objective_id=objective_id, milestone_id="ms-fix",
                             title="缺陷修复与回归",
                             deliverable_condition="补丁与回归报告齐备",
                             actor_id=SEED_OWNER, covered_by_task_ids=[task_b.id])

    proposal = plans.propose_plan(
        session, org_id=org_id, objective_id=objective_id,
        title="受理到修复接力计划",
        nodes=[{"task_id": task_a.id, "unit_id": "unit-support",
                "position_id": "pos-support", "required_capability": "support.triage",
                "depends_on": [], "milestone_id": "ms-triage"},
               {"task_id": task_b.id, "unit_id": "unit-dev",
                "position_id": "pos-dev", "required_capability": "code.fix",
                "depends_on": [task_a.id], "milestone_id": "ms-fix"}],
        actor_id=SEED_OWNER, evidence_refs=["seed://org-demo/plan"],
        expected_effect="受理完成后研发接手修复")
    confirmation = plans.confirmation_payload(session, proposal_id=proposal.proposal_id)
    gov.approve(session, SEED_OWNER, org_id=org_id, target_type="work_plan",
                target_id=proposal.proposal_id,
                target_revision=confirmation["based_on_revision"],
                payload_hash=sha256_json(confirmation), actions={"plan.confirm"})
    plan = plans.confirm_plan(session, proposal_id=proposal.proposal_id,
                              actor_id=SEED_OWNER)

    knowledge_service.record_claim(
        session, org_id=org_id, space_id="space-support", owner_scope="public",
        subject="登录问题受理口径", value="受理后先记录工单与复现步骤，再派研发修复",
        source_refs=["seed://sop/support-triage@v1"])
    knowledge_service.record_claim(
        session, org_id=org_id, space_id="space-internal", owner_scope=SEED_OWNER,
        subject="内部部署拓扑要点", value="测试环境仅内网可达，回归需在 staging 执行",
        source_refs=["seed://notes/founder@v1"])

    session.flush()
    return _seed_summary(session, org_id=org_id, created=True, plan_id=plan.plan_id)


def _seed_summary(session, *, org_id: str, created: bool, plan_id: str = "") -> dict:
    unit_ids = sorted(u.unit_id for u in session.scalars(select(OrganizationUnit).where(
        OrganizationUnit.org_id == org_id)))
    position_ids = sorted(p.position_id for p in session.scalars(select(Position).where(
        Position.org_id == org_id)))
    memberships = list(session.scalars(select(Membership).where(
        Membership.org_id == org_id)))
    assignment_count = len(list(session.scalars(select(Assignment).where(
        Assignment.org_id == org_id))))
    objective_rows = list(session.scalars(select(OrgObjective).where(
        OrgObjective.org_id == org_id)))
    milestone_ids = sorted(m.milestone_id for m in session.scalars(select(Milestone)))
    task_ids = sorted(t.id for t in session.scalars(select(Task).where(
        Task.org_id == org_id)))
    return {"created": created, "org_id": org_id, "units": unit_ids,
            "positions": position_ids,
            "membership_kinds": sorted({m.principal_kind for m in memberships}),
            "memberships": len(memberships), "assignments": assignment_count,
            "objective_ids": [o.objective_id for o in objective_rows],
            "milestones": milestone_ids, "task_ids": task_ids,
            "plan_id": plan_id}


# ── 组织视图：org/position/goal/task/artifact 引用与 reason/next_action ──────

def org_view(api, *, org_id: str, principal=None) -> dict:
    """组织视图只读投影（server /api/work/org 与根 CLI org view 共用本实现）。

    任务展示态复用 WorkApi._view_row（唯一展示态映射），不生成第二份状态；
    产物引用复用 WorkApi._artifact_refs，Agent 可按 path+sha256 精确读取。
    principal 非空（hosted HTTP 受信主体）时按注册组织成员资格校验（N01）；
    CLI/本地模式 principal=None 保持既有行为。
    """
    try:
        if principal is not None:
            denial = api._principal_org_denial(principal, org_id)
            if denial is not None:
                return denial
        with api._session() as session:
            org_row = session.get(Organization, str(org_id))
            if org_row is None:
                return WorkApi._fail("organization_not_found")
            units = [{"unit_id": u.unit_id, "title": u.title, "parent_id": u.parent_id,
                      "enabled": u.enabled, "revision": u.revision}
                     for u in session.scalars(select(OrganizationUnit).where(
                         OrganizationUnit.org_id == org_id).order_by(OrganizationUnit.unit_id))]
            spaces = [{"space_id": s.space_id, "title": s.title, "enabled": s.enabled}
                      for s in session.scalars(select(BusinessSpace).where(
                          BusinessSpace.org_id == org_id).order_by(BusinessSpace.space_id))]
            bindings = [{"unit_id": b.unit_id, "space_id": b.space_id}
                        for b in session.scalars(select(UnitSpaceBinding).where(
                            UnitSpaceBinding.org_id == org_id))]
            positions = [{"position_id": p.position_id, "unit_id": p.unit_id,
                          "title": p.title, "mission": p.mission, "status": p.status,
                          "capability_refs": json.loads(p.capability_refs_json or "[]"),
                          "action_ceiling": json.loads(p.action_ceiling_json or "[]"),
                          "budget_limit": p.budget_limit}
                         for p in session.scalars(select(Position).where(
                             Position.org_id == org_id).order_by(Position.position_id))]
            memberships = [{"membership_id": m.membership_id,
                            "principal_id": m.principal_id,
                            "principal_kind": m.principal_kind,
                            "roles": json.loads(m.roles_json or "[]"),
                            "grants": json.loads(m.grants_json or "[]"),
                            "status": m.status}
                           for m in session.scalars(select(Membership).where(
                               Membership.org_id == org_id).order_by(
                               Membership.membership_id))]
            employees = [{"employee_id": e.employee_id, "membership_id": e.membership_id,
                          "display_name": e.display_name, "status": e.status}
                         for e in session.scalars(select(EmployeeProfile).where(
                             EmployeeProfile.org_id == org_id).order_by(EmployeeProfile.employee_id))]
            assignments = [{"assignment_id": a.assignment_id,
                            "position_id": a.position_id,
                            "employee_id": a.employee_id,
                            "space_ids": json.loads(a.space_ids_json or "[]"),
                            "actions": json.loads(a.actions_json or "[]"),
                            "status": a.status}
                           for a in session.scalars(select(Assignment).where(
                               Assignment.org_id == org_id).order_by(
                               Assignment.assignment_id))]
            objective_rows = list(session.scalars(select(OrgObjective).where(
                OrgObjective.org_id == org_id).order_by(OrgObjective.objective_id)))
            objective_list = [objectives.objective_progress(session, objective_id=o.objective_id)
                              for o in objective_rows]
            task_rows = []
            refs: list[str] = []
            for task in session.scalars(select(Task).where(
                    Task.org_id == org_id).order_by(Task.id)):
                row = api._view_row(session, task)
                row["assigned_position"] = task.assigned_position
                row["artifacts"] = api._artifact_refs(session, task.id)
                task_rows.append(row)
                refs.append(str(task.id))
                refs.extend(artifact["path"] for artifact in row["artifacts"])
            data = {"org_id": org_id, "title": org_row.title,
                    "enabled": org_row.enabled, "revision": org_row.revision,
                    "units": units, "spaces": spaces, "unit_space_bindings": bindings,
                    "positions": positions, "memberships": memberships, "employees": employees,
                    "assignments": assignments, "objectives": objective_list,
                    "tasks": task_rows,
                    "counts": {"units": len(units), "positions": len(positions),
                               "memberships": len(memberships),
                               "assignments": len(assignments),
                               "objectives": len(objective_list),
                               "tasks": len(task_rows)}}
            return WorkApi._ok(data, sorted(set(refs)))
    except OrganizationError as exc:
        return WorkApi._convert(exc)
    except ValueError as exc:
        return WorkApi._convert(exc)


# ── doctor：健康与配置检查（分项事实，不依赖飞书） ───────────────────────────

def doctor_view(api, *, org_id: str = "") -> dict:
    try:
        with api._session() as session:
            org_ids = sorted(o.org_id for o in session.scalars(select(Organization)))
        health = api.view_health(org_id)
        checks = [{"name": "database",
                   "status": "ok" if health["ok"] else "failed",
                   "detail": health["data"].get("database", {})},
                  {"name": "organization",
                   "status": "seeded" if org_ids else "not_seeded",
                   "detail": {"org_ids": org_ids}}]
        if health["ok"]:
            data = health["data"]
            unknown_receipts = data.get("receipts", {}).get("delivery_unknown", 0)
            checks.append({"name": "runs", "status": "ok", "detail": data.get("runs", {})})
            checks.append({"name": "receipts",
                           "status": "attention" if unknown_receipts else "ok",
                           "detail": data.get("receipts", {})})
            checks.append({"name": "waiting", "status": "ok",
                           "detail": data.get("waiting", {})})
            checks.append({"name": "budget",
                           "status": "unconfigured"
                           if not data.get("budget", {}).get("limits_configured")
                           else "ok",
                           "detail": data.get("budget", {})})
            checks.append({"name": "knowledge", "status": "ok",
                           "detail": data.get("knowledge", {})})
        return WorkApi._ok({"checks": checks, "at": health["data"].get("at", ""),
                            "channel_feishu_required_for_root_flow": False},
                           org_ids)
    except ValueError as exc:
        return WorkApi._convert(exc)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _readable_root(result: dict) -> str:
    """根入口特有形状的可读输出：只报事实与计数，不加标语。"""
    data = result.get("data") or {}
    if "checks" in data:
        rows = [f"{c['name']}: {c['status']}" for c in data["checks"]]
        return "健康检查\n  " + "\n  ".join(rows)
    if "units" in data and "positions" in data and "tasks" in data:
        counts = data.get("counts", {})
        lines = [f"组织 {data.get('org_id')}（{data.get('title')}）"
                 f"单元 {counts.get('units')} · 岗位 {counts.get('positions')} · "
                 f"任职 {counts.get('memberships')} · 目标 {counts.get('objectives')} · "
                 f"工作项 {counts.get('tasks')}"]
        for row in data.get("tasks", []):
            display = row.get("display", {})
            lines.append(f"  #{row['id']} {row['title']} — {display.get('label', row['state'])}"
                         + (f"（原因: {row['waiting_reason']}）" if row.get("waiting_reason") else ""))
        for objective in data.get("objectives", []):
            counts = objective["counts"]
            lines.append(f"  目标 {objective['objective_id']}（{objective['status']}）— "
                         f"负责人 {objective.get('owner') or '—'} · "
                         f"里程碑证据 {counts['evidenced']}/{counts['total']}")
        return "\n".join(lines)
    if "created" in data and "org_id" in data:
        if data["created"]:
            return (f"组织 {data['org_id']} 已初始化：单元 {len(data.get('units', []))} · "
                    f"岗位 {len(data.get('positions', []))} · "
                    f"任职 {data.get('memberships')} · 工作项 {len(data.get('task_ids', []))}")
        return f"组织 {data['org_id']} 已存在，未重复初始化"
    if "run_id" in data and "receipt_id" in data:
        return (f"Run {data['run_id']} 已接管（generation {data.get('generation')}），"
                f"在途回执对账单 {data['receipt_id']} 待核对")
    return ""


def _emit(result: dict, *, as_json: bool, out) -> int:
    if as_json:
        print(json.dumps(result, ensure_ascii=False), file=out)
    else:
        readable = _readable_root(result)
        print(readable if readable else _readable(result), file=out)
    return 0 if result.get("ok") else 2


def _resolve_db(args) -> str:
    """统一配置解析（DE-02）：--db > EVOHARNESS_DATABASE_URL；相对路径按 CWD。"""
    from evoharness.config import resolve_config
    return resolve_config(db=getattr(args, "db", "") or "").database_url


def _principal_from_args(args) -> AuthenticatedPrincipal:
    tenant = (getattr(args, "tenant", "") or "").strip() or SEED_TENANT
    return AuthenticatedPrincipal(principal_id=str(args.actor),
                                  tenant_id=tenant, app_id=SEED_APP,
                                  authenticated=True, authorities=frozenset())


def _dispatch_work(api: WorkApi, args) -> dict:
    cmd = args.work_cmd
    if cmd == "view":
        return api.work_view(tenant=args.tenant, owner=args.owner, org_id=args.org)
    if cmd == "create":
        return api.create_work_item(
            tenant=args.tenant, owner=args.owner, title=args.title,
            goal=args.goal, acceptance=args.acceptance_json,
            assignee_id=args.assignee_id, workflow_kind=args.kind,
            org_id=args.org, space_id=args.space)
    if cmd == "get":
        return api.get_work_item(args.id)
    if cmd == "approve":
        payload = api.run_request_payload(args.id)
        if not payload["ok"]:
            return payload
        return api.submit_approval(
            org_id=args.org, actor_id=args.actor, target_type="work_item",
            target_id=str(args.id), target_revision=payload["data"]["target_revision"],
            payload=payload["data"]["payload"], actions=["work.execute"])
    if cmd == "start-run":
        return api.start_run(args.id, actor_id=args.actor, mode=args.mode,
                             output_root=args.output_root or None)
    if cmd == "request-acceptance":
        return api.request_acceptance(args.id, run_id=args.run_id,
                                      requester=args.requester)
    if cmd == "review":
        return api.review_acceptance(args.id, run_id=args.run_id,
                                     reviewer=args.reviewer, verdict=args.verdict,
                                     notes=args.notes)
    if cmd == "takeover":
        try:
            with api._session() as session:
                result = takeover_run(session, _principal_from_args(args),
                                      org_id=args.org, run_id=args.run_id,
                                      reason=args.reason)
                return WorkApi._ok(result, [result["run_id"], result["receipt_id"]])
        except ValueError as exc:
            return WorkApi._convert(exc)
    if cmd == "evidence":
        return api.view_evidence(args.id)
    return WorkApi._fail("unknown_command")


def _common_options() -> argparse.ArgumentParser:
    """全局选项（--db/--artifacts/--json）：主命令与各级子命令都可接受，
    便于 Agent 把全局旗标放在末尾。SUPPRESS 避免子命令默认值覆盖主命令已解析值。"""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="业务库 URL；缺省读 EVOHARNESS_DATABASE_URL")
    common.add_argument("--artifacts", default=argparse.SUPPRESS,
                        help="长输出产物目录")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出统一信封 JSON")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="python -m evoharness", parents=[common],
        description="EvoHarness 根入口：统一组织工作台（init/serve/org/work/doctor）")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", parents=[common],
                            help="初始化合成组织种子（离线，无需飞书）")
    init_p.add_argument("--org-id", default=SEED_ORG_ID)

    serve_p = sub.add_parser("serve", parents=[common],
                             help="启动根 Web 工作台（server.app）")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8800)

    org_p = sub.add_parser("org", parents=[common], help="组织查询")
    org_sub = org_p.add_subparsers(dest="org_cmd", required=True)
    org_view_p = org_sub.add_parser("view", parents=[common],
                                    help="组织视图（单元/岗位/任职/目标/工作）")
    org_view_p.add_argument("--org", default=SEED_ORG_ID)

    work_p = sub.add_parser("work", parents=[common],
                            help="工作项操作（与 API/工作台同一契约层）")
    work_sub = work_p.add_subparsers(dest="work_cmd", required=True)
    w_view = work_sub.add_parser("view", parents=[common], help="工作视图")
    w_view.add_argument("--tenant", default=SEED_TENANT)
    w_view.add_argument("--owner", required=True)
    w_view.add_argument("--org", default="")
    w_create = work_sub.add_parser("create", parents=[common], help="创建委派工作项")
    w_create.add_argument("--tenant", default=SEED_TENANT)
    w_create.add_argument("--owner", default=SEED_OWNER)
    w_create.add_argument("--title", required=True)
    w_create.add_argument("--goal", default="")
    w_create.add_argument("--acceptance-json", default="[]")
    w_create.add_argument("--assignee-id", default="")
    w_create.add_argument("--kind", default="delegated_delivery",
                          choices=["delegated_delivery", "legacy_todo"])
    w_create.add_argument("--org", default=SEED_ORG_ID)
    w_create.add_argument("--space", default="")
    w_get = work_sub.add_parser("get", parents=[common], help="查看工作项详情")
    w_get.add_argument("--id", required=True)
    w_approve = work_sub.add_parser("approve", parents=[common],
                                    help="批准并绑定目标版本与载荷")
    w_approve.add_argument("--id", required=True, type=int)
    w_approve.add_argument("--actor", required=True)
    w_approve.add_argument("--org", default=SEED_ORG_ID)
    w_start = work_sub.add_parser("start-run", parents=[common], help="派工启动 Run")
    w_start.add_argument("--id", required=True, type=int)
    w_start.add_argument("--actor", required=True)
    w_start.add_argument("--mode", default="live", choices=["live", "dryrun"])
    w_start.add_argument("--output-root", default="")
    w_accept = work_sub.add_parser("request-acceptance", parents=[common],
                                   help="执行结束请求验收")
    w_accept.add_argument("--id", required=True, type=int)
    w_accept.add_argument("--run-id", required=True)
    w_accept.add_argument("--requester", required=True)
    w_review = work_sub.add_parser("review", parents=[common], help="独立验收 pass/fail")
    w_review.add_argument("--id", required=True, type=int)
    w_review.add_argument("--run-id", required=True)
    w_review.add_argument("--reviewer", required=True)
    w_review.add_argument("--verdict", required=True, choices=["pass", "fail"])
    w_review.add_argument("--notes", default="")
    w_takeover = work_sub.add_parser("takeover", parents=[common],
                                     help="接管在途 Run（围栏+回执对账）")
    w_takeover.add_argument("--run-id", required=True)
    w_takeover.add_argument("--actor", required=True)
    w_takeover.add_argument("--reason", required=True)
    w_takeover.add_argument("--org", default=SEED_ORG_ID)
    w_takeover.add_argument("--tenant", default=SEED_TENANT)
    w_ev = work_sub.add_parser("evidence", parents=[common],
                               help="按 ID 查看工作项证据")
    w_ev.add_argument("--id", required=True, type=int)

    doctor_p = sub.add_parser("doctor", parents=[common],
                              help="健康与配置检查（分项事实）")
    doctor_p.add_argument("--org", default="")

    worker_p = sub.add_parser("worker", parents=[common],
                              help="Run 执行侧：--once 消费一个 queued Run / "
                                   "--daemon 常驻 / --health 队列年龄与租约")
    worker_mode = worker_p.add_mutually_exclusive_group(required=True)
    worker_mode.add_argument("--once", action="store_true",
                             help="消费一个 queued Run 后退出")
    worker_mode.add_argument("--daemon", action="store_true",
                             help="常驻轮询（优雅停机）")
    worker_mode.add_argument("--health", action="store_true",
                             help="队列年龄/租约/最近错误（只读）")
    worker_p.add_argument("--profile", default="live", choices=["fake", "live"],
                          help="fake=离线回归剧本；live=委派原生 agents.Agent"
                               "（复用现有双协议模型配置）")
    worker_p.add_argument("--poll-interval", type=float, default=2.0,
                          help="daemon 轮询间隔（秒）")
    worker_p.add_argument("--worker-id", default="")
    worker_p.add_argument("--check-argv", default="",
                          help="live 检查器 argv（JSON 数组；默认 python -m pytest -q）")
    return parser


def main(argv=None, *, stdout=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        api = _build_api(args)
    except ValueError as exc:
        return _emit(WorkApi._envelope(False, None, str(exc) or "database_not_configured"),
                     as_json=as_json, out=out)
    try:
        if args.command == "init":
            with api._session() as session:
                result = WorkApi._ok(
                    seed_organization(session, org_id=args.org_id),
                    [args.org_id])
        elif args.command == "org":
            result = org_view(api, org_id=args.org)
        elif args.command == "work":
            result = _dispatch_work(api, args)
        elif args.command == "doctor":
            result = doctor_view(api, org_id=args.org)
        elif args.command == "worker":
            return _worker(api, args, out=out)
        elif args.command == "serve":
            return _serve(api, args, out=out)
        else:
            result = WorkApi._fail("unknown_command")
    except ValueError as exc:
        result = WorkApi._convert(exc)
    return _emit(result, as_json=as_json, out=out)


def _worker(api: WorkApi, args, *, out) -> int:
    """根入口 worker 命令（BP-02）：--once / --daemon / --health。

    daemon 需要保持 Application 打开（轮询消费），不走统一 _emit 返回路径。
    """
    from evoharness.work import worker_cli

    application = api._app   # 同一应用服务实例（单库单写者，不另开装配）
    if args.health:
        return _emit(worker_cli.worker_health(application),
                     as_json=as_json_flag(args), out=out)
    check_argv = None
    raw_argv = str(getattr(args, "check_argv", "") or "").strip()
    if raw_argv:
        try:
            parsed = json.loads(raw_argv)
        except ValueError:
            return _emit(WorkApi._fail("check_argv_invalid_json"),
                         as_json=as_json_flag(args), out=out)
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            return _emit(WorkApi._fail("check_argv_must_be_string_array"),
                         as_json=as_json_flag(args), out=out)
        check_argv = parsed
    if args.once:
        return _emit(worker_cli.worker_once(
            application, api, profile=args.profile, worker_id=args.worker_id,
            check_argv=check_argv), as_json=as_json_flag(args), out=out)
    return worker_cli.worker_daemon(
        application, api, profile=args.profile, worker_id=args.worker_id,
        poll_interval_seconds=args.poll_interval, check_argv=check_argv)


def as_json_flag(args) -> bool:
    return bool(getattr(args, "json", False))


def _serve(api: WorkApi, args, *, out) -> int:
    """根 Web 服务组装：应用服务已通过 _build_api 打开并迁移，再挂 server.app。"""
    import uvicorn

    from server import app as web
    web_app = web.app
    print(json.dumps({"ok": True, "data": {"serving": "server.app:app",
                                           "host": args.host, "port": args.port},
                      "reason_code": "ok", "retryable": False,
                      "next_action": "", "evidence_refs": []}, ensure_ascii=False),
          file=out)
    previous_api = web._WORK_API
    previous_policy = api._acceptance_policy
    web._WORK_API = api
    try:
        if not web._is_local_host(args.host):
            api._acceptance_policy = "strict"
        uvicorn.run(web_app, host=args.host, port=args.port)
    finally:
        web._WORK_API = previous_api
        api._acceptance_policy = previous_policy
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
