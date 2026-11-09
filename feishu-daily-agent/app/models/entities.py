"""数据实体定义。

领域事实（消息、任务、经验、日报、发送意图）与审计事实（操作、回执、模型调用）
分开存放；所有列表状态使用字符串枚举，便于迁移与人工排查。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.clock import utcnow


class Base(DeclarativeBase):
    pass


# ---- 枚举（以字符串存储，便于迁移与排查）----
class Category:
    QUESTION = "question"   # 别人向我提出的问题
    TASK = "task"           # 需要我完成的事项
    NOTICE = "notice"       # 通知/知会，无需动作
    UNKNOWN = "unknown"


class ExperienceStatus:
    CANDIDATE = "candidate"         # 新沉淀，仅可辅助拟稿
    ASSIST = "assist"               # 可辅助拟稿（显式允许）
    APPROVED_AUTO = "approved_auto" # 已批准用于受限自动回复
    PAUSED = "paused"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    # 兼容原型命名
    DRAFT = CANDIDATE
    VERIFIED = APPROVED_AUTO
    DISABLED = PAUSED


class DraftMode:
    AUTO = "auto"
    DRAFT = "draft"
    SHADOW = "shadow"


class DraftStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENDING = "sending"
    SENT = "sent"
    EDITED = "edited"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


class TaskStatus:
    NEEDS_CONFIRMATION = "needs_confirmation"
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class OutboxStatus:
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    RETRY_WAIT = "retry_wait"
    DELIVERY_UNKNOWN = "delivery_unknown"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CoverageStatus:
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class Chat(Base):
    __tablename__ = "chats"

    chat_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    chat_type: Mapped[str] = mapped_column(String(16), default="p2p")
    name: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_time: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class Source(Base):
    """授权来源：读取许可与回复许可各自独立。"""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("tenant", "chat_id", "thread_id", name="uq_source_chat_thread"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    owner: Mapped[str] = mapped_column(String(64), default="", index=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    thread_id: Mapped[str] = mapped_column(String(64), default="")
    source_type: Mapped[str] = mapped_column(String(16), default="p2p")
    discovery: Mapped[str] = mapped_column(String(32), default="bot_list")
    read_identity: Mapped[str] = mapped_column(String(32), default="bot")
    reply_identity: Mapped[str] = mapped_column(String(32), default="bot")
    reply_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_content_types: Mapped[str] = mapped_column(String(255), default="text,post")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(16), default=CoverageStatus.PARTIAL)
    last_success_watermark_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    @property
    def allowed_type_set(self) -> set[str]:
        return {item.strip() for item in (self.allowed_content_types or "").split(",") if item.strip()}


class SourceSync(Base):
    __tablename__ = "source_sync"
    __table_args__ = (
        UniqueConstraint("source_id", "window_start_ms", "window_end_ms", name="uq_sync_window"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("sources.id"), index=True)
    window_start_ms: Mapped[int] = mapped_column(BigInteger)
    window_end_ms: Mapped[int] = mapped_column(BigInteger)
    cursor: Mapped[str] = mapped_column(Text, default="")
    pages_saved: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=CoverageStatus.PARTIAL)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class InboxEvent(Base):
    __tablename__ = "inbox_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="")
    message_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
    processed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("tenant", "message_id", name="uq_message_tenant_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    source_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    thread_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    sender_id: Mapped[str] = mapped_column(String(64), default="")
    sender_name: Mapped[str] = mapped_column(String(255), default="")
    msg_type: Mapped[str] = mapped_column(String(32), default="text")
    content_text: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    create_time: Mapped[int] = mapped_column(BigInteger, index=True)  # 飞书毫秒时间戳
    event_time_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    is_from_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_mention: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str] = mapped_column(String(16), default=Category.UNKNOWN)
    summary: Mapped[str] = mapped_column(String(255), default="")
    due: Mapped[str] = mapped_column(String(64), default="")
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    recalled: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_json: Mapped[str] = mapped_column(Text, default="")
    raw_retention_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    drafts: Mapped[List["ReplyDraft"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("tenant", "task_key", name="uq_task_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_key: Mapped[str] = mapped_column(String(128), index=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    owner: Mapped[str] = mapped_column(String(64), default="", index=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    thread_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    title: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, default="")
    assignee_id: Mapped[str] = mapped_column(String(64), default="")
    assignee_name: Mapped[str] = mapped_column(String(255), default="")
    due_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    due_text: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(24), default=TaskStatus.NEEDS_CONFIRMATION, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, default=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    source_message_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    events: Mapped[List["TaskEvent"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    def evidence(self) -> list[dict]:
        try:
            return json.loads(self.evidence_json or "[]")
        except ValueError:
            return []

    def source_ids(self) -> list[str]:
        try:
            return json.loads(self.source_message_ids or "[]")
        except ValueError:
            return []


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    evidence_message_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    task: Mapped["Task"] = relationship(back_populates="events")


class Experience(Base):
    __tablename__ = "experiences"
    __table_args__ = (
        UniqueConstraint("tenant", "intent_id", "version", name="uq_experience_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    owner: Mapped[str] = mapped_column(String(64), default="", index=True)
    intent_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    question_pattern: Mapped[str] = mapped_column(Text)
    canonical_answer: Mapped[str] = mapped_column(Text)
    patterns_json: Mapped[str] = mapped_column(Text, default="[]")
    negative_examples_json: Mapped[str] = mapped_column(Text, default="[]")
    audience_json: Mapped[str] = mapped_column(Text, default="{}")
    preconditions: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[str] = mapped_column(Text, default="")
    allowed_links: Mapped[str] = mapped_column(Text, default="")
    source_ids: Mapped[str] = mapped_column(Text, default="[]")
    sensitive_level: Mapped[str] = mapped_column(String(16), default="private")
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    valid_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=ExperienceStatus.CANDIDATE, index=True)
    category: Mapped[str] = mapped_column(String(16), default="faq", index=True)
    previous_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    keywords: Mapped[str] = mapped_column(Text, default="")
    embedding_text: Mapped[str] = mapped_column(Text, default="")
    embedding_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    reject_count: Mapped[int] = mapped_column(Integer, default=0)
    auto_reply_count: Mapped[int] = mapped_column(Integer, default=0)
    source_message_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    def set_embedding(self, vector: List[float]) -> None:
        self.embedding_json = json.dumps(vector, ensure_ascii=False)

    def get_embedding(self) -> Optional[List[float]]:
        if not self.embedding_json:
            return None
        return json.loads(self.embedding_json)

    def patterns(self) -> list[str]:
        try:
            return list(json.loads(self.patterns_json or "[]"))
        except ValueError:
            return []

    def negative_examples(self) -> list[str]:
        try:
            return list(json.loads(self.negative_examples_json or "[]"))
        except ValueError:
            return []

    def audience(self) -> dict:
        try:
            return dict(json.loads(self.audience_json or "{}"))
        except ValueError:
            return {}

    def source_message_ids(self) -> list[str]:
        try:
            return list(json.loads(self.source_ids or "[]"))
        except ValueError:
            return []


class ExperienceHead(Base):
    __tablename__ = "experience_heads"

    intent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    experience_id: Mapped[int] = mapped_column(Integer, ForeignKey("experiences.id"))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ExperienceEvent(Base):
    __tablename__ = "experience_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experience_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ReplyDecision(Base):
    __tablename__ = "reply_decisions"
    __table_args__ = (
        UniqueConstraint("tenant", "message_id", name="uq_decision_message"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    message_id: Mapped[str] = mapped_column(String(64), index=True)
    decision: Mapped[str] = mapped_column(String(24))
    reason_codes: Mapped[str] = mapped_column(Text, default="[]")
    experience_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    similarity: Mapped[float] = mapped_column(Float, default=0.0)
    rule_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_version: Mapped[str] = mapped_column(String(64), default="")
    model_version: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    data_snapshot_id: Mapped[str] = mapped_column(String(64), default="")
    shadow: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    def reasons(self) -> list[str]:
        try:
            return list(json.loads(self.reason_codes or "[]"))
        except ValueError:
            return []


class ReplyDraft(Base):
    __tablename__ = "reply_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    message_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("messages.message_id"), index=True
    )
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    experience_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("experiences.id"), nullable=True
    )
    draft_content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    mode: Mapped[str] = mapped_column(String(16), default=DraftMode.DRAFT)
    status: Mapped[str] = mapped_column(
        String(16), default=DraftStatus.PENDING, index=True
    )
    shadow: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_message_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    approved_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    superseded_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    message: Mapped["Message"] = relationship(back_populates="drafts")


class ReviewEvent(Base):
    __tablename__ = "review_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    draft_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    action_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="draft")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    target_version: Mapped[int] = mapped_column(Integer, default=0)
    action: Mapped[str] = mapped_column(String(32))
    operator_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ActionToken(Base):
    """卡片按钮的一次性动作令牌；卡片 value 只携带令牌，不携带可伪造的业务字段。"""

    __tablename__ = "action_tokens"

    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    card_id: Mapped[str] = mapped_column(String(64), default="")
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(64))
    target_version: Mapped[int] = mapped_column(Integer, default=0)
    action: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Outbox(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    kind: Mapped[str] = mapped_column(String(32))
    chat_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="message")
    target_id: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    policy_version: Mapped[str] = mapped_column(String(64), default="")
    experience_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="")
    auto: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default=OutboxStatus.QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    claim_token: Mapped[str] = mapped_column(String(64), default="")
    lease_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    receipt_message_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    logical_date: Mapped[str] = mapped_column(String(10))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    window_start_ms: Mapped[int] = mapped_column(BigInteger)
    window_end_ms: Mapped[int] = mapped_column(BigInteger)
    content_json: Mapped[str] = mapped_column(Text, default="{}")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    delivery_intent_key: Mapped[str] = mapped_column(String(128), default="")
    delivered_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ModelLedger(Base):
    __tablename__ = "model_ledger"
    __table_args__ = (
        Index("ix_model_ledger_day_tenant", "budget_day", "tenant"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default")
    budget_day: Mapped[str] = mapped_column(String(10))
    purpose: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    output_hash: Mapped[str] = mapped_column(String(64), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


# 兼容原型命名
ReviewLog = ReviewEvent
