"""技能作用域撤权事件（DE-06）：显式记录 revoked/restored，搜索入口据此拒绝。

- 事件按 (skill_name, tenant, space, owner) 取最新一条裁决；
- revoked 且 ``source_revision >= 请求基线`` → 拒绝（同版本撤权也生效）；
- restored 之后恢复可搜；事件保留历史，不物理删除。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, Session

from evoharness.clock import utcnow
from evoharness.models import Base

ACTIONS = frozenset({"revoked", "restored"})


class SkillScopeEvent(Base):
    __tablename__ = "skill_scope_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_name: Mapped[str] = mapped_column(String(128), index=True)
    tenant: Mapped[str] = mapped_column(String(64), default="", index=True)
    space: Mapped[str] = mapped_column(String(64), default="", index=True)
    owner: Mapped[str] = mapped_column(String(64), default="")
    source_revision: Mapped[int] = mapped_column(Integer, default=0)
    action: Mapped[str] = mapped_column(String(16), default="revoked")
    actor: Mapped[str] = mapped_column(String(128), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SkillScopeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def record_scope_event(session: Session, *, skill_name: str, tenant: str,
                       space: str, action: str, owner: str = "",
                       source_revision: int = 0, actor: str = "",
                       reason: str = "") -> SkillScopeEvent:
    if not str(skill_name or "").strip():
        raise SkillScopeError("skill_name_required")
    if action not in ACTIONS:
        raise SkillScopeError("invalid_scope_action")
    row = SkillScopeEvent(skill_name=str(skill_name).strip(),
                          tenant=str(tenant or "").strip(),
                          space=str(space or "").strip(),
                          owner=str(owner or "").strip(),
                          source_revision=max(0, int(source_revision or 0)),
                          action=action, actor=str(actor or ""),
                          reason=str(reason or ""))
    session.add(row)
    session.flush()
    return row


def latest_scope_event(session: Session, *, skill_name: str, tenant: str,
                       space: str, owner: str = "") -> SkillScopeEvent | None:
    query = select(SkillScopeEvent).where(
        SkillScopeEvent.skill_name == str(skill_name or "").strip(),
        SkillScopeEvent.tenant == str(tenant or "").strip(),
        SkillScopeEvent.space == str(space or "").strip())
    if owner:
        query = query.where(SkillScopeEvent.owner == str(owner or "").strip())
    return session.scalar(query.order_by(SkillScopeEvent.id.desc()))


def scope_denial(session: Session, *, skill_name: str, tenant: str, space: str,
                 owner: str = "", revision: int = 0) -> dict | None:
    row = latest_scope_event(session, skill_name=skill_name, tenant=tenant,
                             space=space, owner=owner)
    if row is None or row.action != "revoked":
        return None
    if int(row.source_revision) < int(revision or 0):
        return None
    return {"error": "skill_scope_revoked", "skill": row.skill_name,
            "tenant": row.tenant, "space": row.space,
            "source_revision": int(row.source_revision),
            "reason": row.reason or ""}


def make_scope_guard(session_factory, *, on_denial=None):
    """搜索入口守卫：按 manifest 的 skill/tenant/space 查最新撤权裁决。

    ``on_denial`` 供入口在拒绝时清理技能/usage 缓存（回调由装配层注入，
    evoharness 不反向依赖 agents）。
    """
    def guard(manifest) -> dict | None:
        with session_factory() as session:
            denial = scope_denial(session,
                                  skill_name=str(getattr(manifest, "skill_name", "")),
                                  tenant=str(getattr(manifest, "tenant", "")),
                                  space=str(getattr(manifest, "space", "")))
        if denial and callable(on_denial):
            try:
                on_denial(denial)
            except Exception:  # noqa: BLE001 — 清理失败不改变拒绝结论
                pass
        return denial
    return guard
