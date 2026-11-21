"""Member skill projection: fresh, fully bound sources; no global score fallback."""
from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import select

from agents.frontmatter import parse_frontmatter
from agents.skill_evolution import load_skill_stats_scoped
from agents.skill_scope import ScopedSkillRef
from evoharness.clock import utcnow
from evoharness.knowledge.skill_scope_events import latest_scope_event, scope_denial
from evoharness.organization.models import BusinessSpace, Membership, Organization
from evoharness.organization.service import _live


class ScopedSkillsError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _roots():
    override = os.environ.get("EVOHARNESS_SKILLS_ROOT", "").strip()
    candidates = ([Path(override)] if override else
                  [Path.home() / ".evoharness" / "skills",
                   Path.cwd() / ".evoharness" / "skills"])
    return list(dict.fromkeys(root.resolve() for root in candidates))


def _sources(tenant, space, owner):
    """Parse each source once, without the legacy name-only discovery cache."""
    seen = set()
    for root in _roots():
        if not root.is_dir():
            continue
        for file in sorted(root.glob("*/SKILL.md")):
            try:
                if not file.resolve().is_relative_to(root):
                    continue
                parsed = parse_frontmatter(file.read_text(encoding="utf-8-sig"))
                scope = json.loads(parsed.meta.get("scope-json") or "null")
                if not isinstance(scope, dict):
                    continue
                if any(scope.get(k) != v for k, v in
                       (("tenant", tenant), ("space", space), ("owner", owner))):
                    continue
                producer = scope.get("producer")
                revision = scope.get("source_revision")
                if not isinstance(producer, str) or not producer.strip():
                    continue
                if type(revision) is not int or revision < 0:
                    continue
                name = parsed.meta.get("name") or file.parent.name
                ref = ScopedSkillRef(name=name, tenant=tenant, space=space,
                                     owner=owner, producer=producer,
                                     source_revision=revision)
                if ref.cache_key() in seen:
                    continue
                seen.add(ref.cache_key())
                yield file, parsed.meta, ref
            except (OSError, UnicodeError, ValueError, TypeError):
                # Broken or unbound files are not authority for a member view.
                continue


def skills_view(session, *, org_id="", space_id="", tenant="", owner="",
                legacy=False, principal=None):
    admin = principal is None or bool(principal.get("admin"))
    if legacy and not admin:
        raise ScopedSkillsError("principal_target_not_authorized")
    if legacy or (not org_id and admin):
        return {"mode": "legacy", "capabilities": {"legacy_tools": True}}
    if not org_id:
        raise ScopedSkillsError("organization_required")
    if principal is not None and not admin:
        if org_id not in (principal.get("org_ids") or []):
            raise ScopedSkillsError("principal_org_not_authorized")
        # Form/query identity is never used as hosted authority.
        owner = str(principal.get("subject") or "").strip()
        tenant = str(principal.get("tenant_id") or "").strip()
    elif principal is not None:
        owner = owner or str(principal.get("subject") or "").strip()
        tenant = str(principal.get("tenant_id") or tenant).strip()
    if not owner:
        raise ScopedSkillsError("principal_required")
    org = session.get(Organization, org_id)
    if org is None or not org.enabled:
        raise ScopedSkillsError("principal_org_not_authorized")
    now = utcnow()
    memberships = list(session.scalars(select(Membership).where(
        Membership.org_id == org_id, Membership.principal_id == owner)))
    # A scope ref has no app field. Same-tenant multi-app ownership is ambiguous
    # even when the HTTP token identifies one app; never merge its files/scores.
    if tenant:
        memberships = [m for m in memberships if m.tenant_id == tenant]
    active = [m for m in memberships if _live(m, now)]
    if len(active) != 1:
        raise ScopedSkillsError("skill_namespace_ambiguous" if len(active) > 1
                                else "membership_not_active")
    member = active[0]
    if len({m.app_id for m in memberships if m.tenant_id == member.tenant_id}) > 1:
        # Inactive memberships still own historical files. A new app cannot
        # inherit them merely because its membership is the only active one.
        raise ScopedSkillsError("skill_namespace_ambiguous")
    trusted_app = str((principal or {}).get("app_id") or "").strip()
    if trusted_app and trusted_app != member.app_id:
        raise ScopedSkillsError("principal_namespace_mismatch")
    tenant = member.tenant_id
    if not tenant:
        raise ScopedSkillsError("skill_namespace_unbound")
    spaces = list(session.scalars(select(BusinessSpace).where(
        BusinessSpace.org_id == org_id, BusinessSpace.enabled.is_(True)).order_by(
            BusinessSpace.space_id)))
    data = {"mode": "scoped", "capabilities": {"legacy_tools": admin},
            "scope": None, "spaces": [{"space_id": s.space_id, "title": s.title}
                                      for s in spaces],
            "has_report": False, "skills": [], "events": []}
    if not space_id:
        return data
    if space_id not in {s.space_id for s in spaces}:
        raise ScopedSkillsError("space_not_authorized")
    data["scope"] = {"org_id": org_id, "space_id": space_id, "tenant": tenant,
                     "owner": owner, "app_id": member.app_id}
    from agents.scoped_skill_report import read_scoped_skill_report
    # EVO-01/DE-06：技能状态只读本域覆盖层（skill_status_scoped.json），
    # 不回落 base/全局评测产物——无覆盖条目如实为空。
    from agents.skill_status import get_status_scoped_overlay
    statuses = get_status_scoped_overlay({"tenant": tenant, "space": space_id,
                                          "owner": owner})
    for file, meta, ref in _sources(tenant, space_id, owner):
        if scope_denial(session, skill_name=ref.name, tenant=tenant,
                        space=space_id, owner=owner, revision=ref.source_revision):
            continue
        scope = {"tenant": tenant, "space": space_id, "owner": owner,
                 "producer": ref.producer, "source_revision": ref.source_revision}
        stats = load_skill_stats_scoped(scope).get(ref.name, {})
        event = latest_scope_event(session, skill_name=ref.name, tenant=tenant,
                                   space=space_id, owner=owner)
        report = read_scoped_skill_report(ref, file,
                                         scope_generation=event.id if event else 0)
        evaluation = None
        if report is not None:
            outcomes = report["outcomes"]
            evaluation = {"protocol_id": report["protocol_id"],
                          "run_id": report["run_id"],
                          "samples": len({row["sample_id"] for row in outcomes}),
                          "checks": len(outcomes),
                          "passed": sum(row["passed"] for row in outcomes),
                          "pass_rate": report["pass_rate"]}
            data["has_report"] = True
        data["skills"].append({"skill": ref.name,
            "description": str(meta.get("description") or ""),
            "status": statuses.get(ref.name, ""),
            "scope_ref": {"name": ref.name, **scope},
            "counts": {key: stats.get(key, 0) for key in
                       ("invocations", "feedback", "evolutions", "snapshots")},
            "evaluation": evaluation})
    return data
