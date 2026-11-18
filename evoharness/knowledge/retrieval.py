"""Task-scoped knowledge index and on-demand reads for employee execution.

The Worker supplies the binding. Model arguments can select an indexed claim,
but cannot select an organization, owner, or space. No retrieval cache is kept.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from evoharness.clock import utcnow
from evoharness.ids import sha256_json
from evoharness.knowledge.models import KnowledgeClaim
from evoharness.knowledge.provenance import claim_sources_current
from evoharness.models import Task
from evoharness.work.models import ExecutionRun
from evoharness.organization.models import Assignment, Membership
from evoharness.organization.service import (
    AuthenticatedPrincipal, OrganizationError, resolve_context)


class RetrievalError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _reader(session, *, actor_id, tenant, org_id, space_id):
    """Resolve the persisted actor through the existing role authority service."""
    assignment = session.get(Assignment, actor_id)
    if assignment is not None:
        member = session.get(Membership, assignment.membership_id)
        members = [member] if member is not None else []
        assignments = [assignment]
    else:
        members = list(session.scalars(select(Membership).where(
            Membership.org_id == org_id, Membership.tenant_id == tenant,
            Membership.principal_id == actor_id)))
        assignments = list(session.scalars(select(Assignment).where(
            Assignment.org_id == org_id,
            Assignment.membership_id.in_([m.membership_id for m in members]))))
    # Without an app binding on ExecutionRun, two namespaces cannot be guessed.
    if len(members) != 1:
        raise RetrievalError("knowledge_not_authorized")
    member = members[0]
    if member.org_id != org_id or member.tenant_id != tenant:
        raise RetrievalError("knowledge_not_authorized")
    actor = AuthenticatedPrincipal(member.principal_id, member.tenant_id,
                                   member.app_id, True)
    for current in assignments:
        try:
            context = resolve_context(session, actor, org_id=org_id,
                space_id=space_id, assignment_id=current.assignment_id)
        except OrganizationError:
            continue
        if "knowledge.read" in context.allowed_actions:
            return member.principal_id
    raise RetrievalError("knowledge_not_authorized")


def execution_index(session, *, run_id, task_id, org_id, space_id, principal_id,
                    max_entries=12, max_chars=6000):
    """Return a bounded source index using only the persisted task's goal.

    Exact subjects and source-owned aliases match the persisted task goal.
    """
    empty = {"claims": [], "conflicts": [], "expired_hint": [], "omitted": 0}
    if not all((run_id, task_id, org_id, space_id, principal_id)):
        return empty | {"snapshot_sha256": sha256_json(empty)}
    run = session.get(ExecutionRun, run_id)
    task = session.get(Task, int(task_id))
    if (run is None or task is None or run.task_id != task.id
            or run.actor_id != principal_id or task.org_id != org_id
            or task.space_id != space_id or run.tenant != task.tenant):
        raise RetrievalError("knowledge_binding_mismatch")
    query = f"{task.title or ''}\n{task.goal or ''}".casefold()
    now = utcnow()
    assignment = session.get(Assignment, principal_id)
    member = session.get(Membership, assignment.membership_id) if assignment else None
    reader_id = member.principal_id if member is not None else principal_id
    rows = list(session.scalars(select(KnowledgeClaim).where(
        KnowledgeClaim.org_id == org_id,
        KnowledgeClaim.space_id == space_id,
        KnowledgeClaim.owner_scope.in_(["", "public", reader_id]),
        KnowledgeClaim.state.in_(["active", "conflicting"]),
    ).order_by(KnowledgeClaim.claim_id)))
    from .matching import relevant_claims
    relevant = relevant_claims(session,[row for row in rows if claim_sources_current(session,row)],query)
    if relevant:
        _reader(session, actor_id=principal_id, tenant=run.tenant,
                org_id=org_id, space_id=space_id)
    values = {}
    for row in relevant:
        if row.valid_until is None or now <= row.valid_until:
            values.setdefault((row.subject, row.predicate), set()).add(row.value)
    claims, expired, fingerprints = [], [], []
    omitted = 0
    for row in relevant:
        if row.valid_until is not None and now > row.valid_until:
            if len(expired) < max_entries:
                expired.append(row.claim_id)
            continue
        # Conflict metadata must not reveal that an invisible source exists.
        state = ("conflicting" if len(values[(row.subject, row.predicate)]) > 1 else "active")
        entry = {"claim_id": row.claim_id, "subject": row.subject,
                 "predicate": row.predicate, "state": state,
                 "source_refs": json.loads(row.source_refs_json),
                 "source_revision": row.source_revision,
                 "evidence_level": row.evidence_level}
        if (len(claims) >= max_entries or len(json.dumps(
                claims + [entry], ensure_ascii=False)) > max_chars):
            omitted += 1
            continue
        claims.append(entry)
        # Detect edits to the body as well as provenance, permissions and TTL.
        fingerprints.append(sha256_json(entry | {
            "value": row.value, "owner_scope": row.owner_scope,
            "sources": row.source_snapshots_json, "aliases": row.aliases_json,
            "valid_until": row.valid_until.isoformat() if row.valid_until else None,
        }))
    index = {"claims": claims,
             "conflicts": [x["claim_id"] for x in claims if x["state"] == "conflicting"],
             "expired_hint": expired, "omitted": omitted}
    return index | {"snapshot_sha256": sha256_json(index | {"bodies": fingerprints})}


def read_indexed_claim(session, *, index, claim_id, offset=0, limit=4000):
    """Read one already authorized claim; large bodies use explicit pages."""
    entry = next((item for item in index["claims"] if item["claim_id"] == claim_id), None)
    if entry is None:
        raise RetrievalError("knowledge_claim_not_indexed")
    row = session.get(KnowledgeClaim, claim_id)
    if row is None or row.state not in {"active", "conflicting"}:
        raise RetrievalError("knowledge_context_changed")
    if not claim_sources_current(session, row):
        raise RetrievalError("knowledge_context_changed")
    if row.valid_until is not None and utcnow() > row.valid_until:
        raise RetrievalError("knowledge_context_changed")
    try:
        offset, limit = int(offset), int(limit)
    except (TypeError, ValueError):
        raise RetrievalError("knowledge_page_invalid") from None
    if offset < 0 or limit < 1 or limit > 4000:
        raise RetrievalError("knowledge_page_invalid")
    text = row.value or ""
    end = min(len(text), offset + limit)
    result = {"claim_id": row.claim_id, "value": text[offset:end],
            "source_refs": json.loads(row.source_refs_json),
            "source_revision": row.source_revision, "state": entry["state"],
            "offset": offset, "next_offset": end if end < len(text) else None,
            "total_chars": len(text), "trust": "source_data"}
    # ToolExecutor caps text results at 4000 characters. Fit the serialized
    # JSON, including escaping/provenance, instead of letting that cap cut JSON.
    while len(json.dumps(result, ensure_ascii=False)) > 3800:
        overflow = len(json.dumps(result, ensure_ascii=False)) - 3800
        if not result["value"]:
            raise RetrievalError("knowledge_metadata_too_large")
        result["value"] = result["value"][:max(0, len(result["value"]) - overflow)]
        end = offset + len(result["value"])
        result["next_offset"] = end if end < len(text) else None
    return result


def validate_run_snapshot(session, run):
    """Recheck persisted execution provenance at the acceptance boundary."""
    from .execution_skills import validate_run_skills
    validate_run_skills(session, run)
    checkpoint = json.loads(run.checkpoint_json or '{}')
    snapshot = checkpoint.get('knowledge')
    if snapshot is None:  # Compatibility with runs that did not retrieve knowledge.
        return
    if not isinstance(snapshot, dict) or not snapshot.get('snapshot_sha256'):
        raise RetrievalError('knowledge_snapshot_invalid')
    task = session.get(Task, run.task_id)
    if task is None:
        raise RetrievalError('knowledge_binding_mismatch')
    current = execution_index(session, run_id=run.id, task_id=task.id,
        org_id=task.org_id, space_id=task.space_id, principal_id=run.actor_id)
    if (current['snapshot_sha256'] != snapshot['snapshot_sha256']
            or [x['claim_id'] for x in current['claims']] != snapshot.get('claim_ids')):
        raise RetrievalError('knowledge_context_changed')
