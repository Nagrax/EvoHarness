"""统一权限检索与 provenance：不建 Wiki，保留冲突与来源（DE-16）。

- 冲突保留：同 subject+predicate 的不同 value 并存为 conflicting，不自动删任何一方
- 源变更/撤权失效：标 stale 保留正文与历史，不物理删除
- 检索按空间与 owner_scope 裁剪：未知权限不默认公开
"""
from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.knowledge.models import KnowledgeClaim


class KnowledgeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def record_claim(session: Session, *, org_id: str = "", space_id: str = "",
                 owner_scope: str = "private", subject: str, predicate: str = "",
                 value: str, source_refs: list[str] | None = None,
                 source_revision: int = 1, valid_until: datetime | None = None,
                 confidence_basis: str = "", supersedes: str = "") -> KnowledgeClaim:
    """登记主张；来源引用必须显式（不捏造权威）。"""
    if not subject.strip():
        raise KnowledgeError("claim_subject_required")
    if not source_refs:
        raise KnowledgeError("claim_source_required")
    if supersedes:
        previous = session.get(KnowledgeClaim, supersedes)
        if previous is None:
            raise KnowledgeError("superseded_claim_not_found")
        if previous.subject != subject or previous.predicate != predicate:
            raise KnowledgeError("supersede_scope_mismatch")
        previous.state = "superseded"
    row = KnowledgeClaim(claim_id=uuid4().hex, org_id=org_id, space_id=space_id,
                         owner_scope=owner_scope, subject=subject.strip(),
                         predicate=predicate, value=value,
                         source_refs_json=json.dumps(source_refs, ensure_ascii=False),
                         source_revision=source_revision, valid_until=valid_until,
                         confidence_basis=confidence_basis, supersedes=supersedes)
    session.add(row)
    session.flush()
    # 同主体同谓词的活跃主张 value 不同 → 两者标 conflicting（保留各自来源）
    peers = [c for c in session.scalars(select(KnowledgeClaim).where(
        KnowledgeClaim.space_id == space_id, KnowledgeClaim.subject == subject.strip(),
        KnowledgeClaim.predicate == predicate, KnowledgeClaim.state == "active"))
        if c.claim_id != row.claim_id]
    if any(c.value != value for c in peers):
        for peer in peers:
            peer.state = "conflicting"
        row.state = "conflicting"
    session.flush()
    return row


def _visible(row: KnowledgeClaim, *, owner_scope: str, include_private: bool) -> bool:
    if row.owner_scope in {"", "public"}:
        return True
    return include_private and row.owner_scope == owner_scope


def _visible_to_viewer(row: KnowledgeClaim, *, viewer_scopes: frozenset[str]) -> bool:
    """受信主体可见性（R01）：私有主张只对被授权的主体 scope 可见。

    可见集从受信 Principal 派生；调用方传入的 owner_scope/include_private
    查询参数不参与判定（不能作为越权读取的依据）。
    """
    if row.owner_scope in {"", "public"}:
        return True
    return row.owner_scope in viewer_scopes


def search(session: Session, *, space_id: str, subject: str,
           owner_scope: str = "", include_private: bool = False,
           now: datetime | None = None,
           viewer_scopes: "frozenset[str] | set[str] | list[str] | None" = None) -> dict:
    """权限裁剪后的检索结果：活跃主张 + 冲突 + provenance，不能只回一条"答案"。

    viewer_scopes 非空（受信主体模式，R01）：可见性只从受信主体 scope 集派生，
    owner_scope/include_private 参数被忽略，不再作为可见性依据。
    """
    now = now or utcnow()
    if viewer_scopes is not None:
        scopes = frozenset(str(s) for s in viewer_scopes if str(s))
        rows = [row for row in session.scalars(select(KnowledgeClaim).where(
            KnowledgeClaim.space_id == space_id,
            KnowledgeClaim.subject == subject.strip()))
            if _visible_to_viewer(row, viewer_scopes=scopes)]
    else:
        rows = [row for row in session.scalars(select(KnowledgeClaim).where(
            KnowledgeClaim.space_id == space_id,
            KnowledgeClaim.subject == subject.strip()))
            if _visible(row, owner_scope=owner_scope, include_private=include_private)]
    live = [row for row in rows
            if row.state in {"active", "conflicting"}
            and (row.valid_until is None or now <= row.valid_until)]
    expired = [row for row in rows if row.valid_until is not None and now > row.valid_until]
    return {
        "claims": [{
            "claim_id": row.claim_id, "subject": row.subject, "predicate": row.predicate,
            "value": row.value, "state": row.state,
            "source_refs": json.loads(row.source_refs_json),
            "source_revision": row.source_revision,
            "supersedes": row.supersedes,
            "confidence_basis": row.confidence_basis,
        } for row in live],
        # 冲突保留显示：新旧/两源规则并存，由上层决定澄清，不自动裁决
        "conflicts": [row.claim_id for row in live if row.state == "conflicting"],
        "expired_hint": [row.claim_id for row in expired],
    }


def invalidate_for_source(session: Session, *, source_ref: str, reason: str = "") -> list[str]:
    """源变更/撤权：受影响主张标 stale（正文与历史保留），不物理删除。"""
    if not source_ref:
        raise KnowledgeError("source_ref_required")
    invalidated: list[str] = []
    for row in session.scalars(select(KnowledgeClaim).where(
            KnowledgeClaim.state.in_(["active", "conflicting"]))):
        refs = json.loads(row.source_refs_json)
        if source_ref in refs:
            row.state = "stale"
            row.confidence_basis = (row.confidence_basis + f" | stale:{reason}").strip(" |")
            invalidated.append(row.claim_id)
    session.flush()
    return invalidated
