"""批准只绑定具体版本、载荷与动作；撤回与替代显式记录；预算预留与限额。"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from evoharness.models import Base
from evoharness.clock import utcnow


class Decision(Base):
    __tablename__ = "governance_decisions"
    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str] = mapped_column(String(128))
    grantee_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    target_revision: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))          # approve / reject
    actions_json: Mapped[str] = mapped_column(Text, default="[]")
    allow_bot_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BudgetLimit(Base):
    """按 scope 层级配置的 token 限额；未配置的生产空间不自动启动付费任务。"""
    __tablename__ = "budget_limits"
    __table_args__ = (UniqueConstraint("scope_kind", "scope_id", name="uq_budget_scope"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(16))        # org / space / principal
    scope_id: Mapped[str] = mapped_column(String(64))
    limit_tokens: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class BudgetReservation(Base):
    """调用前原子预留，结束后按真实 usage 结算；失败/重试/取消同样归集。

    N02/N04 动作意图扩展（同一张账本表，不另建技术台账）：
    - kind：envelope（Run 级托管额度）| call（单次调用/动作意图）；
    - call_id：稳定调用标识——实际 usage 以唯一 call_id 记一次；
    - intent_status：committed（意图已提交）→ executing（已退出写事务、
      外部调用开始）→ recorded（回执已写）/ unknown（结果未知，保守归集）/
      cancelled（未执行即释放）；
    - usage_status：recorded（真实用量）/ zero（真实零用量）/ unknown
      （结果未知，按预留保守归集）——真实零与未知不用同一个值表示；
    - escrow_tokens：本调用从父 envelope 划入的托管份额（结算时按
      used_tokens 互斥回补，envelope 与调用不叠加占额）；
    - generation/grant_revision/request_hash/provider_request_id/receipt_ref/
      checkpoint_ref：回执对账绑定（哪一代执行、哪份授权、什么请求、
      哪份回执/检查点）。
    """
    __tablename__ = "budget_reservations"
    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    space_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    principal_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    work_item_id: Mapped[str] = mapped_column(Integer, default=0)
    run_id: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(32))          # chat/embedding/judge/reflection/extraction/dreaming
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="reserved", index=True)  # reserved/settled/released
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="call")            # envelope / call
    call_id: Mapped[str] = mapped_column(String(64), default="")
    generation: Mapped[int] = mapped_column(Integer, default=0)
    grant_revision: Mapped[int] = mapped_column(Integer, default=0)
    request_hash: Mapped[str] = mapped_column(String(64), default="")
    intent_status: Mapped[str] = mapped_column(String(16), default="")       # committed/executing/recorded/unknown/cancelled
    outcome: Mapped[str] = mapped_column(String(16), default="")             # ok/failed/cancelled/unknown
    usage_status: Mapped[str] = mapped_column(String(16), default="")        # recorded/zero/unknown/not_applicable
    escrow_tokens: Mapped[int] = mapped_column(Integer, default=0)
    provider_request_id: Mapped[str] = mapped_column(String(128), default="")
    receipt_ref: Mapped[str] = mapped_column(String(128), default="")
    checkpoint_ref: Mapped[str] = mapped_column(String(128), default="")
