"""Source-owned, permission-preserving Experience projections.

Normal mutation hooks publish only after the source transaction commits. Failed
projections retain a content-free rebuild request; sync_all is the repair path.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from app.core.clock import utcnow
from app.memory import skill_bridge
from app.memory.projection_scope import ProjectionScope, default_scope, root_for, atomic_json
from app.models import Experience, Message, Source

logger = logging.getLogger(__name__)
PROJECTABLE_STATUSES = {"approved_auto", "assist", "candidate"}
PENDING_KEY = "experience_projection_requests"


def domain_of(experience: Experience) -> str:
    text = " ".join(filter(None, [experience.question_pattern or "", experience.keywords or "", experience.canonical_answer or ""]))
    return skill_bridge.classify_domain(text, experience.category or "")


def _as_dict(experience: Experience) -> dict:
    audience = experience.audience()
    return dict(id=experience.id, question=experience.question_pattern,
        answer=experience.canonical_answer, steps=experience.steps,
        preconditions=experience.preconditions, negative_examples=experience.negative_examples(),
        status=experience.status, hit_count=experience.hit_count, category=experience.category,
        tenant=experience.tenant, owner=experience.owner,
        space_id=getattr(experience, "space_id", "") or audience.get("space_id", ""),
        audience=audience, visibility=experience.visibility or "private",
        sensitive_level=experience.sensitive_level or "private",
        version=experience.version, source_refs=experience.source_message_ids(),
        producer_kind="projection", managed_by="experience.skill_sync")


def _source_allows(session: Session, experience: Experience) -> bool:
    for message_id in experience.source_message_ids():
        message = session.scalar(select(Message).where(Message.tenant == experience.tenant,
            Message.message_id == message_id, Message.recalled.is_(False)))
        if message is None or message.source_id is None:
            return False
        source = session.scalar(select(Source).where(Source.id == message.source_id,
            Source.tenant == experience.tenant, Source.owner == experience.owner,
            Source.enabled.is_(True), Source.revoked_at.is_(None)))
        if source is None or message.msg_type not in source.allowed_type_set:
            return False
    return True


def _requests(session: Session, domain: str, tenant: str) -> dict[ProjectionScope, list[dict]]:
    rows = list(session.scalars(select(Experience).where(Experience.tenant == tenant)))
    selected = {}
    scopes = set()
    for exp in rows:
        if domain_of(exp) != domain:
            continue
        try:
            scope = ProjectionScope.from_payload(_as_dict(exp))
        except ValueError:
            continue
        scopes.add(scope)
        key = (scope, exp.intent_id or str(exp.id))
        previous = selected.get(key)
        if previous is None or (int(exp.version or 1), exp.id) > (int(previous.version or 1), previous.id):
            selected[key] = exp
    grouped = {scope: [] for scope in scopes}
    for (scope, _), exp in selected.items():
        if exp.status not in PROJECTABLE_STATUSES or (exp.valid_until is not None and exp.valid_until <= utcnow()):
            continue
        if _source_allows(session, exp):
            grouped[scope].append(_as_dict(exp))
    return grouped


def _queue_path(scope: ProjectionScope, domain: str) -> Path:
    return root_for(scope).parent / ".projection-rebuild" / (scope.key + "-" + skill_bridge.domain_slug(domain) + ".json")


def _execute(domain: str, groups: dict[ProjectionScope, list[dict]], actor: str) -> dict:
    results = []
    for scope, payload in groups.items():
        queue = _queue_path(scope, domain)
        try:
            result = skill_bridge.sync_domain(domain, payload, actor=actor, scope=scope).as_dict()
            if result["action"] == "skipped" and result["detail"].startswith("holdout read-only"):
                results.append(result)
                continue
            if result["action"] == "skipped":
                raise RuntimeError(result["detail"])
            queue.unlink(missing_ok=True)
        except Exception as exc:
            # No answers or questions in the operational rebuild queue.
            try:
                atomic_json(queue, dict(domain=domain, scope=scope.as_dict(), state="pending",
                    source_ids=[item["id"] for item in payload], error=type(exc).__name__))
            except OSError:
                logger.error("Projection rebuild queue unavailable for domain: %s", domain)
            result = dict(domain=domain, action="queued_rebuild", detail=type(exc).__name__, experienceIds=[])
            logger.warning("Experience projection deferred for rebuild: %s", domain)
        results.append(result)
    if len(results) == 1:
        return results[0]
    return dict(domain=domain, action="partitioned", partitions=results)


def sync_domain(session: Session, domain: str, *, actor: str = "feishu", tenant: str | None = None) -> dict:
    tenant = tenant or default_scope().tenant
    groups = _requests(session, domain, tenant)
    session.info.setdefault(PENDING_KEY, {})[(tenant, domain)] = (domain, groups, actor)
    return {"domain": domain, "action": "after_commit"}


def _known_scopes(tenant: str) -> dict[str, set[ProjectionScope]]:
    import os
    known = {}
    root = root_for(default_scope())
    roots = [root]
    explicit = os.environ.get("EVOHARNESS_SKILLS_ROOT", "").strip()
    container = Path(explicit).resolve().parent / "knowledge-projections" if explicit else root.parents[1]
    if container.is_dir():
        roots.extend(container.glob("*/skills"))
    for projection_root in roots:
        for manifest in projection_root.glob("feishu-*/.projection.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                scope = ProjectionScope.from_payload(data["scope"])
            except (ValueError, KeyError, OSError):
                logger.warning("Projection manifest requires repair: %s", manifest.name)
                continue
            if scope.tenant == tenant:
                known.setdefault(data["domain"], set()).add(scope)
    return known


def _all_requests(session: Session, tenant: str) -> list[tuple[str, dict]]:
    rows = list(session.scalars(select(Experience).where(Experience.tenant == tenant)))
    known = _known_scopes(tenant)
    result = []
    for domain in sorted({domain_of(exp) for exp in rows} | set(known)):
        groups = _requests(session, domain, tenant)
        for scope in known.get(domain, set()):
            groups.setdefault(scope, [])
        result.append((domain, groups))
    return result


def sync_all(session: Session, *, actor: str = "feishu", tenant: str | None = None) -> list[dict]:
    tenant = tenant or default_scope().tenant
    results = []
    for domain, groups in _all_requests(session, tenant):
        session.info.setdefault(PENDING_KEY, {})[(tenant, domain)] = (domain, groups, actor)
        results.append({"domain": domain, "action": "after_commit"})
    return results


def sync_after_change(session: Session, experience: Experience | None, *, actor: str = "feishu") -> dict | None:
    if experience is None:
        return None
    domain = domain_of(experience)
    groups = _requests(session, domain, experience.tenant)
    session.info.setdefault(PENDING_KEY, {})[(experience.tenant, domain)] = (domain, groups, actor)
    return {"domain": domain, "action": "after_commit"}


def _after_commit(session: Session) -> None:
    for domain, groups, actor in session.info.pop(PENDING_KEY, {}).values():
        _execute(domain, groups, actor)


def _after_rollback(session: Session) -> None:
    session.info.pop(PENDING_KEY, None)
    session.info.pop("projection_touched_tenants", None)


if not event.contains(Session, "after_commit", _after_commit):
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)


# Source revocation/deletion and direct Experience edits must invalidate files too.
# Query after flush so the snapshot includes the actual new DB state; write after commit.
def _before_flush(session: Session, flush_context, instances) -> None:
    tenants = session.info.setdefault("projection_touched_tenants", set())
    for value in session.new | session.dirty | session.deleted:
        if isinstance(value, (Source, Experience, Message)):
            tenants.add(value.tenant or default_scope().tenant)


def _after_flush(session: Session, flush_context) -> None:
    for tenant in session.info.pop("projection_touched_tenants", set()):
        for domain, groups in _all_requests(session, tenant):
            session.info.setdefault(PENDING_KEY, {})[(tenant, domain)] = (domain, groups, "source-lifecycle")


if not event.contains(Session, "before_flush", _before_flush):
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_flush_postexec", _after_flush)
