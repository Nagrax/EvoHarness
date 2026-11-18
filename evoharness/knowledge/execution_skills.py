"""Scoped SOP reads for the existing Worker, with actual-load receipts."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

from agents.frontmatter import parse_frontmatter
from agents.skill_scope import ScopedSkillRef
from evoharness.ids import sha256_json
from evoharness.models import Task
from evoharness.work.models import ExecutionRun
from evoharness.organization.models import Assignment, Membership
from .retrieval import RetrievalError, _reader
from .scoped_skills import _sources
from .skill_scope_events import scope_denial, latest_scope_event


def execution_skill_index(
    session,
    *,
    run_id,
    task_id,
    org_id,
    space_id,
    principal_id,
    include_experimental_for="",
):
    empty = {"skills": []}
    if not all((run_id, task_id, org_id, space_id, principal_id)):
        return {**empty, "snapshot_sha256": sha256_json(empty)}
    run = session.get(ExecutionRun, run_id)
    task = session.get(Task, int(task_id))
    if (
        run is None
        or task is None
        or run.task_id != task.id
        or run.actor_id != principal_id
        or task.org_id != org_id
        or task.space_id != space_id
        or run.tenant != task.tenant
    ):
        raise RetrievalError("skill_binding_mismatch")
    assignment = session.get(Assignment, principal_id)
    member = session.get(Membership, assignment.membership_id) if assignment else None
    reader = member.principal_id if member else principal_id
    available = list(_sources(run.tenant, space_id, reader))
    if available:
        _reader(
            session,
            actor_id=principal_id,
            tenant=run.tenant,
            org_id=org_id,
            space_id=space_id,
        )
    from agents.skills import (
        _parse_skill_file,
        build_retrieval_corpus,
        rank_corpus_for_query,
        _experimental_injection_enabled,
    )
    from agents.skill_tiers import get_tier_scoped

    sources = {}
    for file, meta, ref in available:
        if scope_denial(
            session,
            skill_name=ref.name,
            tenant=run.tenant,
            space=space_id,
            owner=reader,
            revision=ref.source_revision,
        ):
            continue
        scope = {k: v for k, v in asdict(ref).items() if k != "name"}
        if (
            get_tier_scoped(ref.name, scope) == "experimental"
            and not _experimental_injection_enabled()
            and ref.name != include_experimental_for
        ):
            continue
        if ref.name in sources:
            raise RetrievalError("skill_namespace_ambiguous")
        definition = _parse_skill_file(file, "scoped", str(file.parent))
        if definition.context == "fork":
            continue
        sources[ref.name] = (file, definition, ref)
    corpus = build_retrieval_corpus([value[1] for value in sources.values()])
    ranked = (
        rank_corpus_for_query(f"{task.title}\n{task.goal}", corpus) if corpus else []
    )
    rows = []
    for name, score in ranked:
        if score <= 0 or len(rows) >= 6:
            continue
        file, definition, ref = sources[name]
        event = latest_scope_event(
            session,
            skill_name=ref.name,
            tenant=ref.tenant,
            space=ref.space,
            owner=ref.owner,
        )
        version = parse_frontmatter(file.read_text(encoding="utf-8-sig")).meta.get(
            "version", ""
        )
        rows.append(
            {
                "name": name,
                "description": definition.description[:500],
                "ref": asdict(ref),
                "file_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                "skill_sha256": definition.content_sha256,
                "tier": get_tier_scoped(name, asdict(ref)),
                "scope_generation": event.id if event else 0,
                "version": str(version),
            }
        )
    result = {"skills": rows}
    return {**result, "snapshot_sha256": sha256_json(result)}


def read_execution_skill(session, *, binding, index, skill_name, offset=0, limit=2500):
    current = execution_skill_index(session, **binding)
    if current["snapshot_sha256"] != index["snapshot_sha256"]:
        raise RetrievalError("skill_context_changed")
    entry = next((row for row in current["skills"] if row["name"] == skill_name), None)
    if entry is None:
        raise RetrievalError("skill_not_indexed")
    ref = ScopedSkillRef(**entry["ref"])
    matches = [
        (file, meta)
        for file, meta, candidate in _sources(ref.tenant, ref.space, ref.owner)
        if candidate == ref
    ]
    if len(matches) != 1:
        raise RetrievalError("skill_context_changed")
    body = parse_frontmatter(matches[0][0].read_text(encoding="utf-8-sig")).body
    if (
        type(offset) is not int
        or type(limit) is not int
        or offset < 0
        or not 1 <= limit <= 2500
    ):
        raise RetrievalError("skill_page_invalid")
    end = min(len(body), offset + limit)
    result = {
        **entry,
        "instructions": body[offset:end],
        "offset": offset,
        "next_offset": end if end < len(body) else None,
        "total_chars": len(body),
        "trust": "task_scoped_sop",
    }
    while len(json.dumps(result, ensure_ascii=False)) > 3800:
        if not result["instructions"]:
            raise RetrievalError("skill_metadata_too_large")
        result["instructions"] = result["instructions"][:-100]
        result["next_offset"] = offset + len(result["instructions"])
    return result


def validate_run_skills(session, run, *, prepared_release_name=""):
    expected = json.loads(run.checkpoint_json or "{}").get("skills")
    if expected is None:
        return
    task = session.get(Task, run.task_id)
    if not isinstance(expected, dict) or task is None:
        raise RetrievalError("skill_snapshot_invalid")
    current = execution_skill_index(
        session,
        run_id=run.id,
        task_id=run.task_id,
        org_id=task.org_id,
        space_id=task.space_id,
        principal_id=run.actor_id,
        include_experimental_for=prepared_release_name,
    )
    if prepared_release_name:
        after = next(
            (row for row in current["skills"] if row["name"] == prepared_release_name),
            None,
        )
        # Only the conservative tier change of a prepared release is allowed;
        # the exact file, version, scope generation and all other entries match.
        if after and after["tier"] == "experimental":
            from agents.skill_tiers import VALID_TIERS

            for previous_tier in VALID_TIERS:
                after["tier"] = previous_tier
                digest = sha256_json({"skills": current["skills"]})
                if digest == expected.get("snapshot_sha256"):
                    current["snapshot_sha256"] = digest
                    break
            after["tier"] = "experimental"
    if current["snapshot_sha256"] != expected.get("snapshot_sha256"):
        raise RetrievalError("skill_context_changed")
