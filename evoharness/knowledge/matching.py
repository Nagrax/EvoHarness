"""Permission-cropped lexical matches and one hop over source-proven relations."""

import json
import re

from sqlalchemy import select
from .models import KnowledgeRelation


def _terms(text):
    words = set(re.findall(r"[a-z0-9_]{2,}", text.casefold()))
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(part[index : index + 2] for index in range(len(part) - 1))
    return words - {"怎么", "如何", "什么", "是否", "请问", "需要", "处理"}


def relevant_claims(session, rows, query):
    """Rows have already passed identity, visibility, expiry and source checks."""
    from .compiler import claim_fingerprint
    from evoharness.clock import utcnow

    query = query.casefold()
    selected = {}
    for row in rows:
        try:
            terms = [row.subject, *json.loads(row.aliases_json or "[]")]
        except (TypeError, ValueError):
            terms = [row.subject]
        for term in terms:
            if not isinstance(term, str) or not term.strip():
                continue
            if term.casefold() in query:
                selected[row.claim_id] = row
                break
            tokens = _terms(term)
            overlap = tokens & _terms(query)
            if len(overlap) >= 2 and len(overlap) / max(1, len(tokens)) >= 0.5:
                selected[row.claim_id] = row
                break
    if not selected:
        return []
    visible = {row.claim_id: row for row in rows}
    direct = set(selected)
    # Both endpoints must be visible and current; hidden edges contribute no
    # count, IDs or conflict metadata to the model-facing result.
    for edge in session.scalars(
        select(KnowledgeRelation).where(
            KnowledgeRelation.from_claim_id.in_(direct)
            | KnowledgeRelation.to_claim_id.in_(direct)
        )
    ):
        first = visible.get(edge.from_claim_id)
        second = visible.get(edge.to_claim_id)
        if (
            first is None
            or second is None
            or edge.kind != "same_source"
            or first.org_id != edge.org_id
            or second.org_id != edge.org_id
            or first.space_id != edge.space_id
            or second.space_id != edge.space_id
            or claim_fingerprint(first) != edge.from_sha256
            or claim_fingerprint(second) != edge.to_sha256
        ):
            continue
        if any(
            row.valid_until is not None and row.valid_until < utcnow()
            for row in (first, second)
        ):
            continue
        selected[first.claim_id] = first
        selected[second.claim_id] = second
        if len(selected) >= 200:
            break
    return [selected[key] for key in sorted(selected)]
