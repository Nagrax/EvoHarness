"""Organization configuration and versioned authorization in the existing DB session.

AuthenticatedPrincipal is an internal identity-adapter result, never an HTTP body schema.
Roles are display metadata; only explicit grants and delegation ceilings authorize actions.

DE-38 任职生命周期增量：岗位契约更新、暂停/离任接线（停止新认领、撤销未生效委派、
取消传播到在途 Run、在途回执对账）、岗位间能力转交（交接回执）、按任务临时组队。
组织资料与个人来源分别继承：接任者只取得合法组织产物，个人 token 与私有笔记不随岗位转移。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Callable
from uuid import uuid4
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from evoharness.clock import utcnow
from evoharness.models import Task, TaskEvent
from evoharness.work.models import ExecutionRun
from evoharness.work import supplements
from .models import (Organization, BusinessSpace, OrganizationUnit, UnitSpaceBinding,
                     Position, Membership, EmployeeProfile, Assignment, OrganizationEvent,
                     ChannelBinding, PositionHandoff, TaskCrewMember, TenureReceipt)


class OrganizationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    principal_id: str
    tenant_id: str
    app_id: str
    authenticated: bool
    authorities: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ResolvedContext:
    values: dict
    actor: AuthenticatedPrincipal = field(repr=False)

    def snapshot(self) -> dict:
        body = json.loads(_json(self.values))
        return {**body, "snapshot_hash": _hash(body)}

    @property
    def allowed_actions(self) -> frozenset[str]:
        return frozenset(self.values["allowed_actions"])

    @property
    def assignment_id(self) -> str:
        return self.values["assignment_id"]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _actions(raw: str) -> set[str]:
    return set(json.loads(raw or "[]"))


def _time(value: datetime | None) -> datetime:
    value = value or utcnow()
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _live(row, now: datetime) -> bool:
    return row.status == "active" and _time(row.valid_from) <= now and (row.valid_until is None or now < _time(row.valid_until))


def _identity(actor: AuthenticatedPrincipal) -> None:
    if not isinstance(actor, AuthenticatedPrincipal) or not actor.authenticated or not actor.principal_id:
        raise OrganizationError("authenticated_principal_required")


def _owned(session: Session, model, identifier: str, org_id: str):
    row = session.get(model, identifier)
    if row is None or row.org_id != org_id:
        raise OrganizationError("resource_not_in_organization")
    return row


def _membership(session: Session, actor: AuthenticatedPrincipal, org_id: str, now: datetime) -> Membership:
    _identity(actor)
    row = session.scalar(select(Membership).where(Membership.org_id == org_id,
        Membership.principal_id == actor.principal_id, Membership.tenant_id == actor.tenant_id,
        Membership.app_id == actor.app_id))
    if row is None or not _live(row, now):
        raise OrganizationError("membership_inactive")
    return row


def _manager(session: Session, actor: AuthenticatedPrincipal, org_id: str) -> Membership:
    row = _membership(session, actor, org_id, _time(None))
    if "organization.manage" not in _actions(row.grants_json):
        raise OrganizationError("organization_management_not_granted")
    return row


def _subset(values, ceiling) -> set[str]:
    values = set(values)
    if not values <= set(ceiling):
        raise OrganizationError("grant_exceeds_authorized_ceiling")
    return values


def _budget(value: float, ceiling: float | None = None) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0 or (ceiling is not None and value > ceiling):
        raise OrganizationError("budget_exceeds_authorized_ceiling")
    return value


def _event(session: Session, org_id: str, actor: str, action: str, target: str, detail: dict, *, pending: bool = False):
    event = OrganizationEvent(org_id=org_id, actor_membership_id=actor, action=action,
                              target_id=target, detail_json=_json(detail),
                              effects_status="pending" if pending else "not_required")
    session.add(event)
    session.flush()
    return event


def _revise(session: Session, model, row, expected_revision: int | None, **values):
    expected = row.revision if expected_revision is None else expected_revision
    primary = model.__mapper__.primary_key[0]
    identifier = getattr(row, primary.key)
    changed = session.execute(update(model).where(primary == identifier, model.revision == expected)
                              .values(**values, revision=expected + 1), execution_options={"synchronize_session": False})
    if changed.rowcount != 1:
        raise OrganizationError("stale_revision")
    session.flush()
    session.refresh(row)
    return row


def bootstrap_organization(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, title: str,
                           membership_id: str, budget_limit: float = 0) -> Organization:
    _identity(actor)
    if "organization.bootstrap" not in actor.authorities:
        raise OrganizationError("organization_bootstrap_not_granted")
    if session.get(Organization, org_id) is not None:
        raise OrganizationError("organization_exists")
    org = Organization(org_id=org_id, title=title, owner_principal_id=actor.principal_id,
                       budget_limit=_budget(budget_limit))
    session.add(org)
    session.flush()
    grants = set(actor.authorities) - {"organization.bootstrap"}
    # Bootstrap does not invent tool/read/send rights beyond the authenticated adapter.
    member = Membership(membership_id=membership_id, org_id=org_id, principal_id=actor.principal_id,
        tenant_id=actor.tenant_id, app_id=actor.app_id, grants_json=_json(sorted(grants)),
        delegable_actions_json=_json(sorted(grants)), roles_json='["owner"]')
    session.add(member)
    session.flush()
    _event(session, org_id, membership_id, "organization.created", org_id, {"revision": org.revision})
    return org


def create_space(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, space_id: str, title: str) -> BusinessSpace:
    manager = _manager(session, actor, org_id)
    row = BusinessSpace(org_id=org_id, space_id=space_id, title=title,
                        owner_principal_id=actor.principal_id, knowledge_namespace=f"{org_id}/{space_id}")
    session.add(row)
    session.flush()
    _event(session, org_id, manager.membership_id, "space.created", space_id, {})
    return row


def bind_space(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, unit_id: str, space_id: str):
    manager = _manager(session, actor, org_id)
    _owned(session, OrganizationUnit, unit_id, org_id)
    _owned(session, BusinessSpace, space_id, org_id)
    existing = session.scalar(select(UnitSpaceBinding).where(UnitSpaceBinding.unit_id == unit_id, UnitSpaceBinding.space_id == space_id))
    if existing is not None:
        return existing
    row = UnitSpaceBinding(org_id=org_id, unit_id=unit_id, space_id=space_id)
    session.add(row)
    session.flush()
    _event(session, org_id, manager.membership_id, "unit.space_bound", unit_id, {"space_id": space_id})
    return row


def create_unit(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, unit_id: str, title: str,
                parent_id: str | None = None) -> OrganizationUnit:
    manager = _manager(session, actor, org_id)
    if parent_id:
        _owned(session, OrganizationUnit, parent_id, org_id)
    row = OrganizationUnit(org_id=org_id, unit_id=unit_id, title=title, parent_id=parent_id)
    session.add(row)
    session.flush()
    _event(session, org_id, manager.membership_id, "unit.created", unit_id, {})
    return row


def move_unit(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, unit_id: str,
              parent_id: str | None, expected_revision: int):
    manager = _manager(session, actor, org_id)
    row = _owned(session, OrganizationUnit, unit_id, org_id)
    cursor, seen = parent_id, {unit_id}
    while cursor:
        if cursor in seen:
            raise OrganizationError("organization_unit_cycle")
        seen.add(cursor)
        cursor = _owned(session, OrganizationUnit, cursor, org_id).parent_id
    _revise(session, OrganizationUnit, row, expected_revision, parent_id=parent_id)
    _event(session, org_id, manager.membership_id, "unit.moved", unit_id, {"parent_id": parent_id})
    return row


def create_position(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, unit_id: str,
                    position_id: str, title: str, mission: str, actions: set[str], budget_limit: float = 0,
                    inputs=(), outputs=(), acceptance=(), capability_refs=()) -> Position:
    manager = _manager(session, actor, org_id)
    _owned(session, OrganizationUnit, unit_id, org_id)
    _subset(actions, _actions(manager.delegable_actions_json))
    org = session.get(Organization, org_id)
    row = Position(org_id=org_id, unit_id=unit_id, position_id=position_id, title=title, mission=mission,
        action_ceiling_json=_json(sorted(actions)), budget_limit=_budget(budget_limit, org.budget_limit),
        inputs_json=_json(list(inputs)), outputs_json=_json(list(outputs)), acceptance_json=_json(list(acceptance)),
        capability_refs_json=_json(list(capability_refs)))
    session.add(row)
    session.flush()
    _event(session, org_id, manager.membership_id, "position.created", position_id, {})
    return row


def add_member(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, membership_id: str,
               principal_id: str, tenant_id: str, app_id: str, grants: set[str], principal_kind="human",
               roles=(), delegable_actions=(), valid_from: datetime | None = None, valid_until: datetime | None = None) -> Membership:
    manager = _manager(session, actor, org_id)
    if principal_kind not in {"human", "ai_service"}:
        raise OrganizationError("invalid_principal_kind")
    grants = _subset(grants, _actions(manager.delegable_actions_json))
    _subset(delegable_actions, grants)
    start = _time(valid_from)
    if valid_until is not None and _time(valid_until) <= start:
        raise OrganizationError("invalid_membership_validity")
    row = Membership(org_id=org_id, membership_id=membership_id, principal_id=principal_id,
        tenant_id=tenant_id, app_id=app_id, principal_kind=principal_kind, roles_json=_json(list(roles)),
        grants_json=_json(sorted(grants)), delegable_actions_json=_json(sorted(delegable_actions)),
        valid_from=start, valid_until=_time(valid_until) if valid_until else None)
    session.add(row)
    session.flush()
    _event(session, org_id, manager.membership_id, "membership.created", membership_id, {"principal_kind": principal_kind})
    return row


def create_employee(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, employee_id: str,
                    membership_id: str, display_name: str, runtime_binding: str = "") -> EmployeeProfile:
    manager = _manager(session, actor, org_id)
    member = _owned(session, Membership, membership_id, org_id)
    if not _live(member, _time(None)):
        raise OrganizationError("membership_inactive")
    row = EmployeeProfile(org_id=org_id, employee_id=employee_id, membership_id=membership_id,
                          display_name=display_name, runtime_binding=runtime_binding)
    session.add(row)
    session.flush()
    _event(session, org_id, manager.membership_id, "employee.created", employee_id, {})
    return row


def update_runtime(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, employee_id: str,
                   runtime_binding: str, expected_revision: int):
    manager = _manager(session, actor, org_id)
    row = _owned(session, EmployeeProfile, employee_id, org_id)
    before = row.runtime_binding
    _revise(session, EmployeeProfile, row, expected_revision, runtime_binding=runtime_binding)
    _event(session, org_id, manager.membership_id, "employee.runtime_changed", employee_id,
           {"before": before, "after": runtime_binding, "revision": row.revision})
    return row


def assign(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, assignment_id: str,
           position_id: str, employee_id: str, space_ids: set[str], actions: set[str],
           budget_limit: float = 0, valid_from: datetime | None = None, valid_until: datetime | None = None) -> Assignment:
    manager = _manager(session, actor, org_id)
    position = _owned(session, Position, position_id, org_id)
    employee = _owned(session, EmployeeProfile, employee_id, org_id)
    member = _owned(session, Membership, employee.membership_id, org_id)
    if position.status != "active" or employee.status != "active" or not _live(member, _time(None)):
        raise OrganizationError("assignment_subject_inactive")
    _subset(actions, _actions(manager.delegable_actions_json) & _actions(member.grants_json) & _actions(position.action_ceiling_json))
    if not space_ids:
        raise OrganizationError("assignment_space_required")
    for space_id in space_ids:
        _owned(session, BusinessSpace, space_id, org_id)
        binding = session.scalar(select(UnitSpaceBinding.id).where(UnitSpaceBinding.unit_id == position.unit_id,
                                                                  UnitSpaceBinding.space_id == space_id))
        if binding is None:
            raise OrganizationError("space_not_bound_to_unit")
    start = _time(valid_from)
    end = _time(valid_until) if valid_until else None
    if end is not None and end <= start:
        raise OrganizationError("invalid_assignment_validity")
    if member.valid_until and (end is None or end > _time(member.valid_until)):
        raise OrganizationError("assignment_outlives_membership")
    row = Assignment(org_id=org_id, assignment_id=assignment_id, position_id=position_id, employee_id=employee_id,
        membership_id=member.membership_id, granted_by=manager.membership_id,
        actions_json=_json(sorted(actions)), space_ids_json=_json(sorted(space_ids)),
        budget_limit=_budget(budget_limit, position.budget_limit), valid_from=start, valid_until=end)
    session.add(row)
    session.flush()
    _event(session, org_id, manager.membership_id, "assignment.created", assignment_id, {})
    return row


def _unit_active(session: Session, unit: OrganizationUnit, org_id: str) -> list[dict]:
    seen, path = set(), []
    while unit:
        if unit.unit_id in seen or not unit.enabled:
            raise OrganizationError("organization_unit_inactive")
        seen.add(unit.unit_id)
        path.append({"unit_id": unit.unit_id, "revision": unit.revision})
        unit = _owned(session, OrganizationUnit, unit.parent_id, org_id) if unit.parent_id else None
    return path


def resolve_context(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, space_id: str,
                    assignment_id: str, expected_revision: int | None = None,
                    now: datetime | None = None) -> ResolvedContext:
    now = _time(now)
    member = _membership(session, actor, org_id, now)
    org = session.get(Organization, org_id)
    space = _owned(session, BusinessSpace, space_id, org_id)
    assignment = _owned(session, Assignment, assignment_id, org_id)
    if org is None or not org.enabled or not space.enabled:
        raise OrganizationError("organization_or_space_paused")
    if assignment.membership_id != member.membership_id or not _live(assignment, now):
        raise OrganizationError("assignment_not_authorized")
    if expected_revision is not None and assignment.revision != expected_revision:
        raise OrganizationError("stale_assignment_revision")
    position = _owned(session, Position, assignment.position_id, org_id)
    employee = _owned(session, EmployeeProfile, assignment.employee_id, org_id)
    unit = _owned(session, OrganizationUnit, position.unit_id, org_id)
    unit_path = _unit_active(session, unit, org_id)
    binding = session.scalar(select(UnitSpaceBinding.id).where(UnitSpaceBinding.unit_id == unit.unit_id, UnitSpaceBinding.space_id == space_id))
    if position.status != "active" or employee.status != "active" or binding is None or space_id not in _actions(assignment.space_ids_json):
        raise OrganizationError("position_or_scope_inactive")
    if employee.membership_id != member.membership_id:
        raise OrganizationError("employee_membership_mismatch")
    allowed = _actions(member.grants_json) & _actions(position.action_ceiling_json) & _actions(assignment.actions_json)
    values = {"schema_version": 1, "org_id": org_id, "space_id": space_id,
        "principal_id": actor.principal_id, "tenant_id": actor.tenant_id, "app_id": actor.app_id,
        "org_revision": org.revision, "policy_revision": org.policy_revision, "space_revision": space.revision,
        "unit_id": unit.unit_id, "unit_revision": unit.revision, "unit_path": unit_path, "membership_id": member.membership_id,
        "membership_revision": member.revision, "position_id": position.position_id, "position_revision": position.revision,
        "employee_id": employee.employee_id, "employee_revision": employee.revision,
        "assignment_id": assignment_id, "assignment_revision": assignment.revision,
        "allowed_actions": sorted(allowed), "budget_limit": min(org.budget_limit, position.budget_limit, assignment.budget_limit)}
    return ResolvedContext(values, actor)


def validate_context(session: Session, context: ResolvedContext, action: str, *, now: datetime | None = None) -> ResolvedContext:
    if not isinstance(context, ResolvedContext):
        raise OrganizationError("resolved_context_required")
    fresh = resolve_context(session, context.actor, org_id=context.values["org_id"], space_id=context.values["space_id"],
                            assignment_id=context.assignment_id, now=now)
    if fresh.snapshot() != context.snapshot():
        raise OrganizationError("authorization_context_changed")
    if action not in fresh.allowed_actions:
        raise OrganizationError("action_not_granted")
    return fresh


def validate_snapshot(session: Session, snapshot: dict, actor: AuthenticatedPrincipal, action: str,
                      *, now: datetime | None = None) -> ResolvedContext:
    _identity(actor)
    body = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    if snapshot.get("snapshot_hash") != _hash(body):
        raise OrganizationError("context_snapshot_hash_mismatch")
    if (body.get("principal_id"), body.get("tenant_id"), body.get("app_id")) != (actor.principal_id, actor.tenant_id, actor.app_id):
        raise OrganizationError("context_principal_mismatch")
    return validate_context(session, ResolvedContext(body, actor), action, now=now)


def transition(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, kind: str, target_id: str,
               status: str, expected_revision: int, lifecycle_effects: Callable | None = None):
    manager = _manager(session, actor, org_id)
    models = {"membership": Membership, "position": Position, "employee": EmployeeProfile, "assignment": Assignment}
    if kind not in models or status not in {"active", "paused", "revoked", "ended", "retired"}:
        raise OrganizationError("invalid_lifecycle_transition")
    row = _owned(session, models[kind], target_id, org_id)
    if row.status in {"revoked", "ended", "retired"} and status == "active":
        raise OrganizationError("terminal_assignment_requires_new_grant")
    _revise(session, models[kind], row, expected_revision, status=status)
    impacted = []
    for assignment in session.scalars(select(Assignment).where(Assignment.org_id == org_id, Assignment.status == "active")):
        matches = (kind == "membership" and assignment.membership_id == target_id) or (kind == "position" and assignment.position_id == target_id) or (kind == "employee" and assignment.employee_id == target_id) or (kind == "assignment" and assignment.assignment_id == target_id)
        if matches:
            impacted.append(assignment.assignment_id)
            if kind != "assignment" and status in {"revoked", "ended", "retired"}:
                _revise(session, Assignment, assignment, assignment.revision, status="ended")
            elif kind != "assignment" and status == "paused" and _time(assignment.valid_from) > _time(None):
                # 暂停即撤销未生效委派：尚未生效的指派立即终止，不给它等到生效的机会
                _revise(session, Assignment, assignment, assignment.revision, status="ended")
    if kind == "assignment" and target_id not in impacted:
        impacted.append(target_id)
    event = _event(session, org_id, manager.membership_id, f"{kind}.{status}", target_id,
                   {"assignment_ids": impacted, "revision": row.revision}, pending=status != "active")
    if lifecycle_effects is not None and event.effects_status == "pending":
        lifecycle_effects(session, event)
        event.effects_status = "applied"
        session.flush()
    return row, event


def position_state(session: Session, *, org_id: str, position_id: str, now: datetime | None = None) -> str:
    row = _owned(session, Position, position_id, org_id)
    if row.status != "active":
        return row.status
    active = [item for item in session.scalars(select(Assignment).where(Assignment.position_id == position_id)) if _live(item, _time(now))]
    return "active" if active else "vacant"


def renew_membership(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, membership_id: str,
                     grants: set[str], expected_revision: int, valid_until: datetime | None = None):
    """A fresh explicit grant can restore membership, never old ended assignments."""
    manager = _manager(session, actor, org_id)
    member = _owned(session, Membership, membership_id, org_id)
    _subset(grants, _actions(manager.delegable_actions_json))
    if valid_until is not None and _time(valid_until) <= _time(None):
        raise OrganizationError("invalid_membership_validity")
    _revise(session, Membership, member, expected_revision, status="active", grants_json=_json(sorted(grants)),
            delegable_actions_json="[]", valid_from=_time(None), valid_until=_time(valid_until) if valid_until else None)
    _event(session, org_id, manager.membership_id, "membership.regranted", membership_id, {"revision": member.revision})
    return member


def set_enabled(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, kind: str, target_id: str,
                enabled: bool, expected_revision: int, lifecycle_effects: Callable | None = None):
    manager = _manager(session, actor, org_id)
    models = {"organization": Organization, "space": BusinessSpace, "unit": OrganizationUnit}
    if kind not in models:
        raise OrganizationError("invalid_lifecycle_target")
    row = _owned(session, models[kind], target_id, org_id)
    _revise(session, models[kind], row, expected_revision, enabled=enabled)
    event = _event(session, org_id, manager.membership_id, f"{kind}.enabled_changed", target_id,
                   {"enabled": enabled, "revision": row.revision}, pending=not enabled)
    if lifecycle_effects is not None and event.effects_status == "pending":
        lifecycle_effects(session, event)
        event.effects_status = "applied"
        session.flush()
    return row, event


def bind_channel(session: Session, actor: AuthenticatedPrincipal, *, org_id: str, space_id: str,
                 tenant_id: str, app_id: str, channel_id: str, thread_scope: str = "",
                 read_grant: bool = True, write_grant: bool = False) -> ChannelBinding:
    """显式绑定渠道（或话题）到业务空间；读写授权分别授予。"""
    manager = _manager(session, actor, org_id)
    _owned(session, BusinessSpace, space_id, org_id)
    if not tenant_id or not app_id or not channel_id:
        raise OrganizationError("channel_identity_required")
    existing = session.scalar(select(ChannelBinding).where(
        ChannelBinding.tenant_id == tenant_id, ChannelBinding.app_id == app_id,
        ChannelBinding.channel_id == channel_id, ChannelBinding.thread_scope == thread_scope))
    if existing is not None:
        raise OrganizationError("channel_binding_exists")
    row = ChannelBinding(org_id=org_id, space_id=space_id, tenant_id=tenant_id, app_id=app_id,
                         channel_id=channel_id, thread_scope=thread_scope,
                         read_grant=read_grant, write_grant=write_grant)
    session.add(row)
    session.flush()
    _event(session, org_id, manager.membership_id, "channel.bound", channel_id,
           {"space_id": space_id, "thread_scope": thread_scope,
            "read": read_grant, "write": write_grant})
    return row


def resolve_space(session: Session, *, tenant_id: str, app_id: str, channel_id: str,
                  thread_scope: str | None = None) -> dict:
    """把渠道事件解析到唯一业务空间；未绑定或歧义都显式失败，不猜默认空间。

    话题级精确绑定优先于渠道级绑定；同一范围内多个启用绑定指向不同空间视为歧义。
    """
    rows = [row for row in session.scalars(select(ChannelBinding).where(
        ChannelBinding.tenant_id == tenant_id, ChannelBinding.app_id == app_id,
        ChannelBinding.channel_id == channel_id, ChannelBinding.enabled.is_(True))) if row.enabled]
    if not rows:
        raise OrganizationError("channel_unbound")
    if thread_scope:
        exact = [row for row in rows if row.thread_scope == thread_scope]
        candidates = exact or [row for row in rows if row.thread_scope == ""]
    else:
        candidates = [row for row in rows if row.thread_scope == ""]
    if not candidates:
        raise OrganizationError("channel_unbound")
    spaces = {row.space_id for row in candidates}
    if len(spaces) > 1:
        raise OrganizationError("channel_binding_ambiguous")
    row = candidates[0]
    space = session.get(BusinessSpace, row.space_id)
    if space is None or not space.enabled:
        raise OrganizationError("space_paused_or_missing")
    return {"org_id": row.org_id, "space_id": row.space_id, "binding_revision": row.revision,
            "space_revision": space.revision, "read_grant": row.read_grant, "write_grant": row.write_grant}


# ── DE-38：任职生命周期、岗位转交与临时组队 ─────────────────────────────────

# 仍有执行意图、需要取消传播或在途回执对账的 Run 状态
RUN_OPEN_STATES = ("queued", "claimed", "running", "waiting_budget", "waiting_resource",
                   "waiting_external", "waiting_authorization", "waiting_acceptance",
                   "waiting_recovery", "paused")
# queued 尚无副作用，只需取消；其余状态可能已有外部事实，必须开对账单
RUN_EFFECT_STATES = RUN_OPEN_STATES[1:]

CREW_ROLES = frozenset({"accountable", "executor", "reviewer"})


def default_offboard_effects(session: Session, event: OrganizationEvent) -> dict:
    """任职暂停/离任的默认接线（lifecycle_effects 回调）。

    对事件波及的每条任职指派：
    - 取消传播：向该任务补充队列写 cancellation，执行器在安全边界停止；
    - 在途回执对账：对可能已有副作用的 Run 开 open 对账单，未知回执不自动写成功。
    停止新认领由授权上下文失效保证（transition 已终止/冻结任职，resolve_context 拒绝）。
    """
    detail = json.loads(event.detail_json or "{}")
    fenced_runs: list[str] = []
    receipt_ids: list[str] = []
    for assignment_id in detail.get("assignment_ids", []):
        assignment = session.get(Assignment, assignment_id)
        if assignment is None:
            continue
        for run in session.scalars(select(ExecutionRun).where(
                ExecutionRun.state.in_(RUN_OPEN_STATES))):
            context = json.loads(run.context_json or "{}")
            if context.get("assignment_id") != assignment_id:
                continue
            supplements.enqueue(session, run.task_id,
                                event_id=f"org-offboard:{event.event_id}:{run.id}",
                                event_kind="cancellation",
                                payload={"reason": event.action, "run_id": run.id,
                                         "assignment_id": assignment_id})
            if run.state in RUN_EFFECT_STATES:
                receipt = TenureReceipt(receipt_id=f"rct-{event.event_id}-{run.id}"[:64],
                                        org_id=event.org_id, kind="run_reconcile",
                                        target_id=run.id, assignment_id=assignment_id,
                                        task_id=run.task_id,
                                        detail_json=_json({"state": run.state,
                                                           "generation": run.generation,
                                                           "lease_owner": run.lease_owner}))
                session.add(receipt)
                receipt_ids.append(receipt.receipt_id)
            fenced_runs.append(run.id)
    event.detail_json = _json({**detail, "fenced_run_ids": fenced_runs, "receipt_ids": receipt_ids})
    return {"fenced_run_ids": fenced_runs, "receipt_ids": receipt_ids}


def open_receipts(session: Session, *, org_id: str) -> list[TenureReceipt]:
    return list(session.scalars(select(TenureReceipt).where(
        TenureReceipt.org_id == org_id, TenureReceipt.status == "open")))


def resolve_tenure_receipt(session: Session, actor: AuthenticatedPrincipal, *, org_id: str,
                           receipt_id: str, outcome: str, note: str = "",
                           expected_revision: int | None = None) -> TenureReceipt:
    """显式回执对账：reconciled 必须带证据说明；failed 保留真实失败；其余一律保持 open。"""
    manager = _manager(session, actor, org_id)
    row = _owned(session, TenureReceipt, receipt_id, org_id)
    if outcome not in {"reconciled", "failed"}:
        raise OrganizationError("receipt_outcome_required")
    if outcome == "reconciled" and not note.strip():
        raise OrganizationError("receipt_reconcile_note_required")
    if row.status != "open":
        raise OrganizationError("receipt_already_resolved")
    _revise(session, TenureReceipt, row, expected_revision, status=outcome,
            outcome_note=note.strip()[:400], resolved_at=utcnow())
    _event(session, org_id, manager.membership_id, "receipt.resolved", receipt_id, {"outcome": outcome})
    return row


def update_position_contract(session: Session, actor: AuthenticatedPrincipal, *, org_id: str,
                             position_id: str, expected_revision: int, mission: str | None = None,
                             capability_refs: list[str] | None = None,
                             action_ceiling: set[str] | None = None, budget_limit: float | None = None,
                             inputs: list | None = None, outputs: list | None = None,
                             acceptance: list | None = None) -> Position:
    """改岗位执行模型/契约：版本化更新岗位行；Task 以稳定 ID 引用岗位，目标与历史不受影响。

    授权上限只收紧不放宽到管理者可委派集合之外；在途任职的权限在 resolve_context
    按成员×岗位×指派交集即时收敛，无需级联改写。
    """
    manager = _manager(session, actor, org_id)
    row = _owned(session, Position, position_id, org_id)
    values: dict = {}
    if action_ceiling is not None:
        values["action_ceiling_json"] = _json(sorted(_subset(action_ceiling, _actions(manager.delegable_actions_json))))
    if budget_limit is not None:
        org = session.get(Organization, org_id)
        values["budget_limit"] = _budget(budget_limit, org.budget_limit)
    if mission is not None:
        values["mission"] = mission
    if capability_refs is not None:
        values["capability_refs_json"] = _json(list(capability_refs))
    if inputs is not None:
        values["inputs_json"] = _json(list(inputs))
    if outputs is not None:
        values["outputs_json"] = _json(list(outputs))
    if acceptance is not None:
        values["acceptance_json"] = _json(list(acceptance))
    if not values:
        return row
    _revise(session, Position, row, expected_revision, **values)
    _event(session, org_id, manager.membership_id, "position.contract_updated", position_id,
           {"changed": sorted(values), "revision": row.revision})
    return row


def propose_handoff(session: Session, actor: AuthenticatedPrincipal, *, org_id: str,
                    task_id: int, from_assignment_id: str, to_position_id: str,
                    requested_capability: str, acceptance_revision: int,
                    artifacts: list | None = None, unfinished: list | None = None,
                    source_space_ids: list[str] | None = None) -> PositionHandoff:
    """岗位间任务转交提案：按能力匹配合法转交，不要求机械流水线。

    - 提出者：组织管理者或现任职主体本人；
    - 目标岗位必须声明该能力，缺能力/缺空间绑定/合同版本过期均拒绝并给具体原因；
    - 同一任务同时只允许一张未决转交单，避免责任归属歧义。
    """
    now = _time(None)
    try:
        manager = _manager(session, actor, org_id)
        proposer = manager.membership_id
    except OrganizationError:
        member = _membership(session, actor, org_id, now)
        from_assignment = _owned(session, Assignment, from_assignment_id, org_id)
        if from_assignment.membership_id != member.membership_id or not _live(from_assignment, now):
            raise OrganizationError("handoff_proposer_not_authorized")
        proposer = member.membership_id
    from_assignment = _owned(session, Assignment, from_assignment_id, org_id)
    task = session.get(Task, task_id)
    if task is None or task.org_id != org_id:
        raise OrganizationError("task_not_in_organization")
    if task.workflow_kind != "delegated_delivery":
        raise OrganizationError("delegated_task_required")
    if task.status in {"done", "cancelled"}:
        raise OrganizationError("task_not_transferable")
    if acceptance_revision != task.revision:
        raise OrganizationError("handoff_stale_revision")
    from_position = _owned(session, Position, from_assignment.position_id, org_id)
    if task.assigned_position and task.assigned_position != from_position.position_id:
        raise OrganizationError("handoff_position_mismatch")
    to_position = _owned(session, Position, to_position_id, org_id)
    if to_position.status != "active":
        raise OrganizationError("position_or_scope_inactive")
    if requested_capability not in _actions(to_position.capability_refs_json):
        # 能力不匹配的转交在此拒绝，原因（缺口）进入错误码与事件
        raise OrganizationError("handoff_capability_gap")
    binding = session.scalar(select(UnitSpaceBinding.id).where(
        UnitSpaceBinding.unit_id == to_position.unit_id, UnitSpaceBinding.space_id == task.space_id))
    if binding is None:
        raise OrganizationError("handoff_space_not_bound")
    open_proposal = session.scalar(select(PositionHandoff).where(
        PositionHandoff.task_id == task_id, PositionHandoff.status == "proposed"))
    if open_proposal is not None:
        raise OrganizationError("handoff_already_proposed")
    row = PositionHandoff(handoff_id=f"ho-{task_id}-{to_position_id}-{uuid4().hex[:12]}"[:64],
                          org_id=org_id, task_id=task_id,
                          from_assignment_id=from_assignment_id, from_position_id=from_position.position_id,
                          to_position_id=to_position_id, requested_capability=requested_capability,
                          acceptance_revision=acceptance_revision,
                          artifacts_json=_json(list(artifacts or [])),
                          unfinished_json=_json(list(unfinished or [])),
                          source_space_ids_json=_json(list(source_space_ids or [])),
                          proposed_by=proposer)
    session.add(row)
    session.flush()
    _event(session, org_id, proposer, "handoff.proposed", row.handoff_id,
           {"task_id": task_id, "from_position": from_position.position_id,
            "to_position": to_position_id, "capability": requested_capability}, pending=True)
    return row


def decide_handoff(session: Session, actor: AuthenticatedPrincipal, *, org_id: str,
                   handoff_id: str, decision: str, reason: str = "",
                   expected_revision: int | None = None) -> PositionHandoff:
    """交接回执裁决：接收方核对回执后接受/拒绝；拒绝必须给具体缺口原因。

    接受即任务责任岗位切换（Task 主键、目标、合同版本、历史事件全部保留），
    并对旧岗位在该任务上的在途 Run 做取消传播与回执对账登记。
    """
    if decision not in {"accept", "reject"}:
        raise OrganizationError("invalid_handoff_decision")
    if decision == "reject" and not reason.strip():
        raise OrganizationError("handoff_rejection_reason_required")
    handoff = _owned(session, PositionHandoff, handoff_id, org_id)
    if handoff.status != "proposed":
        raise OrganizationError("handoff_not_pending")
    task = session.get(Task, handoff.task_id)
    if task is None or task.org_id != org_id:
        raise OrganizationError("task_not_in_organization")
    if task.revision != handoff.acceptance_revision:
        raise OrganizationError("handoff_stale_revision")
    to_position = _owned(session, Position, handoff.to_position_id, org_id)
    now = _time(None)
    if decision == "accept":
        # 接收方必须是目标岗位的在任承担者，且其指派范围覆盖任务空间
        member = _membership(session, actor, org_id, now)
        receiver_assignment = None
        for candidate in session.scalars(select(Assignment).where(
                Assignment.org_id == org_id, Assignment.position_id == handoff.to_position_id)):
            if (candidate.membership_id == member.membership_id and _live(candidate, now)
                    and task.space_id in _actions(candidate.space_ids_json)):
                receiver_assignment = candidate
                break
        if receiver_assignment is None:
            raise OrganizationError("handoff_receiver_not_authorized")
        _revise(session, PositionHandoff, handoff, expected_revision, status="accepted",
                decided_by=member.membership_id, decided_at=utcnow(),
                decision_reason=reason.strip()[:400])
        # 目标与历史不丢：不改 Task 主键/目标/验收/revision，只切责任岗位并追加事件
        task.assigned_position = to_position.position_id
        session.add(TaskEvent(task_id=task.id, event_type="position_handoff",
                              actor=actor.principal_id,
                              detail=f"{handoff.from_position_id}->{to_position.position_id}:{handoff.handoff_id}",
                              evidence_message_ids=_json({"artifacts": json.loads(handoff.artifacts_json),
                                                          "unfinished": json.loads(handoff.unfinished_json),
                                                          "source_space_ids": json.loads(handoff.source_space_ids_json),
                                                          "capability": handoff.requested_capability})))
        # 取消传播：旧岗位在本任务的在途 Run 补充队列取消，并开对账单
        for run in session.scalars(select(ExecutionRun).where(
                ExecutionRun.task_id == task.id, ExecutionRun.state.in_(RUN_OPEN_STATES))):
            context = json.loads(run.context_json or "{}")
            if context.get("assignment_id") != handoff.from_assignment_id:
                continue
            supplements.enqueue(session, run.task_id,
                                event_id=f"org-handoff:{handoff.handoff_id}:{run.id}",
                                event_kind="cancellation",
                                payload={"reason": "handoff_accepted", "run_id": run.id,
                                         "from_assignment_id": handoff.from_assignment_id})
            if run.state in RUN_EFFECT_STATES:
                session.add(TenureReceipt(receipt_id=f"rct-ho-{handoff.handoff_id}-{run.id}"[:64],
                                          org_id=org_id, kind="run_reconcile", target_id=run.id,
                                          assignment_id=handoff.from_assignment_id, task_id=task.id,
                                          detail_json=_json({"state": run.state, "generation": run.generation,
                                                             "lease_owner": run.lease_owner})))
    else:
        # 拒绝：接收方（任一在任主体）或组织管理者均可，必须给出具体缺口原因
        try:
            member = _membership(session, actor, org_id, now)
            decider = member.membership_id
        except OrganizationError:
            decider = _manager(session, actor, org_id).membership_id
        _revise(session, PositionHandoff, handoff, expected_revision, status="rejected",
                decided_by=decider, decided_at=utcnow(),
                decision_reason=reason.strip()[:400])
        _event(session, org_id, decider, f"handoff.{decision}", handoff.handoff_id,
               {"task_id": handoff.task_id, "reason": reason.strip()[:400],
                "status": handoff.status}, pending=True)
        return handoff
    _event(session, org_id, member.membership_id, f"handoff.{decision}", handoff.handoff_id,
           {"task_id": handoff.task_id, "reason": reason.strip()[:400],
            "status": handoff.status}, pending=False)
    return handoff


def form_task_crew(session: Session, actor: AuthenticatedPrincipal, *, org_id: str,
                   task_id: int, crew: list[dict]) -> list[TaskCrewMember]:
    """按任务临时组队：明确 accountable owner / executor / reviewer，多任职合规可用。

    - 每个成员必须是覆盖任务空间的有效任职（来源、有效期、权限版本在任职里）；
    - accountable 恰好一个，且其岗位与任务当前责任岗位一致；
    - 执行与审阅不得是同一任职（执行与审阅分离）。
    """
    manager = _manager(session, actor, org_id)
    task = session.get(Task, task_id)
    if task is None or task.org_id != org_id:
        raise OrganizationError("task_not_in_organization")
    if task.status in {"done", "cancelled"}:
        raise OrganizationError("task_not_transferable")
    if not crew:
        raise OrganizationError("crew_members_required")
    seen_pairs: set[tuple[str, str]] = set()
    accountable_position = ""
    executor_ids: set[str] = set()
    reviewer_ids: set[str] = set()
    rows: list[TaskCrewMember] = []
    for entry in crew:
        role = str(entry.get("role") or "")
        assignment_id = str(entry.get("assignment_id") or "")
        if role not in CREW_ROLES or not assignment_id:
            raise OrganizationError("invalid_crew_entry")
        if (assignment_id, role) in seen_pairs:
            raise OrganizationError("crew_member_duplicated")
        seen_pairs.add((assignment_id, role))
        assignment = _owned(session, Assignment, assignment_id, org_id)
        position = _owned(session, Position, assignment.position_id, org_id)
        if position.status != "active" or not _live(assignment, _time(None)):
            raise OrganizationError("assignment_subject_inactive")
        if task.space_id and task.space_id not in _actions(assignment.space_ids_json):
            raise OrganizationError("assignment_space_required")
        if role == "accountable":
            if accountable_position:
                raise OrganizationError("crew_accountable_required_single")
            accountable_position = position.position_id
        elif role == "executor":
            executor_ids.add(assignment_id)
        else:
            reviewer_ids.add(assignment_id)
    if not accountable_position:
        raise OrganizationError("crew_accountable_required_single")
    if executor_ids & reviewer_ids:
        raise OrganizationError("crew_review_separation_required")
    if task.assigned_position and task.assigned_position != accountable_position:
        raise OrganizationError("crew_accountable_position_mismatch")
    for entry in crew:
        rows.append(TaskCrewMember(org_id=org_id, task_id=task_id,
                                   assignment_id=str(entry["assignment_id"]),
                                   role=str(entry["role"])))
        session.add(rows[-1])
    session.flush()
    _event(session, org_id, manager.membership_id, "crew.formed", str(task_id),
           {"members": [{"assignment_id": r.assignment_id, "role": r.role} for r in rows]})
    return rows


def disband_task_crew(session: Session, actor: AuthenticatedPrincipal, *, org_id: str,
                      task_id: int) -> list[TaskCrewMember]:
    """临时组队解散：成员行标 ended 保留历史；任务与产物引用稳定 ID 不受影响。"""
    manager = _manager(session, actor, org_id)
    rows = list(session.scalars(select(TaskCrewMember).where(
        TaskCrewMember.org_id == org_id, TaskCrewMember.task_id == task_id,
        TaskCrewMember.status == "active")))
    for row in rows:
        _revise(session, TaskCrewMember, row, row.revision, status="ended")
    if rows:
        _event(session, org_id, manager.membership_id, "crew.disbanded", str(task_id),
               {"members": len(rows)})
    return rows
