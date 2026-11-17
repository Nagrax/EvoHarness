"""版本化知识主张：来源可追、冲突保留、supersede 链与有效期。"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from evoharness.models import Base
from evoharness.clock import utcnow


class KnowledgeClaim(Base):
    __tablename__ = "knowledge_claims"
    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    space_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    owner_scope: Mapped[str] = mapped_column(String(128), default="", index=True)
    subject: Mapped[str] = mapped_column(String(255), index=True)
    predicate: Mapped[str] = mapped_column(String(128), default="")
    value: Mapped[str] = mapped_column(Text, default="")
    # 来源引用：commit / config指纹 / message@revision / 查询时间戳，不捏造
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    source_revision: Mapped[str] = mapped_column(Integer, default=1)
    valid_from: Mapped[datetime] = mapped_column(default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(nullable=True)
    confidence_basis: Mapped[str] = mapped_column(String(255), default="")
    # active / superseded / stale（源变更失效）/ conflicting（冲突保留显示）
    state: Mapped[str] = mapped_column(String(16), default="active", index=True)
    supersedes: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
