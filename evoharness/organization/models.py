"""Organization entities share the existing EvoHarness Base and business database."""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from evoharness.models import Base
from evoharness.clock import utcnow


class Organization(Base):
    __tablename__ = "organizations"
    org_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    owner_principal_id: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    policy_revision: Mapped[int] = mapped_column(Integer, default=1)
    budget_limit: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BusinessSpace(Base):
    __tablename__ = "business_spaces"
    space_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    owner_principal_id: Mapped[str] = mapped_column(String(128), default="")
    knowledge_namespace: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class OrganizationUnit(Base):
    __tablename__ = "organization_units"
    unit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    parent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organization_units.unit_id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class UnitSpaceBinding(Base):
    __tablename__ = "unit_space_bindings"
    __table_args__ = (UniqueConstraint("unit_id", "space_id", name="uq_unit_space"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("organization_units.unit_id"), index=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("business_spaces.space_id"), index=True)


class Position(Base):
    __tablename__ = "organization_positions"
    position_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("organization_units.unit_id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    mission: Mapped[str] = mapped_column(Text, default="")
    inputs_json: Mapped[str] = mapped_column(Text, default="[]")
    outputs_json: Mapped[str] = mapped_column(Text, default="[]")
    acceptance_json: Mapped[str] = mapped_column(Text, default="[]")
    capability_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    action_ceiling_json: Mapped[str] = mapped_column(Text, default="[]")
    budget_limit: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(24), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Membership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (UniqueConstraint("org_id", "tenant_id", "app_id", "principal_id", name="uq_scoped_membership"),)
    membership_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    principal_id: Mapped[str] = mapped_column(String(128), index=True)
    principal_kind: Mapped[str] = mapped_column(String(24), default="human")
    tenant_id: Mapped[str] = mapped_column(String(64), default="")
    app_id: Mapped[str] = mapped_column(String(64), default="")
    roles_json: Mapped[str] = mapped_column(Text, default="[]")
    grants_json: Mapped[str] = mapped_column(Text, default="[]")
    delegable_actions_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(24), default="active")
    valid_from: Mapped[datetime] = mapped_column(default=utcnow)
    valid_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class EmployeeProfile(Base):
    __tablename__ = "employee_profiles"
    employee_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    membership_id: Mapped[str] = mapped_column(ForeignKey("organization_memberships.membership_id"), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    runtime_binding: Mapped[str] = mapped_column(String(255), default="")
    instructions_version: Mapped[str] = mapped_column(String(64), default="1")
    status: Mapped[str] = mapped_column(String(24), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Assignment(Base):
    __tablename__ = "position_assignments"
    assignment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    position_id: Mapped[str] = mapped_column(ForeignKey("organization_positions.position_id"), index=True)
    membership_id: Mapped[str] = mapped_column(ForeignKey("organization_memberships.membership_id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee_profiles.employee_id"), index=True)
    granted_by: Mapped[str] = mapped_column(String(64))
    actions_json: Mapped[str] = mapped_column(Text, default="[]")
    space_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    budget_limit: Mapped[float] = mapped_column(Float, default=0)
    valid_from: Mapped[datetime] = mapped_column(default=utcnow)
    valid_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class OrganizationEvent(Base):
    __tablename__ = "organization_events"
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(64))
    actor_membership_id: Mapped[str] = mapped_column(String(64))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    effects_status: Mapped[str] = mapped_column(String(24), default="not_required")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PositionHandoff(Base):
    """岗位间任务转交单（DE-38）：带交接回执语义，能力匹配才可提出，接收方核对后接受/拒绝。

    Task 以稳定 ID（tasks.id / assigned_position）引用，目标与历史不因转交重建。
    """
    __tablename__ = "position_handoffs"
    handoff_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    from_assignment_id: Mapped[str] = mapped_column(ForeignKey("position_assignments.assignment_id"), index=True)
    from_position_id: Mapped[str] = mapped_column(String(64))
    to_position_id: Mapped[str] = mapped_column(ForeignKey("organization_positions.position_id"), index=True)
    requested_capability: Mapped[str] = mapped_column(String(128), default="")
    acceptance_revision: Mapped[int] = mapped_column(Integer, default=1)
    artifacts_json: Mapped[str] = mapped_column(Text, default="[]")
    unfinished_json: Mapped[str] = mapped_column(Text, default="[]")
    source_space_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    proposed_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class TaskCrewMember(Base):
    """按任务临时组队（DE-38）：每项工作明确 accountable owner、执行岗位与必要审阅者。

    多任职/跨岗位成员均可入队；执行与审阅不得是同一任职（执行与审阅分离）。
    """
    __tablename__ = "task_crew_members"
    __table_args__ = (UniqueConstraint("org_id", "task_id", "assignment_id", "role", name="uq_task_crew_role"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("position_assignments.assignment_id"), index=True)
    role: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class TenureReceipt(Base):
    """离任/暂停的在途回执对账单（DE-38）：未知回执保持 open，不自动写成功。"""
    __tablename__ = "tenure_receipts"
    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    kind: Mapped[str] = mapped_column(String(24), default="run_reconcile")
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    assignment_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[int] = mapped_column(Integer, default=0)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="open")
    outcome_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ChannelBinding(Base):
    """渠道到业务空间的显式绑定；一个渠道可涉及多个分身/员工，歧义不得猜测。"""
    __tablename__ = "channel_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "app_id", "channel_id", "thread_scope", name="uq_channel_scope"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("business_spaces.space_id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    app_id: Mapped[str] = mapped_column(String(64))
    channel_id: Mapped[str] = mapped_column(String(64))
    # 空串代表整个渠道；非空代表单个话题/线程的更细粒度绑定
    thread_scope: Mapped[str] = mapped_column(String(64), default="")
    read_grant: Mapped[bool] = mapped_column(Boolean, default=True)
    write_grant: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
