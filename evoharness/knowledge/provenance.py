"""Resolve published Experience through its existing source and channel authority."""

from __future__ import annotations

import json
import re

from sqlalchemy import select

from evoharness.clock import utcnow
from evoharness.ids import sha256_json
from evoharness.models import Experience, Message, Source
from evoharness.organization.models import (
    BusinessSpace,
    ChannelBinding,
    Membership,
    Organization,
)
from evoharness.organization.service import _live


def experience_source_ref(row):
    # The persisted row ID is globally unique in this product DB; the snapshot
    # additionally binds tenant, owner, app and business scope.
    return f"experience-row:{row.id}@{int(row.version or 1)}"


def resolve_experience_binding(session, row):
    refs = sorted(
        set(
            [str(v) for v in row.source_message_ids() if v]
            + ([row.source_message_id] if row.source_message_id else [])
        )
    )
    if not refs or not row.tenant or not row.owner:
        return None
    candidates = None
    evidence = []
    now = utcnow()
    for ref in refs:
        message = session.scalar(
            select(Message).where(
                Message.tenant == row.tenant,
                Message.message_id == ref,
                Message.recalled.is_(False),
            )
        )
        source = (
            session.get(Source, message.source_id)
            if message and message.source_id
            else None
        )
        if (
            source is None
            or source.tenant != row.tenant
            or source.owner != row.owner
            or not source.enabled
            or source.revoked_at is not None
            or source.chat_id != message.chat_id
            or (source.thread_id and source.thread_id != message.thread_id)
            or message.msg_type not in source.allowed_type_set
        ):
            return None
        bindings = list(
            session.scalars(
                select(ChannelBinding).where(
                    ChannelBinding.tenant_id == row.tenant,
                    ChannelBinding.channel_id == message.chat_id,
                    ChannelBinding.enabled.is_(True),
                    ChannelBinding.read_grant.is_(True),
                )
            )
        )
        bindings = [b for b in bindings if b.thread_scope in {"", message.thread_id}]
        specific = [
            b
            for b in bindings
            if b.thread_scope and b.thread_scope == message.thread_id
        ]
        if specific:
            bindings = specific
        allowed = set()
        for binding in bindings:
            org = session.get(Organization, binding.org_id)
            space = session.get(BusinessSpace, binding.space_id)
            memberships = list(
                session.scalars(
                    select(Membership).where(
                        Membership.org_id == binding.org_id,
                        Membership.tenant_id == row.tenant,
                        Membership.app_id == binding.app_id,
                        Membership.principal_id == row.owner,
                    )
                )
            )
            if (
                org is not None
                and org.enabled
                and space is not None
                and space.enabled
                and space.org_id == org.org_id
                and len(memberships) == 1
                and _live(memberships[0], now)
            ):
                allowed.add((binding.org_id, binding.space_id, binding.app_id))
        candidates = allowed if candidates is None else candidates & allowed
        evidence.append(
            {
                "message_id": message.message_id,
                "revision": message.revision,
                "content_sha256": sha256_json(message.content_text),
                "source_id": source.id,
                "source_updated_at": str(source.updated_at),
                "bindings": sorted(
                    (b.id, b.revision, b.org_id, b.space_id, b.app_id)
                    for b in bindings
                    if (b.org_id, b.space_id, b.app_id) in allowed
                ),
            }
        )
    if not candidates or len(candidates) != 1:
        return None
    org_id, space_id, app_id = next(iter(candidates))
    return {
        "org_id": org_id,
        "space_id": space_id,
        "app_id": app_id,
        "tenant": row.tenant,
        "owner": row.owner,
        "sources": evidence,
    }


def read_experience_source(session, ref, *, org_id, space_id, owner_scope):
    from evoharness.knowledge.service import KnowledgeError

    match = re.fullmatch(r"experience-row:(\d+)@(\d+)", str(ref))
    if not match:
        raise KnowledgeError("claim_source_unverified")
    row = session.get(Experience, int(match[1]))
    if (
        row is None
        or int(row.version or 1) != int(match[2])
        or row.status not in {"approved_auto", "assist"}
        or row.visibility not in {"private", "public"}
        or (row.valid_until is not None and row.valid_until <= utcnow())
    ):
        raise KnowledgeError("claim_source_unavailable")
    binding = resolve_experience_binding(session, row)
    expected_owner = "public" if row.visibility == "public" else row.owner
    if (
        binding is None
        or binding["org_id"] != org_id
        or binding["space_id"] != space_id
        or owner_scope != expected_owner
    ):
        raise KnowledgeError("claim_source_scope_mismatch")
    payload = {
        "ref": ref,
        **binding,
        "revision": int(row.version or 1),
        "owner_scope": expected_owner,
        "question": row.question_pattern,
        "answer": row.canonical_answer,
        "patterns": row.patterns(),
        "valid_until": str(row.valid_until),
    }
    return {
        "ref": ref,
        "sha256": sha256_json(payload),
        "revision": int(row.version or 1),
        "owner_scope": expected_owner,
        **{
            key: binding[key]
            for key in ("org_id", "space_id", "app_id", "tenant", "owner")
        },
    }


def claim_sources_current(session, claim):
    if claim.evidence_level != "source_verified":
        return True  # Legacy declarations keep their explicit evidence level.
    try:
        snapshots = json.loads(claim.source_snapshots_json)
        refs = json.loads(claim.source_refs_json)
        if not snapshots or [item["ref"] for item in snapshots] != refs:
            return False
        for expected in snapshots:
            actual = read_experience_source(
                session,
                expected["ref"],
                org_id=claim.org_id,
                space_id=claim.space_id,
                owner_scope=claim.owner_scope,
            )
            if actual != expected:
                return False
        return True
    except (ValueError, KeyError, TypeError):
        return False
