"""跨单元交接包与接收方核验（DE-40）。

指南 §18.2"多个岗位协作时用原任务的子交付/依赖关系及 ContextPack 交接，
不复制目标和状态"的产品核心，全部复用既有依赖：

- 交接包 = ContextPack 语义（结构化上下文 + snapshot hash，与
  organization.ResolvedContext.snapshot 同构）：产物 / 未完成项 / 验收版本 /
  责任与资源，全部引用原 Task 的稳定 ID，不复制目标与状态。
- 提案与裁决复用 DE-38 的 PositionHandoff / TenureReceipt 语义：
  prepare_handoff 只包装 org.propose_handoff（能力不匹配/版本过期/空间未绑定
  在那里被拒绝并给具体原因）；decide_from_review 只包装 org.decide_handoff
  （接受即责任岗位切换 + 旧岗位在途 Run 取消传播与回执对账）。
- 接收方核验：review_package 按接收方实际授权（成员×岗位×指派交集）裁剪——
  权限不足返回具体缺口（space_not_authorized / capability_not_granted），
  被裁剪内容只给数量不给路径与摘要，不泄露。
- 幂等：同一任务同时只允许一张未决交接单（DE-38 已保证），重复 prepare
  返回同一张单且不再发通知（outbox 意图键幂等）——重复交接不多建工作、
  不多发消息。
- 更换执行人保持原 Task/验收与交接证据：accept 只切 assigned_position 并追加
  TaskEvent(position_handoff)，任务主键/目标/验收/revision 与历史全部不变；
  无固定岗位流水线、不建第二目标库。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.ids import sha256_json
from evoharness.models import Task, TaskEvent
from evoharness.outbox import enqueue as outbox_enqueue
from evoharness.organization import service as org
from evoharness.organization.models import (Assignment, Membership, Position,
                                            PositionHandoff, TaskCrewMember)
from evoharness.work.models import Artifact, ExecutionRun
from evoharness.work import supplements
from evoharness.work.scheduling import ResourceLease

NOTICE_KIND = "notice"


class HandoffError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _loads(raw: str, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except ValueError:
        return fallback


def _now() -> datetime:
    return utcnow()


# ── 交接包：ContextPack 语义（结构化内容 + snapshot hash）────────────────────


def build_package(session: Session, task: Task) -> dict:
    """从原 Task/Run/回执事实汇编交接包：全部是引用，不复制目标或状态。"""
    artifacts = []
    for row in session.scalars(select(Artifact).join(
            ExecutionRun, Artifact.run_id == ExecutionRun.id).where(
            ExecutionRun.task_id == task.id).order_by(Artifact.id)):
        artifacts.append({"kind": row.kind, "path": row.path, "sha256": row.sha256,
                          "run_id": row.run_id, "task_revision": row.task_revision})
    unfinished = _unfinished_items(session, task)
    crew = [{"assignment_id": m.assignment_id, "role": m.role} for m in
            session.scalars(select(TaskCrewMember).where(
                TaskCrewMember.task_id == task.id,
                TaskCrewMember.status == "active"))]
    resources = [{"lease_id": row.lease_id,
                  "resource_kind": row.resource_kind,
                  "resource_key": row.resource_key,
                  "version": row.version}
                 for row in session.scalars(select(ResourceLease).where(
                     ResourceLease.holder_id == str(task.id),
                     ResourceLease.status == "held"))]
    body = {
        "task_id": task.id,
        "task_key": task.task_key,
        "acceptance": _loads(task.acceptance_json, []),
        "acceptance_revision": task.revision,
        "artifacts": artifacts,
        "unfinished": unfinished,
        "source_space_ids": [task.space_id] if task.space_id else [],
        "responsibility": {"assigned_position": task.assigned_position,
                           "crew": crew},
        "resources": resources,
    }
    body["package_hash"] = sha256_json(body)
    return body


def _unfinished_items(session: Session, task: Task) -> list[dict]:
    """未完成项 = 最近 Run 检查点的 todo + 未消费的在途补充（含取消）。"""
    items: list[dict] = []
    run = session.scalar(select(ExecutionRun).where(
        ExecutionRun.task_id == task.id).order_by(
        ExecutionRun.created_at.desc(), ExecutionRun.id.desc()).limit(1))
    if run is not None:
        checkpoint = _loads(run.checkpoint_json, {})
        for entry in checkpoint.get("todo", []) or []:
            items.append({"source": "checkpoint", "item": entry})
    for row in supplements.pending(session, task.id):
        items.append({"source": "supplement", "kind": row.event_kind,
                      "event_id": row.event_id})
    return items


def verify_package_hash(package: dict) -> bool:
    body = {k: v for k, v in package.items() if k != "package_hash"}
    return package.get("package_hash") == sha256_json(body)


# ── 提案：幂等包装 DE-38 propose_handoff ────────────────────────────────────


def prepare_handoff(session: Session, actor: "org.AuthenticatedPrincipal", *,
                    org_id: str, task_id: int, from_assignment_id: str,
                    to_position_id: str, requested_capability: str) -> dict:
    """发起跨单元交接：汇编交接包并交给 DE-38 提案入口。

    - 能力不匹配（handoff_capability_gap）、版本过期（handoff_stale_revision）、
      空间未绑定等拒绝原样上抛，原因具体；
    - 同一任务已有未决交接单 → 返回同一张单（duplicate=True），不多建工作；
    - 首次提案发一条通知意图（intent=handoff|task_key|handoff_id，outbox 幂等），
      重复提案不再发（不多发消息）。
    """
    task = session.get(Task, task_id)
    if task is None or task.org_id != org_id:
        raise HandoffError("task_not_in_organization")
    package = build_package(session, task)
    try:
        row = org.propose_handoff(
            session, actor, org_id=org_id, task_id=task_id,
            from_assignment_id=from_assignment_id, to_position_id=to_position_id,
            requested_capability=requested_capability,
            acceptance_revision=task.revision,
            artifacts=package["artifacts"], unfinished=package["unfinished"],
            source_space_ids=package["source_space_ids"])
    except org.OrganizationError as exc:
        if exc.code != "handoff_already_proposed":
            raise  # 能力不匹配/版本过期/空间未绑定等拒绝原样上抛，原因具体
        # 同一任务已有未决交接单：幂等返回同一张单，不多建工作、不再校验放行
        existing = session.scalar(select(PositionHandoff).where(
            PositionHandoff.task_id == task_id,
            PositionHandoff.status == "proposed"))
        return {"handoff": existing, "package": None, "duplicate": True,
                "notice_intent": ""}
    intent = f"handoff|{task.task_key}|{row.handoff_id}"
    outbox_enqueue(session, intent=intent, tenant=task.tenant, kind=NOTICE_KIND,
                   target_type="task", target_id=str(task.id),
                   payload={"text": f"任务 {task.task_key} 交接提案待核验",
                            "handoff_id": row.handoff_id,
                            "to_position": to_position_id,
                            "acceptance_revision": task.revision})
    session.flush()
    return {"handoff": row, "package": package, "duplicate": False,
            "notice_intent": intent}


# ── 接收方核验：按实际授权裁剪，缺口具体、不泄露内容 ─────────────────────────


def _receiver_scope(session: Session, actor, org_id: str) -> dict:
    """接收方实际授权范围：在任成员资格 × 指派（空间与岗位能力）。"""
    if not isinstance(actor, org.AuthenticatedPrincipal) or not actor.authenticated:
        raise HandoffError("authenticated_principal_required")
    now = _now()
    member = session.scalar(select(Membership).where(
        Membership.org_id == org_id, Membership.principal_id == actor.principal_id,
        Membership.tenant_id == actor.tenant_id, Membership.app_id == actor.app_id))
    if member is None or member.status != "active":
        raise HandoffError("membership_inactive")
    if member.valid_from is not None and member.valid_from > now:
        raise HandoffError("membership_inactive")
    if member.valid_until is not None and now >= member.valid_until:
        raise HandoffError("membership_inactive")
    space_ids: set[str] = set()
    capabilities: set[str] = set()
    for assignment in session.scalars(select(Assignment).where(
            Assignment.org_id == org_id,
            Assignment.membership_id == member.membership_id)):
        if assignment.status != "active":
            continue
        if assignment.valid_from is not None and assignment.valid_from > now:
            continue
        if assignment.valid_until is not None and now >= assignment.valid_until:
            continue
        space_ids |= set(_loads(assignment.space_ids_json, []))
        position = session.get(Position, assignment.position_id)
        if position is not None and position.status == "active":
            capabilities |= set(_loads(position.capability_refs_json, []))
    return {"membership_id": member.membership_id,
            "space_ids": space_ids, "capabilities": capabilities}


def review_package(session: Session, actor, *, org_id: str, handoff_id: str) -> dict:
    """接收方视角的交接包：权限不足返回具体缺口，被裁剪内容不外泄。

    - 空间未授权：该空间来源的产物路径/摘要全部裁剪，只留 kind 与数量；
      未完成项同理（来源内容可能含被裁剪上下文）；
    - 能力未授予：缺口列出请求的能力名（这是合同要求，不是内容泄露）；
    - 验收条件始终可见：那是接收方必须满足的合同，不是被裁剪的来源内容。
    """
    handoff = session.get(PositionHandoff, handoff_id)
    if handoff is None or handoff.org_id != org_id:
        raise HandoffError("handoff_not_in_organization")
    task = session.get(Task, handoff.task_id)
    if task is None:
        raise HandoffError("task_not_found")
    scope = _receiver_scope(session, actor, org_id)
    gaps: list[dict] = []
    if task.space_id and task.space_id not in scope["space_ids"]:
        gaps.append({"code": "space_not_authorized", "space_id": task.space_id})
    if (handoff.requested_capability
            and handoff.requested_capability not in scope["capabilities"]):
        gaps.append({"code": "capability_not_granted",
                     "capability": handoff.requested_capability})
    # 来源空间未授权 → 该空间的内容（产物路径/摘要、未完成项明细）全部裁剪
    redacted = any(g["code"] == "space_not_authorized" for g in gaps)
    artifacts = _loads(handoff.artifacts_json, [])
    unfinished = _loads(handoff.unfinished_json, [])
    package = {
        "handoff_id": handoff.handoff_id,
        "status": handoff.status,
        "task_id": task.id,
        "task_key": task.task_key,
        "acceptance": _loads(task.acceptance_json, []),
        "acceptance_revision": handoff.acceptance_revision,
        "responsibility": {"assigned_position": task.assigned_position,
                           "requested_capability": handoff.requested_capability},
        "source_space_ids": _loads(handoff.source_space_ids_json, []),
        "gaps": gaps,
    }
    if redacted:
        package["artifacts"] = [{"kind": a.get("kind"), "redacted": True}
                                for a in artifacts]
        package["unfinished"] = [{"redacted": True, "count": len(unfinished)}]
        package["redacted"] = True
    else:
        package["artifacts"] = artifacts
        package["unfinished"] = unfinished
        package["redacted"] = False
    resources = [{"lease_id": row.lease_id,
                  "resource_kind": row.resource_kind,
                  "resource_key": row.resource_key,
                  "version": row.version}
                 for row in session.scalars(select(ResourceLease).where(
                     ResourceLease.holder_id == str(task.id),
                     ResourceLease.status == "held"))]
    package["resources"] = resources
    return package


# ── 裁决：核验缺口后接受/有因拒绝 ────────────────────────────────────────────


def decide_from_review(session: Session, actor, *, org_id: str, handoff_id: str,
                       decision: str, reason: str = "",
                       expected_revision: int | None = None) -> PositionHandoff:
    """接收方裁决：接受前必须无未决缺口；拒绝必须带具体原因（DE-38 校验）。"""
    if decision == "accept":
        review = review_package(session, actor, org_id=org_id, handoff_id=handoff_id)
        if review["gaps"]:
            codes = ",".join(sorted(g["code"] for g in review["gaps"]))
            raise HandoffError(f"accept_blocked_by_gaps:{codes}")
    return org.decide_handoff(session, actor, org_id=org_id, handoff_id=handoff_id,
                              decision=decision, reason=reason,
                              expected_revision=expected_revision)


def handoff_history(session: Session, *, org_id: str, task_id: int) -> dict:
    """交接证据视图：交接单生命周期 + 任务上的 position_handoff 事件。"""
    handoffs = [{"handoff_id": row.handoff_id,
                 "from_position_id": row.from_position_id,
                 "to_position_id": row.to_position_id,
                 "requested_capability": row.requested_capability,
                 "acceptance_revision": row.acceptance_revision,
                 "status": row.status,
                 "decision_reason": row.decision_reason}
                for row in session.scalars(select(PositionHandoff).where(
                    PositionHandoff.org_id == org_id,
                    PositionHandoff.task_id == task_id).order_by(PositionHandoff.created_at))]
    events = [{"actor": e.actor, "detail": e.detail,
               "evidence": _loads(e.evidence_message_ids, {})}
              for e in session.scalars(select(TaskEvent).where(
                  TaskEvent.task_id == task_id,
                  TaskEvent.event_type == "position_handoff").order_by(TaskEvent.id))]
    return {"handoffs": handoffs, "events": events}
