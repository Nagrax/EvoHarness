"""Execution and evidence reference the original Task and shared metadata."""
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from evoharness.models import Base
from evoharness.clock import utcnow
from datetime import datetime


class ExecutionRun(Base):
    __tablename__ = "execution_runs"
    __table_args__ = (UniqueConstraint("tenant", "idempotency_key", name="uq_run_intent"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    tenant: Mapped[str] = mapped_column(String(64))
    task_revision: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    actor_id: Mapped[str] = mapped_column(String(64))
    context_json: Mapped[str] = mapped_column(Text)
    context_hash: Mapped[str] = mapped_column(String(64))
    output_root: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str] = mapped_column(String(64), default="")
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SkillLearningEvidence(Base):
    """Task-owned input/output of the original online learning lifecycle."""
    __tablename__ = "task_skill_learning"
    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    task_revision: Mapped[int] = mapped_column(Integer)
    reviewer: Mapped[str] = mapped_column(String(128))
    verdict: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str] = mapped_column(Text, default="")
    skills_json: Mapped[str] = mapped_column(Text, default="[]")
    context_sha256: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(24), default="pending")
    claim_token: Mapped[str] = mapped_column(String(64), default="")
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Artifact(Base):
    __tablename__ = "work_artifacts"
    __table_args__ = (UniqueConstraint("run_id", "generation", "path", name="uq_run_artifact"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    task_revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    scope_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ArtifactReceipt(Base):
    """R07：测试报告类产物的真实检查回执（checker id + 时间 + 结果摘要 hash）。

    回执在登记时与产物内容摘要绑定（artifact_sha256）：报告被改写或回执被
    移植到其他产物时，验收复核即判定 receipt_unbound；无回执的纯文本报告
    是否作为通过依据由 EVOHARNESS_ACCEPTANCE_RECEIPTS 策略控制。
    """
    __tablename__ = "work_artifact_receipts"
    __table_args__ = (UniqueConstraint("artifact_id", name="uq_artifact_receipt"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("work_artifacts.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    checker: Mapped[str] = mapped_column(String(128))
    checked_at: Mapped[str] = mapped_column(String(64))
    summary_sha256: Mapped[str] = mapped_column(String(64))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    # DE-13：检查执行事实（生产强制口径）。legacy 回执可为空/0，不伪造统计。
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    executions: Mapped[int] = mapped_column(Integer, default=0)
    ran: Mapped[int] = mapped_column(Integer, default=0)
    bundle_sha256: Mapped[str] = mapped_column(String(64), default="")
    node_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    # DE-13：测试集差集事实（检查前基线 / 检查后现状 / 被删 node）。
    baseline_tests: Mapped[int] = mapped_column(Integer, default=0)
    current_tests: Mapped[int] = mapped_column(Integer, default=0)
    removed_node_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    modified_node_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TaskSupplement(Base):
    """在途追加的持久有序队列：同一 source 事件只入队一次，取消优先。"""
    __tablename__ = "task_supplements"
    __table_args__ = (UniqueConstraint("task_id", "event_id", name="uq_supplement_event"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(128))
    event_kind: Mapped[str] = mapped_column(String(16))   # note / cancellation
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
