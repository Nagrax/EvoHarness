"""Task results enter the original extractor/maintainer as scoped candidates."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update, or_, and_, event
from sqlalchemy.orm import Session
import logging

from agents.frontmatter import parse_frontmatter
from agents.skill_evolution import skill_state_read_only
from agents.skill_scope import ScopedSkillRef
from evoharness.ids import sha256_json
from evoharness.clock import utcnow
from evoharness.models import Task
from evoharness.work.models import Artifact, ExecutionRun, SkillLearningEvidence
from .execution_skills import validate_run_skills
from .scoped_skills import _sources


class LearningError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _feedback_after_commit(session):
    if session.in_nested_transaction():
        return
    from agents.skill_evolution import record_skill_feedback

    for item in session.info.pop("task_skill_feedback", []):
        try:
            record_skill_feedback(**item)
        except OSError:
            logging.getLogger(__name__).warning(
                "Skill usage feedback write deferred; task evidence is durable"
            )


def _feedback_after_rollback(session):
    if not session.in_nested_transaction():
        session.info.pop("task_skill_feedback", None)


if not event.contains(Session, "after_commit", _feedback_after_commit):
    event.listen(Session, "after_commit", _feedback_after_commit)
    event.listen(Session, "after_rollback", _feedback_after_rollback)


def _context_digest(task, run):
    return sha256_json(
        [
            task.title,
            task.goal,
            task.acceptance_json,
            task.owner,
            task.assignee_id,
            task.org_id,
            task.space_id,
            run.context_hash,
        ]
    )


def capture_feedback(session, task, run, *, reviewer, verdict, notes):
    loaded = json.loads(run.checkpoint_json or "{}").get("skills", {}).get("loaded", [])
    if not loaded or skill_state_read_only():
        return None
    key = sha256_json(
        [task.id, run.id, run.generation, run.task_revision, reviewer, verdict, notes]
    )
    existing = session.get(SkillLearningEvidence, key)
    if existing is not None:
        return existing
    row = SkillLearningEvidence(
        evidence_id=key,
        task_id=task.id,
        run_id=run.id,
        generation=run.generation,
        task_revision=run.task_revision,
        reviewer=reviewer,
        verdict=verdict,
        notes=str(notes or "")[:4000],
        context_sha256=_context_digest(task, run),
        skills_json=json.dumps(loaded, ensure_ascii=False),
        state="pending" if notes.strip() else "observed",
    )
    session.add(row)
    session.flush()
    for entry in loaded:
        ref = entry["ref"]
        session.info.setdefault("task_skill_feedback", []).append(
            dict(
                skill_name=entry["name"],
                rating="positive" if verdict == "pass" else "negative",
                note=str(notes or ""),
                scope={k: v for k, v in ref.items() if k != "name"},
                skill_sha256=entry["skill_sha256"],
                source_event_id=key,
            )
        )
    return row


def learning_view(session, task_id):
    return [
        {
            "evidence_id": row.evidence_id,
            "run_id": row.run_id,
            "state": row.state,
            "notes": row.notes,
            "verdict": row.verdict,
            "result": json.loads(row.result_json or "{}"),
        }
        for row in session.scalars(
            select(SkillLearningEvidence)
            .where(SkillLearningEvidence.task_id == task_id)
            .order_by(SkillLearningEvidence.created_at)
        )
    ]


def _actor_membership(session, task, actor):
    from evoharness.organization.models import Membership
    from evoharness.organization.service import _live

    if task is None or actor not in {task.owner, task.assignee_id}:
        raise LearningError("learning_actor_not_authorized")
    members = list(
        session.scalars(
            select(Membership).where(
                Membership.org_id == task.org_id,
                Membership.tenant_id == task.tenant,
                Membership.principal_id == actor,
            )
        )
    )
    if len(members) != 1 or not _live(members[0], utcnow()):
        raise LearningError("learning_actor_not_authorized")
    return members[0]


def _context(
    session, evidence_id, actor, *, validate_sources=True, prepared_release_name=""
):
    row = session.get(SkillLearningEvidence, evidence_id)
    if row is None:
        raise LearningError("learning_evidence_not_found")
    task = session.get(Task, row.task_id)
    run = session.get(ExecutionRun, row.run_id)
    _actor_membership(session, task, actor)
    if (
        run is None
        or run.generation != row.generation
        or run.task_revision != row.task_revision
        or run.task_id != task.id
        or task.revision != row.task_revision
        or _context_digest(task, run) != row.context_sha256
    ):
        raise LearningError("learning_evidence_stale")
    if skill_state_read_only():
        raise LearningError("learning_holdout_read_only")
    if not validate_sources:
        return row, task, run, [], {}
    validate_run_skills(session, run, prepared_release_name=prepared_release_name)
    loaded = json.loads(row.skills_json)
    definitions, files = [], {}
    from agents.skills import _parse_skill_file

    for entry in loaded:
        ref = ScopedSkillRef(**entry["ref"])
        matches = [
            (file, meta)
            for file, meta, current in _sources(ref.tenant, ref.space, ref.owner)
            if current == ref
        ]
        if len(matches) != 1:
            raise LearningError("skill_context_changed")
        file, meta = matches[0]
        if hashlib.sha256(file.read_bytes()).hexdigest() != entry["file_sha256"]:
            raise LearningError("skill_context_changed")
        definitions.append(_parse_skill_file(file, "scoped", str(file.parent)))
        files[ref.name] = (file, meta, ref)
    if not definitions:
        raise LearningError("learning_requires_loaded_skill")
    return row, task, run, definitions, files


async def build_candidate(factory, evidence_id, *, actor, side_query=None):
    claim_token = uuid4().hex
    model_port = None
    with factory.begin() as session:
        row, task, run, definitions, files = _context(session, evidence_id, actor)
        if row.state in {"proposed", "adopted", "discarded"}:
            return json.loads(row.result_json)
        claimed = session.execute(
            update(SkillLearningEvidence)
            .where(
                SkillLearningEvidence.evidence_id == evidence_id,
                or_(
                    SkillLearningEvidence.state.in_(["pending", "failed"]),
                    and_(
                        SkillLearningEvidence.state == "creating",
                        SkillLearningEvidence.lease_until < utcnow(),
                    ),
                ),
            )
            .values(
                state="creating",
                claim_token=claim_token,
                lease_until=utcnow() + timedelta(minutes=5),
            )
        )
        if claimed.rowcount != 1:
            raise LearningError("learning_candidate_busy")
        answer = session.scalar(
            select(Artifact).where(Artifact.run_id == run.id, Artifact.kind == "answer")
        )
        assistant_text = ""
        if answer:
            from pathlib import Path

            data = Path(answer.path).read_bytes()
            if hashlib.sha256(data).hexdigest() != answer.sha256:
                raise LearningError("learning_artifact_changed")
            assistant_text = data.decode("utf-8")[:6000]
        messages = [
            {"role": "user", "content": task.goal or task.title},
            {"role": "assistant", "content": assistant_text},
            {"role": "user", "content": row.notes},
        ]
        attribution = dict(
            position_id=task.assigned_position, task_id=task.id, evidence_id=evidence_id
        )
        budget_scope = dict(
            org_id=task.org_id,
            space_id=task.space_id,
            principal_id=run.actor_id,
            work_item_id=task.id,
            run_id=run.id,
            category="skill_learning",
        )

    def check():
        with factory.begin() as session:
            current, _, _, _, _ = _context(session, evidence_id, actor)
            if (
                current.state != "creating"
                or current.claim_token != claim_token
                or current.lease_until is None
                or current.lease_until <= utcnow()
            ):
                raise LearningError("learning_claim_lost")

    try:
        if side_query is None:
            from evoharness.governance.budget_gateway import BudgetGateway
            from evoharness.work.agent_adapter import (
                GatewayModelPort,
                _native_model_config,
            )

            config = _native_model_config()
            if config is None:
                raise LearningError("learning_model_not_configured")
            model = config["model"]
            port = GatewayModelPort(
                BudgetGateway(factory), pre_check=check, budget_kwargs=budget_scope
            )
            model_port = port
            client = port.build_client(config)

            async def side_query(system, user):
                if config["use_openai"]:
                    response = await client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        max_tokens=1600,
                    )
                    return response.choices[0].message.content or ""
                response = await client.messages.create(
                    model=model,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    max_tokens=1600,
                )
                return "".join(
                    block.text
                    for block in response.content
                    if getattr(block, "type", "") == "text"
                )

        async def query(system, user):
            check()
            return await side_query(system, user)

        async def propose(*, action, target_skill, candidate, decision):
            if target_skill not in files:
                raise LearningError("learning_target_not_loaded")
            file, meta, ref = files[target_skill]
            instructions = (
                decision.get("revised_instructions")
                if action == "revise"
                else decision.get("merged_instructions")
            ) or candidate.instructions
            if not str(instructions or "").strip():
                raise LearningError("learning_instructions_required")
            return {
                "ok": True,
                "action": "propose",
                "skill": target_skill,
                "proposal": {
                    "name": target_skill,
                    "instructions": str(instructions),
                    "description": str(
                        decision.get("merged_description") or candidate.description
                    ),
                    "when_to_use": str(
                        decision.get("merged_when_to_use") or candidate.when_to_use
                    ),
                    "producer_kind": "direct",
                },
                "ref": asdict(ref),
                "previous_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                "previous_version": str(meta.get("version", "")),
                "decision": decision,
            }

        from agents.online_skill_evolution import (
            online_ingest,
            maintain_online_skill_candidate,
        )

        async def maintainer(**kwargs):
            return await maintain_online_skill_candidate(
                **kwargs, skill_catalog=definitions, write_candidate=propose,
                scoped_revision_only=True,
            )

        result = await online_ingest(
            messages=messages,
            side_query=query,
            retrieved_reference={"name": definitions[0].name},
            hint="Only this task and its authorized review are learning evidence.",
            attribution=attribution,
            maintainer=maintainer,
            provenance_writer=lambda **kwargs: None,
            authorized_review=True,
        )
        check()
        with factory.begin() as session:
            _context(session, evidence_id, actor)
            # The lease can change after check() releases its transaction.
            # Commit the result only while this exact claimant still owns it.
            completed = session.execute(
                update(SkillLearningEvidence)
                .where(
                    SkillLearningEvidence.evidence_id == evidence_id,
                    SkillLearningEvidence.state == "creating",
                    SkillLearningEvidence.claim_token == claim_token,
                    SkillLearningEvidence.lease_until > utcnow(),
                )
                .values(
                    result_json=json.dumps(result, ensure_ascii=False),
                    state=(
                        "proposed"
                        if result.get("proposal")
                        else "discarded" if result.get("ok") else "failed"
                    ),
                    claim_token="",
                    lease_until=None,
                )
            )
            if completed.rowcount != 1:
                raise LearningError("learning_claim_lost")
        return result
    except Exception:
        with factory.begin() as session:
            session.execute(
                update(SkillLearningEvidence)
                .where(
                    SkillLearningEvidence.evidence_id == evidence_id,
                    SkillLearningEvidence.state == "creating",
                    SkillLearningEvidence.claim_token == claim_token,
                )
                .values(state="failed", claim_token="", lease_until=None)
            )
        raise
    finally:
        if model_port is not None and model_port.real is not None:
            await model_port.real.close()


def adopt_candidate(session, evidence_id, *, actor):
    # Retries return the recorded receipt even though the original source has
    # since changed to the adopted version.
    existing = session.get(SkillLearningEvidence, evidence_id)
    if existing is not None and existing.state == "adopted":
        task = session.get(Task, existing.task_id)
        _actor_membership(session, task, actor)
        return json.loads(existing.result_json)
    row, task, run, _, _ = _context(session, evidence_id, actor, validate_sources=False)
    if row.state != "proposed":
        raise LearningError("learning_candidate_not_ready")
    result = json.loads(row.result_json)
    ref = ScopedSkillRef(**result["ref"])
    loaded = next(
        (
            entry
            for entry in json.loads(row.skills_json)
            if entry["ref"] == result["ref"] and entry["name"] == result["skill"]
        ),
        None,
    )
    if loaded is None:
        raise LearningError("learning_target_not_loaded")
    matches = [
        (file, meta)
        for file, meta, current in _sources(ref.tenant, ref.space, ref.owner)
        if current == ref
    ]
    if len(matches) != 1:
        raise LearningError("skill_context_changed")
    file, meta = matches[0]
    if meta.get("producer-kind") == "projection" or meta.get("managed-by"):
        raise LearningError("managed_projection_source_owned")
    from evoharness.organization.models import Membership
    from evoharness.organization.service import _live
    from evoharness.clock import utcnow

    members = list(
        session.scalars(
            select(Membership).where(
                Membership.org_id == task.org_id,
                Membership.tenant_id == task.tenant,
                Membership.principal_id == actor,
            )
        )
    )
    if (
        len(members) != 1
        or not _live(members[0], utcnow())
        or (
            actor != ref.owner
            and "organization.manage" not in json.loads(members[0].grants_json)
        )
    ):
        raise LearningError("learning_publish_not_authorized")
    from .retrieval import _reader
    from .skill_scope_events import latest_scope_event, scope_denial

    _reader(
        session,
        actor_id=run.actor_id,
        tenant=run.tenant,
        org_id=task.org_id,
        space_id=task.space_id,
    )
    scope_event = latest_scope_event(
        session,
        skill_name=ref.name,
        tenant=ref.tenant,
        space=ref.space,
        owner=ref.owner,
    )
    if scope_denial(
        session,
        skill_name=ref.name,
        tenant=ref.tenant,
        space=ref.space,
        owner=ref.owner,
        revision=ref.source_revision,
    ) or (scope_event.id if scope_event else 0) != loaded.get("scope_generation", 0):
        raise LearningError("skill_context_changed")
    from .release import (
        publish_skill_bundle,
        recover_skill_bundle,
        prepared_skill_bundle,
    )

    publication = dict(
        skill_name=ref.name,
        bundle=result["proposal"],
        expected_version=result["previous_version"],
        expected_active_sha256=result["previous_sha256"],
        actor=actor,
        active_dir=str(file.parent),
        scope={k: v for k, v in asdict(ref).items() if k != "name"},
        release_id=evidence_id,
    )
    receipt = recover_skill_bundle(**publication)
    if receipt is None:
        prepared = prepared_skill_bundle(**publication)
        _context(
            session,
            evidence_id,
            actor,
            prepared_release_name=ref.name if prepared else "",
        )
        receipt = publish_skill_bundle(
            **publication, lesson="task-learning:" + evidence_id, reason=row.notes
        )
    if not receipt.get("ok"):
        raise LearningError(str(receipt.get("error") or "learning_publish_failed"))
    result["publication"] = receipt
    row.result_json = json.dumps(result, ensure_ascii=False)
    row.state = "adopted"
    session.flush()
    return result
