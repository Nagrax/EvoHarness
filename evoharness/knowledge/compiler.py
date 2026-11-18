"""Atomic, reproducible Experience views in the existing knowledge service."""

from __future__ import annotations

import json
from itertools import combinations

from sqlalchemy import select, update

from evoharness.clock import utcnow
from evoharness.ids import sha256_json
from evoharness.models import Experience
from .experience_bridge import sync_experience
from .models import KnowledgeClaim, KnowledgePublication, KnowledgeRelation
from .provenance import (
    experience_source_ref,
    read_experience_source,
    resolve_experience_binding,
)
from .service import KnowledgeError


def claim_fingerprint(row):
    return sha256_json(
        [
            row.claim_id,
            row.org_id,
            row.space_id,
            row.owner_scope,
            row.subject,
            row.value,
            row.source_revision,
            row.source_snapshots_json,
            row.state,
        ]
    )


def scope_key(org_id, space_id, owner):
    return sha256_json([org_id, space_id, owner])


def prepare_manifest(session, *, org_id, space_id, tenant, owner):
    """Freeze only this owner's approved, currently authorized sources."""
    rows = session.scalars(
        select(Experience).where(
            Experience.tenant == tenant,
            Experience.owner == owner,
            Experience.status.in_(["approved_auto", "assist"]),
        )
    )
    latest = {}
    for row in rows:
        if row.valid_until is not None and row.valid_until <= utcnow():
            continue
        binding = resolve_experience_binding(session, row)
        if binding is None or (binding["org_id"], binding["space_id"]) != (
            org_id,
            space_id,
        ):
            continue
        previous = latest.get(row.intent_id)
        if previous is None or row.version > previous.version:
            latest[row.intent_id] = row
    manifest = []
    for row in latest.values():
        owner_scope = "public" if row.visibility == "public" else owner
        snapshot = read_experience_source(
            session,
            experience_source_ref(row),
            org_id=org_id,
            space_id=space_id,
            owner_scope=owner_scope,
        )
        manifest.append({"experience_id": row.id, "snapshot": snapshot})
    return sorted(manifest, key=lambda item: item["snapshot"]["ref"])


def publish_manifest(
    session, *, org_id, space_id, tenant, owner, manifest, expected_revision
):
    """All inputs validate before the current publication changes; no model IO."""
    if (
        not isinstance(manifest, list)
        or type(expected_revision) is not int
        or expected_revision < 0
        or len(manifest) > 500
    ):
        raise KnowledgeError("knowledge_manifest_invalid")
    key = scope_key(org_id, space_id, owner)
    build_sha256 = sha256_json(
        {
            "schema": 1,
            "compiler": "experience-v1",
            "scope": [org_id, space_id, tenant, owner],
            "manifest": manifest,
        }
    )
    with session.begin_nested():
        publication = session.get(KnowledgePublication, key)
        if publication is None:
            if expected_revision != 0:
                raise KnowledgeError("knowledge_publication_changed")
            publication = KnowledgePublication(
                scope_key=key,
                org_id=org_id,
                space_id=space_id,
                owner_id=owner,
                revision=0,
            )
            session.add(publication)
            session.flush()
        elif publication.revision != expected_revision:
            raise KnowledgeError("knowledge_publication_changed")
        checked, seen = [], set()
        for entry in manifest:
            if not isinstance(entry, dict) or set(entry) != {
                "experience_id",
                "snapshot",
            }:
                raise KnowledgeError("knowledge_manifest_invalid")
            row = session.get(Experience, entry["experience_id"])
            if (
                row is None
                or row.tenant != tenant
                or row.owner != owner
                or row.id in seen
            ):
                raise KnowledgeError("knowledge_manifest_source_invalid")
            seen.add(row.id)
            snapshot = read_experience_source(
                session,
                experience_source_ref(row),
                org_id=org_id,
                space_id=space_id,
                owner_scope="public" if row.visibility == "public" else owner,
            )
            if snapshot != entry["snapshot"]:
                raise KnowledgeError("knowledge_manifest_source_changed")
            checked.append(row)
        # A caller cannot omit a visible approved input and report completion.
        if (
            prepare_manifest(
                session, org_id=org_id, space_id=space_id, tenant=tenant, owner=owner
            )
            != manifest
        ):
            raise KnowledgeError("knowledge_manifest_incomplete")
        if publication.build_sha256 == build_sha256:
            return {**json.loads(publication.receipt_json), "unchanged": True}
        changed = session.execute(
            update(KnowledgePublication)
            .where(
                KnowledgePublication.scope_key == key,
                KnowledgePublication.revision == expected_revision,
            )
            .values(revision=expected_revision + 1)
        )
        if changed.rowcount != 1:
            raise KnowledgeError("knowledge_publication_changed")
        outcomes = [
            sync_experience(session, row, org_id=org_id, space_id=space_id)
            for row in checked
        ]
        if any(not item.get("ok") or not item.get("claim_id") for item in outcomes):
            raise KnowledgeError("knowledge_build_incomplete")
        claim_ids = [item["claim_id"] for item in outcomes]
        previous_ids = json.loads(publication.claim_ids_json or "[]")
        for claim_id in set(previous_ids) - set(claim_ids):
            old = session.get(KnowledgeClaim, claim_id)
            if old is not None and old.state in {"active", "conflicting"}:
                old.state = "stale"
        for (left, left_result), (right, right_result) in combinations(
            zip(checked, outcomes), 2
        ):
            shared = set(left.source_message_ids()) & set(right.source_message_ids())
            if not shared:
                continue
            first = session.get(KnowledgeClaim, left_result["claim_id"])
            second = session.get(KnowledgeClaim, right_result["claim_id"])
            relation_id = sha256_json([first.claim_id, second.claim_id, "same_source"])
            if session.get(KnowledgeRelation, relation_id) is None:
                session.add(
                    KnowledgeRelation(
                        relation_id=relation_id,
                        org_id=org_id,
                        space_id=space_id,
                        from_claim_id=first.claim_id,
                        to_claim_id=second.claim_id,
                        kind="same_source",
                        from_sha256=claim_fingerprint(first),
                        to_sha256=claim_fingerprint(second),
                        evidence_refs_json=json.dumps(sorted(shared)),
                    )
                )
        receipt = {
            "completed": True,
            "revision": expected_revision + 1,
            "build_sha256": build_sha256,
            "inputs": len(manifest),
            "processed": len(outcomes),
            "claim_ids": claim_ids,
            "changed": sum(item["action"] != "unchanged" for item in outcomes),
            "model_calls": 0,
        }
        publication.manifest_json = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True
        )
        publication.claim_ids_json = json.dumps(claim_ids)
        publication.build_sha256 = build_sha256
        publication.receipt_json = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True
        )
        session.flush()
        return receipt
