"""组织目标、单元目标与里程碑：验收版本与证据绑定（DE-39）。

指南 §18.1"目标进入任务，结果返回目标"的产品核心：

- 目标变更/验收/负责人变更是**显式版本事件**（revision CAS + ObjectiveEvent），
  确认权限来自既有决定（DE-12 governance.authorization：版本、载荷、动作、
  撤回任一不符即拒绝）；模型与协调者不能自授权改目标。
- 里程碑引用具体交付条件并绑定覆盖它的原 Task ID（不造第二任务库）；
  子任务验收只更新它覆盖的里程碑证据（record_task_evidence），
  绝不自动把上层目标标完成——目标达成只能经 mark_achieved 在
  全部里程碑证据齐备且有权决定在场时显式进入。
- 目标进度按里程碑证据状态汇报（objective_progress），不按 done 数量求百分比；
  证据失效保留历史完成快照（history_json），不抹掉已发生事实。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from evoharness.clock import utcnow
from evoharness.ids import sha256_json
from evoharness.models import Base, Task, TaskStatus
from evoharness.organization.models import Organization, OrganizationUnit
from evoharness.governance import authorization as gov

# 目标语义变更的动作名：决定（Decision.actions_json）必须显式包含它
OBJECTIVE_MANAGE_ACTION = "objective.manage"


class ObjectiveError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _now() -> datetime:
    return utcnow()


def _require_decision(session, *, target_id: str, target_revision: int, payload: dict):
    """语义变更必须持有既有决定（DE-12）：目标/版本/载荷任一不符即拒绝。

    决定缺失/过期/载荷不符抛 governance 的 AuthorizationError——授权语义
    归 DE-12 所有，本层不吞不换。
    """
    return gov.validate(session, target_type="objective", target_id=target_id,
                        target_revision=target_revision,
                        payload_hash=sha256_json(payload),
                        action=OBJECTIVE_MANAGE_ACTION)


# ── 实体 ─────────────────────────────────────────────────────────────────────


class OrgObjective(Base):
    """组织目标（unit_id 为空）或单元目标；acceptance 绑定验收版本。"""
    __tablename__ = "org_objectives"
    objective_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    unit_id: Mapped[str] = mapped_column(String(64), default="")
    parent_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("org_objectives.objective_id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    acceptance_json: Mapped[str] = mapped_column(Text, default="[]")
    owner: Mapped[str] = mapped_column(String(128), default="")
    deadline_text: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    def acceptance(self) -> list:
        try:
            return json.loads(self.acceptance_json or "[]")
        except ValueError:
            return []


class Milestone(Base):
    """里程碑：引用具体交付条件 + 覆盖它的原 Task ID；证据绑定 task revision。"""
    __tablename__ = "org_milestones"
    milestone_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    objective_id: Mapped[str] = mapped_column(
        ForeignKey("org_objectives.objective_id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    deliverable_condition: Mapped[str] = mapped_column(Text, default="")
    covered_by_task_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    invalidated_json: Mapped[str] = mapped_column(Text, default="{}")
    history_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    def covered_task_ids(self) -> list[int]:
        try:
            return [int(v) for v in json.loads(self.covered_by_task_ids_json or "[]")]
        except ValueError:
            return []

    def evidence(self) -> dict:
        try:
            return json.loads(self.evidence_json or "{}")
        except ValueError:
            return {}

    def history(self) -> list:
        try:
            return json.loads(self.history_json or "[]")
        except ValueError:
            return []


class ObjectiveEvent(Base):
    """显式版本事件审计：目标/验收/负责人/证据的每次变化都可定位。"""
    __tablename__ = "org_objective_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    objective_id: Mapped[str] = mapped_column(
        ForeignKey("org_objectives.objective_id"), index=True)
    event_kind: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(128), default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=_now)


# ── 内部校验 ─────────────────────────────────────────────────────────────────


def _get_objective(session: Session, objective_id: str) -> OrgObjective:
    row = session.get(OrgObjective, objective_id)
    if row is None:
        raise ObjectiveError("objective_not_found")
    return row


def _validate_scope(session: Session, *, org_id: str, unit_id: str) -> None:
    org = session.get(Organization, org_id)
    if org is None or not org.enabled:
        raise ObjectiveError("organization_not_found_or_paused")
    if unit_id:
        unit = session.get(OrganizationUnit, unit_id)
        if unit is None or unit.org_id != org_id or not unit.enabled:
            raise ObjectiveError("organization_unit_not_found_or_paused")


def _validate_parent(session: Session, *, org_id: str, unit_id: str,
                     parent_id: str) -> None:
    if not parent_id:
        return
    parent = session.get(OrgObjective, parent_id)
    if parent is None or parent.org_id != org_id:
        raise ObjectiveError("objective_parent_not_in_organization")
    if parent.status != "active":
        raise ObjectiveError("objective_parent_inactive")
    # 单元目标挂组织目标或同单元上层目标，不跨单元挂父（跨单元靠计划协作）
    if unit_id and parent.unit_id not in ("", unit_id):
        raise ObjectiveError("objective_parent_unit_mismatch")


def _org_task(session: Session, task_id: int, *, org_id: str, error) -> Task:
    task = session.get(Task, int(task_id))
    if task is None:
        raise error("task_not_found")
    if org_id and task.org_id != org_id:
        raise error("task_not_in_organization")
    return task


def _event(session: Session, objective: OrgObjective, *, kind: str, actor: str,
           detail: dict, evidence_refs=(), revision: int | None = None) -> ObjectiveEvent:
    row = ObjectiveEvent(objective_id=objective.objective_id, event_kind=kind,
                         actor=actor,
                         revision=objective.revision if revision is None else revision,
                         detail_json=_json(detail),
                         evidence_refs_json=_json(list(evidence_refs)))
    session.add(row)
    session.flush()
    return row


def _record_milestone_history(milestone: Milestone, *, kind: str, snapshot: dict) -> None:
    history = milestone.history()
    history.append({"kind": kind, "at": _now().isoformat(), "snapshot": snapshot})
    milestone.history_json = _json(history)


# ── 决定载荷（批准方与服务方共用同一序列化）─────────────────────────────────


def creation_payload(*, title: str, acceptance: list, owner: str, unit_id: str = "",
                     parent_id: str = "", deadline_text: str = "") -> dict:
    return {"title": title, "acceptance": acceptance, "owner": owner,
            "unit_id": unit_id, "parent_id": parent_id, "deadline_text": deadline_text}


def revision_payload(*, changes: dict, reason: str) -> dict:
    return {"changes": changes, "reason": reason}


def achieve_payload(*, milestone_ids: list) -> dict:
    return {"achieve": True, "milestone_evidenced": sorted(milestone_ids)}


# ── 目标生命周期 ─────────────────────────────────────────────────────────────


def create_objective(session: Session, *, org_id: str, objective_id: str, title: str,
                     acceptance: list, owner: str, actor_id: str, unit_id: str = "",
                     parent_id: str = "", deadline_text: str = "") -> OrgObjective:
    """建立组织/单元目标：验收条件非空，且必须持有 revision=1 的既有决定。"""
    _validate_scope(session, org_id=org_id, unit_id=unit_id)
    _validate_parent(session, org_id=org_id, unit_id=unit_id, parent_id=parent_id)
    if not title.strip():
        raise ObjectiveError("objective_title_required")
    if not acceptance:
        # 空验收 = 验收歧义：不允许建只有口号的目标
        raise ObjectiveError("objective_acceptance_required")
    if session.get(OrgObjective, objective_id) is not None:
        raise ObjectiveError("objective_exists")
    payload = creation_payload(title=title.strip(), acceptance=acceptance, owner=owner,
                               unit_id=unit_id, parent_id=parent_id,
                               deadline_text=deadline_text)
    _require_decision(session, target_id=objective_id, target_revision=1, payload=payload)
    row = OrgObjective(objective_id=objective_id, org_id=org_id, unit_id=unit_id,
                       parent_id=parent_id or None, title=title.strip(),
                       acceptance_json=_json(acceptance), owner=owner,
                       deadline_text=deadline_text)
    session.add(row)
    session.flush()
    _event(session, row, kind="created", actor=actor_id,
           detail={"acceptance_items": len(acceptance), "unit_id": unit_id,
                   "parent_id": parent_id})
    return row


def revise_objective(session: Session, *, objective_id: str, actor_id: str,
                     expected_revision: int, reason: str, title: str | None = None,
                     acceptance: list | None = None, owner: str | None = None,
                     deadline_text: str | None = None,
                     evidence_refs=()) -> OrgObjective:
    """目标/验收/负责人变更是显式版本事件：revision+1，旧决定随之失效。"""
    if not reason.strip():
        raise ObjectiveError("objective_change_reason_required")
    row = _get_objective(session, objective_id)
    if row.revision != expected_revision:
        raise ObjectiveError("stale_objective_revision")
    changes: dict = {}
    if title is not None and title.strip() and title.strip() != row.title:
        changes["title"] = title.strip()
    if acceptance is not None and _json(acceptance) != row.acceptance_json:
        changes["acceptance"] = acceptance
    if owner is not None and owner.strip() and owner.strip() != row.owner:
        changes["owner"] = owner.strip()
    if deadline_text is not None and deadline_text != row.deadline_text:
        changes["deadline_text"] = deadline_text
    if not changes:
        return row
    if "acceptance" in changes and not changes["acceptance"]:
        raise ObjectiveError("objective_acceptance_required")
    _require_decision(session, target_id=objective_id,
                      target_revision=expected_revision,
                      payload=revision_payload(changes=changes, reason=reason.strip()))
    if "title" in changes:
        row.title = changes["title"]
    if "acceptance" in changes:
        row.acceptance_json = _json(changes["acceptance"])
    if "owner" in changes:
        row.owner = changes["owner"]
    if "deadline_text" in changes:
        row.deadline_text = changes["deadline_text"]
    row.revision = expected_revision + 1
    kind = ("objective_revised" if len(changes) > 1 else
            {"title": "goal_revised", "acceptance": "acceptance_revised",
             "owner": "owner_changed", "deadline_text": "deadline_revised"}[next(iter(changes))])
    _event(session, row, kind=kind, actor=actor_id,
           detail={"changed": sorted(changes), "reason": reason.strip()[:400]},
           evidence_refs=evidence_refs)
    session.flush()
    return row


def add_milestone(session: Session, *, objective_id: str, milestone_id: str, title: str,
                  deliverable_condition: str, actor_id: str,
                  covered_by_task_ids=()) -> Milestone:
    """里程碑引用具体交付条件与覆盖它的原 Task ID；不造第二任务库。"""
    objective = _get_objective(session, objective_id)
    if not title.strip():
        raise ObjectiveError("milestone_title_required")
    if not deliverable_condition.strip():
        # 交付条件为空 = 里程碑验收歧义，明确阻止
        raise ObjectiveError("milestone_deliverable_condition_required")
    if session.get(Milestone, milestone_id) is not None:
        raise ObjectiveError("milestone_exists")
    task_ids: list[int] = []
    for raw in covered_by_task_ids:
        task = _org_task(session, raw, org_id=objective.org_id, error=ObjectiveError)
        if task.id not in task_ids:
            task_ids.append(task.id)
    row = Milestone(milestone_id=milestone_id, objective_id=objective_id,
                    title=title.strip(), deliverable_condition=deliverable_condition.strip(),
                    covered_by_task_ids_json=_json(task_ids))
    session.add(row)
    session.flush()
    _event(session, objective, kind="milestone_added", actor=actor_id,
           detail={"milestone_id": milestone_id, "covered_task_ids": task_ids})
    return row


# ── 结果返回目标：证据回写 ───────────────────────────────────────────────────


def milestones_for_task(session: Session, *, task_id: int) -> list[Milestone]:
    """任务覆盖的里程碑（只读）：供工作中心/通道层投影目标引用。"""
    rows = list(session.scalars(select(Milestone).order_by(Milestone.milestone_id)))
    return [row for row in rows if int(task_id) in row.covered_task_ids()]


def record_milestone_evidence(session: Session, *, milestone_id: str, task_id: int,
                              actor_id: str, artifact_refs=(), note: str = "") -> Milestone:
    """单条证据回写：绑定任务当前 revision；任务未验收 done 则拒绝。

    这是"结果返回目标"的唯一入口：聊天完成信号与 Run 退出都不经过这里，
    更不会改目标状态。
    """
    milestone = session.get(Milestone, milestone_id)
    if milestone is None:
        raise ObjectiveError("milestone_not_found")
    task = _org_task(session, task_id, org_id="", error=ObjectiveError)
    if task.status != TaskStatus.DONE:
        # done 的唯一入口是 DE-13 验收服务；没有验收就没有里程碑证据
        raise ObjectiveError("milestone_evidence_requires_accepted_delivery")
    if int(task_id) not in milestone.covered_task_ids():
        raise ObjectiveError("milestone_not_covered_by_task")
    previous = milestone.evidence()
    if milestone.status == "evidenced" and previous.get("task_revision") == task.revision:
        # 同一验收版本重复回写：幂等，不再翻版本
        return milestone
    if milestone.status == "evidenced":
        _record_milestone_history(milestone, kind="evidence_superseded",
                                  snapshot=dict(previous))
    snapshot = {"task_id": task.id, "task_revision": task.revision,
                "artifact_refs": list(artifact_refs), "verified_by": actor_id,
                "verified_at": _now().isoformat(), "note": note[:400]}
    milestone.evidence_json = _json(snapshot)
    milestone.invalidated_json = "{}"
    milestone.status = "evidenced"
    milestone.revision += 1
    objective = _get_objective(session, milestone.objective_id)
    _event(session, objective, kind="milestone_evidence_recorded", actor=actor_id,
           detail={"milestone_id": milestone_id, "task_id": task.id,
                   "task_revision": task.revision,
                   "previous_status": "evidenced" if previous else "pending"})
    session.flush()
    return milestone


def record_task_evidence(session: Session, *, task_id: int, actor_id: str,
                         artifact_refs=(), note: str = "") -> list[Milestone]:
    """子任务验收后回写它覆盖的全部里程碑——只更新覆盖的，不碰其他里程碑，
    更不把上层目标标完成。幂等：同一验收版本重复调用不再翻版本。"""
    updated: list[Milestone] = []
    for milestone in milestones_for_task(session, task_id=task_id):
        updated.append(record_milestone_evidence(
            session, milestone_id=milestone.milestone_id, task_id=task_id,
            actor_id=actor_id, artifact_refs=artifact_refs, note=note))
    return updated


def invalidate_milestone_evidence(session: Session, *, milestone_id: str, reason: str,
                                  actor_id: str, evidence_refs=()) -> Milestone:
    """关键交付撤回/失效：里程碑回到 invalidated；历史完成快照保留不删。"""
    if not reason.strip():
        raise ObjectiveError("invalidation_reason_required")
    milestone = session.get(Milestone, milestone_id)
    if milestone is None:
        raise ObjectiveError("milestone_not_found")
    if milestone.status != "evidenced":
        # 未有证据或已失效：幂等，不重复翻版本
        return milestone
    previous = milestone.evidence()
    _record_milestone_history(milestone, kind="evidence_invalidated",
                              snapshot=dict(previous))
    milestone.invalidated_json = _json({"reason": reason.strip()[:400],
                                        "by": actor_id,
                                        "at": _now().isoformat(),
                                        "evidence_refs": list(evidence_refs)})
    milestone.status = "invalidated"
    milestone.revision += 1
    objective = _get_objective(session, milestone.objective_id)
    _event(session, objective, kind="milestone_evidence_invalidated", actor=actor_id,
           detail={"milestone_id": milestone_id, "reason": reason.strip()[:200],
                   "was": previous.get("task_revision")},
           evidence_refs=evidence_refs)
    session.flush()
    return milestone


# ── 目标达成与进度 ───────────────────────────────────────────────────────────


def mark_achieved(session: Session, *, objective_id: str, actor_id: str,
                  expected_revision: int) -> OrgObjective:
    """目标达成只能显式进入：全部里程碑证据齐备 + 既有决定；任何一条
    pending/invalidated 都阻止，聊天完成与子任务验收永远不会走到这里。"""
    row = _get_objective(session, objective_id)
    if row.revision != expected_revision:
        raise ObjectiveError("stale_objective_revision")
    if row.status != "active":
        raise ObjectiveError("objective_not_active")
    milestones = list(session.scalars(select(Milestone).where(
        Milestone.objective_id == objective_id)))
    if not milestones:
        raise ObjectiveError("objective_has_no_milestones")
    blocked = [m.milestone_id for m in milestones if m.status != "evidenced"]
    if blocked:
        raise ObjectiveError("objective_milestones_not_evidenced:" + ",".join(sorted(blocked)))
    payload = achieve_payload(milestone_ids=[m.milestone_id for m in milestones])
    _require_decision(session, target_id=objective_id,
                      target_revision=expected_revision, payload=payload)
    row.status = "achieved"
    row.revision = expected_revision + 1
    _event(session, row, kind="objective_achieved", actor=actor_id,
           detail={"milestones": sorted(m.milestone_id for m in milestones)})
    session.flush()
    return row


def objective_progress(session: Session, *, objective_id: str) -> dict:
    """目标进度口径：里程碑证据状态，不按 done 数量求百分比。"""
    row = _get_objective(session, objective_id)
    milestones = list(session.scalars(select(Milestone).where(
        Milestone.objective_id == objective_id).order_by(Milestone.milestone_id)))
    counts = {
        "total": len(milestones),
        "evidenced": sum(1 for m in milestones if m.status == "evidenced"),
        "pending": sum(1 for m in milestones if m.status == "pending"),
        "invalidated": sum(1 for m in milestones if m.status == "invalidated"),
    }
    return {
        "objective_id": row.objective_id,
        "org_id": row.org_id,
        "unit_id": row.unit_id,
        "status": row.status,
        "revision": row.revision,
        "owner": row.owner,
        "milestones": [{"milestone_id": m.milestone_id, "title": m.title,
                        "status": m.status,
                        "covered_task_ids": m.covered_task_ids()} for m in milestones],
        "counts": counts,
        # 显式声明进度口径：证据状态而非 done 数量；percent 恒为 None
        "progress_model": "milestone_evidence",
        "percent_complete": None,
    }


def objective_events(session: Session, *, objective_id: str) -> list[ObjectiveEvent]:
    return list(session.scalars(select(ObjectiveEvent).where(
        ObjectiveEvent.objective_id == objective_id).order_by(ObjectiveEvent.id)))
