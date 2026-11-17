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


def _claims_for_intent(session, intent_id: str) -> list[KnowledgeClaim]:
    prefix = f"experience:{intent_id}@"
    return [row for row in session.scalars(select(KnowledgeClaim))
            if any(ref.startswith(prefix) for ref in _refs(row))]


def _invalidate(session, claim: KnowledgeClaim, reason: str) -> None:
    if claim.state in {"active", "conflicting"}:
        claim.state = "stale"
        claim.confidence_basis = (claim.confidence_basis
                                  + f" | stale:{reason}").strip(" |")


def _content_key(row: Experience) -> str:
    return sha256_json({"intent": row.intent_id, "version": int(row.version or 1),
                        "question": row.question_pattern, "answer": row.canonical_answer,
                        "source_ids": row.source_ids,
                        "message": row.source_message_id or ""})[:24]


def sync_experience(session, row: Experience, *, space_id: str = "") -> dict:
    """把一个 Experience 的当前事实登记为知识主张（幂等；不自动采用/召回）。"""
    ref = experience_ref(row)
    now = utcnow()
    available = (row.status in AVAILABLE_STATUSES
                 and (row.valid_until is None or row.valid_until > now))
    siblings = _claims_for_intent(session, row.intent_id)
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
    marker = f"experience-sync:{key}"
    for claim in siblings:
        if ref in _refs(claim) and claim.state in {"active", "conflicting"} \
                and marker in claim.confidence_basis:
            session.flush()
            return {"ok": True, "action": "unchanged", "claim_id": claim.claim_id,
                    "ref": ref}
    previous = sorted(
        (claim for claim in siblings
         if claim.state in {"active", "conflicting", "stale"}),
        key=lambda claim: int(claim.source_revision or 0), reverse=True)
    previous_claim = previous[0] if previous else None
    refs = [ref]
    if row.source_message_id:
        refs.append(f"message:{row.source_message_id}")
    try:
        source_ids = [str(v) for v in json.loads(row.source_ids or "[]") if str(v)]
    except ValueError:
        source_ids = []
    refs.extend(f"source:{value}" for value in source_ids)
    owner_scope = (row.owner or "private") if row.visibility == "private" else "public"
    claim = knowledge_service.record_claim(
        session, space_id=space_id, owner_scope=owner_scope,
        subject=f"experience:{row.intent_id}"[:255], predicate=PREDICATE,
        value=row.canonical_answer, source_refs=refs,
        source_revision=int(row.version or 1), valid_until=row.valid_until,
        confidence_basis=marker,
        supersedes=previous_claim.claim_id if previous_claim is not None else "")
    session.flush()
    return {"ok": True, "action": "recorded" if previous_claim is None else "superseded",
            "claim_id": claim.claim_id,
            "superseded": previous_claim.claim_id if previous_claim else "",
            "ref": ref}


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
