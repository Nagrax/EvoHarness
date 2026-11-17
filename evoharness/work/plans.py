"""跨单元工作计划：WorkPlan 只引用原 Task ID 与依赖 DAG（DE-39）。

- 不造第二任务库：计划节点只引用既有 Task（tasks.id），节点记录确认时的
  task revision；执行仍走同一个 Run 服务，本模块不派工、不执行。
- 计划拆解可由模型建议（propose_plan / raise_proposal 只记录提案），
  确认目标与资源的权限来自既有决定（DE-12 governance.authorization：
  confirm_plan 必须通过 decision 校验才落地）。
- 循环依赖 / 无责任任务 / 验收歧义 / 岗位无能力证据 / 资源空间未绑定
  在提案与确认两处都明确阻止，或按 on_violation="clarify" 转澄清。
- 关键交付撤回（任务重开/取消）按依赖 DAG 标记受影响里程碑与下游任务
  （mark_delivery_withdrawn），保留历史完成快照；无影响的交付原样保留，
  不重复执行。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from evoharness.clock import utcnow
from evoharness.ids import sha256_json
from evoharness.models import Base, Task, TaskEvent
from evoharness.organization.models import Position, UnitSpaceBinding
from evoharness.governance import authorization as gov
from evoharness.work import objectives
from evoharness.work.objectives import Milestone, OrgObjective

PLAN_CONFIRM_ACTION = "plan.confirm"

PROPOSAL_KINDS = frozenset({"propose_plan", "clarify_plan", "goal_conflict",
                            "data_gap", "temporary_collab"})
NODE_STATUSES = frozenset({"confirmed", "affected", "stale"})


class PlanError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _now() -> datetime:
    return utcnow()


# ── 实体 ─────────────────────────────────────────────────────────────────────


class WorkPlan(Base):
    """工作计划：引用目标与原 Task 依赖 DAG；不是可独立改状态的任务库。"""
    __tablename__ = "work_plans"
    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    objective_id: Mapped[str] = mapped_column(
        ForeignKey("org_objectives.objective_id"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    based_on_objective_revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="confirmed", index=True)
    confirmed_by: Mapped[str] = mapped_column(String(128), default="")
    decision_id: Mapped[str] = mapped_column(String(64), default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class PlanNode(Base):
    """计划节点：原 Task 的计划投影（责任岗位/依赖/覆盖里程碑）。"""
    __tablename__ = "work_plan_nodes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("work_plans.plan_id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    unit_id: Mapped[str] = mapped_column(String(64), default="")
    position_id: Mapped[str] = mapped_column(String(64), default="")
    required_capability: Mapped[str] = mapped_column(String(128), default="")
    depends_on_json: Mapped[str] = mapped_column(Text, default="[]")
    milestone_id: Mapped[str] = mapped_column(String(64), default="")
    task_revision_recorded: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="confirmed", index=True)
    affected_reason: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    def depends_on(self) -> list[int]:
        try:
            return [int(v) for v in json.loads(self.depends_on_json or "[]")]
        except ValueError:
            return []


class WorkPlanProposal(Base):
    """协调提案（契约 schema 的 CoordinationProposal 落地形态）：
    proposal_id / work_ref / based_on_revision / evidence_refs / payload / expected_effect。
    提案本身不执行、不改状态；确认必须经既有决定。"""
    __tablename__ = "work_plan_proposals"
    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    objective_id: Mapped[str] = mapped_column(
        ForeignKey("org_objectives.objective_id"), index=True)
    work_ref: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(32), default="propose_plan")
    title: Mapped[str] = mapped_column(String(255), default="")
    based_on_revision: Mapped[int] = mapped_column(Integer, default=1)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    expected_effect: Mapped[str] = mapped_column(Text, default="")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    violations_json: Mapped[str] = mapped_column(Text, default="[]")
    created_by: Mapped[str] = mapped_column(String(128), default="")
    decided_by: Mapped[str] = mapped_column(String(128), default="")
    decided_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    def payload(self) -> dict:
        try:
            return json.loads(self.payload_json or "{}")
        except ValueError:
            return {}

    def nodes(self) -> list[dict]:
        return list(self.payload().get("nodes") or [])

    def evidence_refs(self) -> list[str]:
        try:
            return [str(v) for v in json.loads(self.evidence_refs_json or "[]")]
        except ValueError:
            return []

    def violations(self) -> list[str]:
        try:
            return [str(v) for v in json.loads(self.violations_json or "[]")]
        except ValueError:
            return []


# ── 结构校验（提案与确认共用同一套规则）─────────────────────────────────────


def _task(session: Session, task_id, *, org_id: str) -> Task:
    task = session.get(Task, int(task_id))
    if task is None:
        raise PlanError("plan_task_missing:%s" % task_id)
    if org_id and task.org_id != org_id:
        raise PlanError("plan_task_not_in_organization:%s" % task_id)
    return task


def _acceptance_clear(task: Task) -> bool:
    try:
        acceptance = json.loads(task.acceptance_json or "[]")
    except ValueError:
        return False
    return bool(task.goal.strip()) and bool(acceptance)


def _dependency_cycle(nodes: list[dict]) -> list[int]:
    """task_id 为节点的依赖图；返回处在环上的任务（稳定序），无环返回空。"""
    graph: dict[int, list[int]] = {}
    for node in nodes:
        entry = graph.setdefault(int(node["task_id"]), [])
        entry.extend(int(dep) for dep in (node.get("depends_on") or []))
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {task: WHITE for task in graph}
    cycles: set[int] = set()
    for start in sorted(graph):
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        path = [start]
        stack = [(start, iter(graph.get(start, [])))]
        while stack:
            current, edges = stack[-1]
            advanced = False
            for nxt in edges:
                state = color.get(nxt, BLACK)
                if state == GRAY:
                    # 回边命中灰节点：path 上从它首次出现到当前都是环成员
                    cycles.update(path[path.index(nxt):])
                    return sorted(cycles)
                if state == WHITE and nxt in graph:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append((nxt, iter(graph[nxt])))
                    advanced = True
                    break
            if not advanced:
                color[current] = BLACK
                path.pop()
                stack.pop()
    return sorted(cycles)


def _validate_nodes(session: Session, *, org_id: str, objective: OrgObjective,
                    nodes: list[dict]) -> list[str]:
    """返回违规清单（code[:detail]）；空清单 = 结构合法可确认。"""
    violations: list[str] = []
    if not nodes:
        return ["plan_nodes_required"]
    seen_tasks: set[int] = set()
    for node in nodes:
        try:
            task_id = int(node.get("task_id"))
        except (TypeError, ValueError):
            violations.append("plan_node_task_id_invalid")
            continue
        if task_id in seen_tasks:
            violations.append("plan_task_duplicated:%s" % task_id)
        seen_tasks.add(task_id)
        try:
            task = _task(session, task_id, org_id=org_id)
        except PlanError as exc:
            violations.append(str(exc))
            continue
        # 无责任任务：没有责任岗位或任务本身没有 owner
        position_id = str(node.get("position_id") or "").strip()
        if not position_id or not task.owner.strip():
            violations.append("plan_node_unowned:%s" % task_id)
        # 验收歧义：目标/验收为空的任务不能进计划
        if not _acceptance_clear(task):
            violations.append("plan_acceptance_ambiguous:%s" % task_id)
        if position_id:
            position = session.get(Position, position_id)
            if position is None or position.org_id != org_id or position.status != "active":
                violations.append("position_inactive:%s" % position_id)
            else:
                unit_id = str(node.get("unit_id") or "").strip()
                if unit_id and unit_id != position.unit_id:
                    violations.append("plan_unit_mismatch:%s" % position_id)
                capability = str(node.get("required_capability") or "").strip()
                declared = set(json.loads(position.capability_refs_json or "[]"))
                # 角色名 ≠ 工作能力：岗位没声明该能力证据就不能接目标任务
                if capability and capability not in declared:
                    violations.append("position_capability_missing:%s" % position_id)
                if task.space_id:
                    binding = session.scalar(select(UnitSpaceBinding.id).where(
                        UnitSpaceBinding.unit_id == position.unit_id,
                        UnitSpaceBinding.space_id == task.space_id))
                    if binding is None:
                        violations.append("plan_space_not_bound:%s" % task_id)
        milestone_id = str(node.get("milestone_id") or "").strip()
        if milestone_id:
            milestone = session.get(Milestone, milestone_id)
            if milestone is None:
                violations.append("plan_milestone_missing:%s" % milestone_id)
            elif milestone.objective_id != objective.objective_id:
                violations.append("plan_milestone_objective_mismatch:%s" % milestone_id)
            elif not milestone.deliverable_condition.strip():
                violations.append("plan_milestone_ambiguous:%s" % milestone_id)
    for dep in (dep for node in nodes for dep in (node.get("depends_on") or [])):
        if int(dep) not in {int(n.get("task_id")) for n in nodes}:
            violations.append("plan_dependency_unknown:%s" % dep)
    cycles = _dependency_cycle(nodes)
    if cycles:
        violations.append("plan_dependency_cycle:" +
                          ",".join(str(v) for v in cycles))
    return sorted(set(violations))


def _normalize_nodes(nodes: list[dict]) -> list[dict]:
    normalized = []
    for node in nodes:
        normalized.append({
            "task_id": int(node["task_id"]),
            "unit_id": str(node.get("unit_id") or ""),
            "position_id": str(node.get("position_id") or ""),
            "required_capability": str(node.get("required_capability") or ""),
            "depends_on": sorted({int(dep) for dep in (node.get("depends_on") or [])}),
            "milestone_id": str(node.get("milestone_id") or ""),
        })
    return sorted(normalized, key=lambda item: item["task_id"])


def _get_objective_for_org(session: Session, *, org_id: str,
                           objective_id: str) -> OrgObjective:
    objective = session.get(OrgObjective, objective_id)
    if objective is None or objective.org_id != org_id:
        raise PlanError("objective_not_in_organization")
    return objective


def _get_proposal(session: Session, proposal_id: str) -> WorkPlanProposal:
    row = session.get(WorkPlanProposal, proposal_id)
    if row is None:
        raise PlanError("proposal_not_found")
    return row


# ── 提案 ─────────────────────────────────────────────────────────────────────


def propose_plan(session: Session, *, org_id: str, objective_id: str, title: str,
                 nodes: list[dict], actor_id: str, evidence_refs=(),
                 expected_effect: str = "", work_ref: str = "",
                 proposal_id: str | None = None,
                 on_violation: str = "reject") -> WorkPlanProposal:
    """模型/协调者建议拆解：只记录提案，不派工不执行。

    结构违规（循环依赖/无责任任务/验收歧义/能力缺失等）默认明确阻止；
    on_violation="clarify" 时转澄清：提案落库为 needs_clarification 并记录
    违规清单，等待责任人补齐后重新提案。
    """
    if on_violation not in {"reject", "clarify"}:
        raise PlanError("invalid_on_violation")
    if not evidence_refs:
        raise PlanError("proposal_evidence_required")
    objective = _get_objective_for_org(session, org_id=org_id, objective_id=objective_id)
    violations = _validate_nodes(session, org_id=org_id, objective=objective,
                                 nodes=_normalize_nodes(nodes))
    if violations and on_violation == "reject":
        raise PlanError(violations[0])
    if proposal_id is None:
        proposal_id = "pp-%s-%s" % (objective_id, sha256_json(
            {"nodes": _normalize_nodes(nodes), "title": title})[:16])
    existing = session.get(WorkPlanProposal, proposal_id)
    if existing is not None:
        # 同一提案幂等重放：不产生第二份平行提案
        return existing
    row = WorkPlanProposal(
        proposal_id=proposal_id, org_id=org_id, objective_id=objective_id,
        work_ref=work_ref or "objective:%s" % objective_id,
        kind="propose_plan", title=title.strip(),
        based_on_revision=objective.revision,
        payload_json=_json({"nodes": _normalize_nodes(nodes)}),
        expected_effect=expected_effect.strip(),
        evidence_refs_json=_json([str(v) for v in evidence_refs]),
        status="needs_clarification" if violations else "open",
        violations_json=_json(violations),
        created_by=actor_id)
    session.add(row)
    session.flush()
    return row


def confirmation_payload(session: Session, *, proposal_id: str) -> dict:
    """确认载荷：批准方（负责人）与本服务用同一序列化绑定决定。"""
    proposal = _get_proposal(session, proposal_id)
    return {"kind": proposal.kind, "objective_id": proposal.objective_id,
            "based_on_revision": proposal.based_on_revision,
            "title": proposal.title,
            "nodes": _normalize_nodes(proposal.nodes()),
            "expected_effect": proposal.expected_effect}


def confirm_plan(session: Session, *, proposal_id: str, actor_id: str,
                 plan_id: str | None = None) -> WorkPlan:
    """提案 → 已确认计划：既有决定（DE-12）+ 结构复核 + 目标版本一致。

    - 提案 needs_clarification：不能确认（转澄清后须重新提案）；
    - 目标 revision 已前进：提案过期，重新基于新版本提案；
    - 同目标已确认计划里已有同一任务：拒绝平行计划（不重复创建平行任务）。
    """
    proposal = _get_proposal(session, proposal_id)
    if proposal.status == "applied":
        raise PlanError("proposal_already_applied")
    if proposal.status != "open":
        raise PlanError("proposal_needs_clarification")
    objective = _get_objective_for_org(session, org_id=proposal.org_id,
                                       objective_id=proposal.objective_id)
    if objective.revision != proposal.based_on_revision:
        raise PlanError("proposal_stale_revision")
    violations = _validate_nodes(session, org_id=proposal.org_id, objective=objective,
                                 nodes=_normalize_nodes(proposal.nodes()))
    if violations:
        raise PlanError(violations[0])
    decision = gov.validate(session, target_type="work_plan", target_id=proposal_id,
                            target_revision=proposal.based_on_revision,
                            payload_hash=sha256_json(confirmation_payload(session, proposal_id=proposal_id)),
                            action=PLAN_CONFIRM_ACTION)
    nodes = _normalize_nodes(proposal.nodes())
    if plan_id is None:
        plan_id = "plan-%s" % proposal_id
    if session.get(WorkPlan, plan_id) is not None:
        raise PlanError("plan_exists")
    confirmed_tasks = {int(tid) for tid in session.scalars(
        select(PlanNode.task_id).where(PlanNode.plan_id.in_(
            select(WorkPlan.plan_id).where(WorkPlan.org_id == proposal.org_id,
                                           WorkPlan.objective_id == objective.objective_id,
                                           WorkPlan.status == "confirmed"))))}
    for node in nodes:
        if node["task_id"] in confirmed_tasks:
            raise PlanError("task_already_planned:%s" % node["task_id"])
    plan = WorkPlan(plan_id=plan_id, org_id=proposal.org_id,
                    objective_id=objective.objective_id, title=proposal.title,
                    based_on_objective_revision=objective.revision,
                    status="confirmed", confirmed_by=actor_id,
                    decision_id=decision.decision_id)
    session.add(plan)
    for node in nodes:
        task = _task(session, node["task_id"], org_id=proposal.org_id)
        session.add(PlanNode(
            plan_id=plan_id, task_id=task.id, unit_id=node["unit_id"],
            position_id=node["position_id"],
            required_capability=node["required_capability"],
            depends_on_json=_json(node["depends_on"]),
            milestone_id=node["milestone_id"],
            task_revision_recorded=task.revision, status="confirmed"))
    proposal.status = "applied"
    proposal.decided_by = actor_id
    proposal.decided_at = _now()
    session.flush()
    return plan


def raise_proposal(session: Session, *, org_id: str, objective_id: str, kind: str,
                   payload: dict, actor_id: str, evidence_refs=(),
                   expected_effect: str = "", work_ref: str = "",
                   proposal_id: str | None = None) -> WorkPlanProposal:
    """目标冲突 / 资料缺口 / 临时协作等语义提案：可核验引用 + 期望效果。

    提案不执行任何动作；payload 里的引用在此即时核验，verify_proposal 可重放。
    """
    if kind not in PROPOSAL_KINDS:
        raise PlanError("invalid_proposal_kind")
    if not evidence_refs:
        raise PlanError("proposal_evidence_required")
    objective = _get_objective_for_org(session, org_id=org_id, objective_id=objective_id)
    payload = dict(payload or {})
    if kind == "goal_conflict":
        ids = [str(v) for v in (payload.get("conflicting_objective_ids") or [])]
        if len(set(ids)) < 2:
            raise PlanError("goal_conflict_needs_two_objectives")
        for other_id in set(ids):
            other = session.get(OrgObjective, other_id)
            if other is None or other.org_id != org_id or other.status != "active":
                raise PlanError("conflict_objective_missing:%s" % other_id)
    elif kind == "data_gap":
        gaps = [str(v) for v in (payload.get("missing_sources") or []) if str(v).strip()]
        if not gaps:
            raise PlanError("data_gap_sources_required")
        payload["missing_sources"] = gaps
    elif kind == "temporary_collab":
        capability = str(payload.get("needed_capability") or "").strip()
        if not capability:
            raise PlanError("needed_capability_required")
        suggested = str(payload.get("suggested_position_id") or "").strip()
        if suggested:
            position = session.get(Position, suggested)
            declared = set(json.loads(position.capability_refs_json or "[]")) if position else set()
            if position is None or position.org_id != org_id or position.status != "active" \
                    or capability not in declared:
                # 角色名 ≠ 能力：建议岗位没有该能力证据就不进提案
                raise PlanError("position_capability_missing:%s" % suggested)
    if proposal_id is None:
        proposal_id = "wp-%s-%s" % (kind, sha256_json(
            {"objective_id": objective_id, "kind": kind, "payload": payload})[:16])
    existing = session.get(WorkPlanProposal, proposal_id)
    if existing is not None:
        return existing
    row = WorkPlanProposal(proposal_id=proposal_id, org_id=org_id,
                           objective_id=objective_id,
                           work_ref=work_ref or "objective:%s" % objective_id,
                           kind=kind, based_on_revision=objective.revision,
                           payload_json=_json(payload),
                           expected_effect=expected_effect.strip(),
                           evidence_refs_json=_json([str(v) for v in evidence_refs]),
                           status="open", created_by=actor_id)
    session.add(row)
    session.flush()
    return row


def verify_proposal(session: Session, *, proposal_id: str) -> dict:
    """可核验性重放：目标版本、引用与载荷再次核对，过期/失效如实报告。"""
    proposal = _get_proposal(session, proposal_id)
    problems: list[str] = []
    objective = session.get(OrgObjective, proposal.objective_id)
    if objective is None:
        problems.append("objective_missing")
    else:
        if objective.revision != proposal.based_on_revision:
            problems.append("proposal_stale_revision")
        if objective.status != "active":
            problems.append("objective_inactive")
    if not proposal.evidence_refs():
        problems.append("proposal_evidence_required")
    if proposal.kind == "propose_plan":
        violations = _validate_nodes(session, org_id=proposal.org_id,
                                     objective=objective,
                                     nodes=_normalize_nodes(proposal.nodes()))
        problems.extend(violations)
    elif proposal.kind == "goal_conflict":
        for other_id in set(proposal.payload().get("conflicting_objective_ids") or []):
            other = session.get(OrgObjective, str(other_id))
            if other is None or other.org_id != proposal.org_id or other.status != "active":
                problems.append("conflict_objective_missing:%s" % other_id)
    elif proposal.kind == "temporary_collab":
        suggested = str(proposal.payload().get("suggested_position_id") or "").strip()
        if suggested:
            position = session.get(Position, suggested)
            declared = set(json.loads(position.capability_refs_json or "[]")) if position else set()
            capability = str(proposal.payload().get("needed_capability") or "").strip()
            if position is None or capability not in declared:
                problems.append("position_capability_missing:%s" % suggested)
    return {"proposal_id": proposal.proposal_id, "kind": proposal.kind,
            "status": proposal.status,
            "based_on_revision": proposal.based_on_revision,
            "current_objective_revision": objective.revision if objective else None,
            "evidence_refs": proposal.evidence_refs(),
            "expected_effect": proposal.expected_effect,
            "verifiable": proposal.status == "open" and not problems,
            "problems": sorted(set(problems))}


# ── 交付撤回传播与版本漂移 ───────────────────────────────────────────────────


def plan_nodes(session: Session, *, plan_id: str) -> list[PlanNode]:
    return list(session.scalars(select(PlanNode).where(
        PlanNode.plan_id == plan_id).order_by(PlanNode.id)))


def _plan(session: Session, plan_id: str) -> WorkPlan:
    row = session.get(WorkPlan, plan_id)
    if row is None:
        raise PlanError("plan_not_found")
    return row


def mark_delivery_withdrawn(session: Session, *, plan_id: str, task_id: int,
                            reason: str, actor_id: str) -> dict:
    """关键交付撤回：按依赖标记受影响里程碑与下游任务。

    - 触发条件是真实撤回：任务已不在 done（验收重开/取消）才允许传播；
    - 受影响里程碑证据失效（历史快照保留），下游任务按依赖闭包标记；
    - 无影响交付原样保留；重复调用幂等，不产生新的版本翻动。
    """
    if not reason.strip():
        raise PlanError("withdrawal_reason_required")
    plan = _plan(session, plan_id)
    if plan.status != "confirmed":
        raise PlanError("plan_not_confirmed")
    nodes = plan_nodes(session, plan_id=plan_id)
    target_nodes = [node for node in nodes if node.task_id == int(task_id)]
    if not target_nodes:
        raise PlanError("plan_task_not_in_plan")
    task = session.get(Task, int(task_id))
    if task is None:
        raise PlanError("plan_task_missing:%s" % task_id)
    if task.status == "done":
        # 交付仍完好：不能凭空传播撤回（无影响交付不被误伤）
        raise PlanError("delivery_not_withdrawn")
    affected_milestones: list[str] = []
    for node in target_nodes:
        if not node.milestone_id:
            continue
        milestone = session.get(Milestone, node.milestone_id)
        if milestone is not None and milestone.status == "evidenced":
            objectives.invalidate_milestone_evidence(
                session, milestone_id=milestone.milestone_id, reason=reason,
                actor_id=actor_id, evidence_refs=["task:%s" % task_id])
            affected_milestones.append(milestone.milestone_id)
    # 依赖闭包：直接或间接依赖该任务的全部下游节点
    dependents: dict[int, list[int]] = {}
    for node in nodes:
        for dep in node.depends_on():
            dependents.setdefault(int(dep), []).append(node.task_id)
    frontier, affected_tasks = [int(task_id)], {int(task_id)}
    while frontier:
        current = frontier.pop()
        for downstream in dependents.get(current, []):
            if downstream not in affected_tasks:
                affected_tasks.add(downstream)
                frontier.append(downstream)
    newly_affected: list[int] = []
    for node in nodes:
        if node.task_id in affected_tasks and node.status == "confirmed":
            node.status = "affected"
            node.affected_reason = ("upstream_delivery_withdrawn: task %s | %s"
                                    % (task_id, reason.strip()[:200]))
            node.revision += 1
            newly_affected.append(node.task_id)
    for downstream_task in sorted(newly_affected):
        # 工作中心可见：下游任务收到依赖失效事件（不改任务状态，不重复执行）
        session.add(TaskEvent(task_id=downstream_task,
                              event_type="plan_dependency_affected", actor=actor_id,
                              detail="plan:%s upstream task:%s withdrawn: %s"
                                     % (plan_id, task_id, reason.strip()[:200])))
    session.flush()
    return {"plan_id": plan_id, "withdrawn_task_id": int(task_id),
            "affected_milestones": sorted(set(affected_milestones)),
            "affected_task_ids": sorted(affected_tasks),
            "unaffected_task_ids": sorted(n.task_id for n in nodes
                                          if n.task_id not in affected_tasks)}


def plan_staleness(session: Session, *, plan_id: str) -> dict:
    """版本漂移检查：任务 revision 前进（范围变更）的节点标 stale；
    目标 revision 前进则整个计划过期（旧决定已随之失效）。"""
    plan = _plan(session, plan_id)
    objective = session.get(OrgObjective, plan.objective_id)
    stale_rows: list[dict] = []
    for node in plan_nodes(session, plan_id=plan_id):
        task = session.get(Task, node.task_id)
        if task is None:
            continue
        if task.revision != node.task_revision_recorded and node.status == "confirmed":
            node.status = "stale"
            node.affected_reason = ("task_revision %s -> %s"
                                    % (node.task_revision_recorded, task.revision))
            node.revision += 1
        if node.status == "stale":
            stale_rows.append({"task_id": node.task_id,
                               "recorded_revision": node.task_revision_recorded,
                               "current_revision": task.revision,
                               "reason": node.affected_reason})
    session.flush()
    return {"plan_id": plan_id,
            "objective_id": plan.objective_id,
            "based_on_objective_revision": plan.based_on_objective_revision,
            "current_objective_revision": objective.revision if objective else None,
            "objective_revised": bool(objective and
                                      objective.revision != plan.based_on_objective_revision),
            "stale_nodes": stale_rows,
            "stale_task_ids": sorted(row["task_id"] for row in stale_rows)}


def plan_overview(session: Session, *, plan_id: str) -> dict:
    """计划只读视图：节点引用与状态；不含执行语义。"""
    plan = _plan(session, plan_id)
    return {"plan_id": plan.plan_id, "org_id": plan.org_id,
            "objective_id": plan.objective_id, "title": plan.title,
            "status": plan.status, "revision": plan.revision,
            "based_on_objective_revision": plan.based_on_objective_revision,
            "nodes": [{"task_id": node.task_id, "unit_id": node.unit_id,
                       "position_id": node.position_id,
                       "required_capability": node.required_capability,
                       "depends_on": node.depends_on(),
                       "milestone_id": node.milestone_id,
                       "task_revision_recorded": node.task_revision_recorded,
                       "status": node.status} for node in plan_nodes(session, plan_id=plan_id)]}
