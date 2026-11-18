"""Experience（owning source）→ KnowledgeClaim provenance 桥（DE-16/BP-03）。

- 只读 Experience 当前事实，登记带来源引用的主张；**不复制/改写源**，
  Experience 仍是唯一写者，检索/召回仍走原链路。
- 来源引用：``experience:<intent>@<version>``、``message:<id>``、``source:<id>``。
- 幂等：内容键写入 ``confidence_basis``（``experience-sync:<key>``），重复同步
  不新增主张。
- 新版本：旧版本主张按同 intent 的旧 ref 标 stale，新主张 supersede 旧主张
  （历史与来源保留）。
- 停用/过期/撤权：同 intent 的所有版本主张标 stale，不物理删除。
"""
from __future__ import annotations

import json

from sqlalchemy import select

from evoharness.clock import utcnow
from evoharness.ids import sha256_json
from evoharness.knowledge import service as knowledge_service
from evoharness.knowledge.models import KnowledgeClaim
from evoharness.models import Experience, ExperienceStatus

AVAILABLE_STATUSES = {ExperienceStatus.CANDIDATE, ExperienceStatus.ASSIST,
                      ExperienceStatus.APPROVED_AUTO}
PREDICATE = "canonical_answer"


def experience_ref(row: Experience) -> str:
    return f"experience:{row.intent_id}@{int(row.version or 1)}"


def _refs(row: KnowledgeClaim) -> list[str]:
    try:
        return [str(v) for v in json.loads(row.source_refs_json or "[]")]
    except ValueError:
        return []


def _claims_for_intent(session, origin: str) -> list[KnowledgeClaim]:
    return [row for row in session.scalars(select(KnowledgeClaim))
            if row.confidence_basis.startswith(origin + " ")]


def _invalidate(session, claim: KnowledgeClaim, reason: str) -> None:
    if claim.state in {"active", "conflicting"}:
        claim.state = "stale"
        claim.confidence_basis = (claim.confidence_basis
                                  + f" | stale:{reason}").strip(" |")


def _content_key(row: Experience) -> str:
    return sha256_json({"intent": row.intent_id, "version": int(row.version or 1),
                        "question": row.question_pattern, "answer": row.canonical_answer,
                        "source_ids": row.source_ids,
                        "owner": row.owner, "tenant": row.tenant,
                        "visibility": row.visibility, "patterns": row.patterns(),
                        "message": row.source_message_id or ""})[:24]


def sync_experience(session, row: Experience, *, space_id: str = "", org_id: str = "") -> dict:
    """把一个 Experience 的当前事实登记为知识主张（幂等；不自动采用/召回）。"""
    from .provenance import (experience_source_ref, read_experience_source,
                             resolve_experience_binding)
    binding = resolve_experience_binding(session, row)
    if binding is not None:
        if ((org_id and org_id != binding["org_id"])
                or (space_id and space_id != binding["space_id"])):
            raise knowledge_service.KnowledgeError("claim_source_scope_mismatch")
        org_id, space_id = binding["org_id"], binding["space_id"]
    elif org_id:
        return {"ok": False, "action": "pending_binding", "reason": "experience_scope_unbound"}
    ref = experience_source_ref(row) if binding is not None else experience_ref(row)
    origin = "experience-origin:" + sha256_json({"tenant": row.tenant, "owner": row.owner,
        "intent": row.intent_id, "org": org_id, "space": space_id})[:24]
    now = utcnow()
    available = (row.status in AVAILABLE_STATUSES
                 and (row.valid_until is None or row.valid_until > now))
    if binding is not None and row.status not in {ExperienceStatus.ASSIST, ExperienceStatus.APPROVED_AUTO}:
        available = False
    siblings = _claims_for_intent(session, origin)
    # Revoke every projection of this exact source, including a binding which
    # can no longer resolve. Another tenant's same-named intent is untouched.
    for claim in session.scalars(select(KnowledgeClaim)):
        if experience_source_ref(row) in _refs(claim) and claim not in siblings:
            if (not available or binding is None
                    or (claim.org_id, claim.space_id) != (org_id, space_id)):
                _invalidate(session, claim, "experience_binding_unavailable")
    if not available:
        stale = []
        for claim in siblings:
            if claim.state in {"active", "conflicting"}:
                _invalidate(session, claim, f"experience_{row.status}")
                stale.append(claim.claim_id)
        session.flush()
        return {"ok": True, "action": "invalidated", "claim_ids": stale, "ref": ref}

    # 旧版本/旧内容：先失效，再以新主张 supersede（历史保留）
    for claim in siblings:
        refs = _refs(claim)
        if ref not in refs and claim.state in {"active", "conflicting"}:
            _invalidate(session, claim, "experience_version_superseded")
    key = _content_key(row)
    marker = f"{origin} experience-sync:{key}"
    for claim in siblings:
        if ref in _refs(claim) and claim.state in {"active", "conflicting"} \
                and marker in claim.confidence_basis:
            from .provenance import claim_sources_current
            if claim_sources_current(session, claim):
                session.flush()
                return {"ok": True, "action": "unchanged", "claim_id": claim.claim_id,
                        "ref": ref}
    previous = sorted(
        (claim for claim in siblings
         if claim.state in {"active", "conflicting", "stale"}),
        key=lambda claim: int(claim.source_revision or 0), reverse=True)
    previous_claim = previous[0] if previous else None
    refs = [ref]
    if row.source_message_id and binding is None:
        refs.append(f"message:{row.source_message_id}")
    try:
        source_ids = [str(v) for v in json.loads(row.source_ids or "[]") if str(v)]
    except ValueError:
        source_ids = []
    if binding is None:
        refs.extend(f"source:{value}" for value in source_ids)
    owner_scope = "public" if row.visibility == "public" else (row.owner or "private")
    writer = knowledge_service.record_verified_claim if binding else knowledge_service.record_claim
    extra = {"source_reader": read_experience_source} if binding else {}
    claim = writer(
        session, org_id=org_id, space_id=space_id, owner_scope=owner_scope,
        subject=f"experience:{row.intent_id}"[:255], predicate=PREDICATE,
        aliases=[text for text in [row.question_pattern, *row.patterns()] if str(text).strip()],
        value=row.canonical_answer, source_refs=refs,
        source_revision=int(row.version or 1), valid_until=row.valid_until,
        confidence_basis=marker,
        supersedes=previous_claim.claim_id if previous_claim is not None else "", **extra)
    session.flush()
    return {"ok": True, "action": "recorded" if previous_claim is None else "superseded",
            "claim_id": claim.claim_id,
            "superseded": previous_claim.claim_id if previous_claim else "",
            "ref": ref, "binding_state": "bound" if binding else "unbound"}


def sync_all(session, *, tenant: str = "", space_id: str = "",
             limit: int = 500) -> dict:
    """按 (tenant, intent) 取最新版本逐个同步；返回分项计数。"""
    query = select(Experience)
    if tenant:
        query = query.where(Experience.tenant == tenant)
    rows = list(session.scalars(query))
    latest: dict[tuple[str, str], Experience] = {}
    for row in rows:
        key = (row.tenant or "", row.intent_id or "")
        current = latest.get(key)
        if current is None or int(row.version or 1) > int(current.version or 1):
            latest[key] = row
    counts = {"recorded": 0, "superseded": 0, "unchanged": 0, "invalidated": 0}
    for row in list(latest.values())[:max(1, int(limit))]:
        outcome = sync_experience(session, row, space_id=space_id)
        action = outcome.get("action")
        if action in counts:
            counts[action] += 1
    return counts
