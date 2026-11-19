import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from evoharness.clock import utcnow
from evoharness.models import Base
from evoharness.organization import service
from evoharness.organization.models import BusinessSpace, Membership, Organization, OrganizationUnit


NAMESPACE = "workspace"
COOKIE = "__Secure-evo_workspace"
SESSION_DURATION = timedelta(hours=12)
INVITE_DURATION = timedelta(days=1)
MEMBER_GRANTS = {"work.execute", "knowledge.read"}
OWNER_GRANTS = MEMBER_GRANTS | {"organization.manage", "work.approve", "knowledge.write"}
_PASSWORD_SLOTS = threading.BoundedSemaphore(2)


class Account(Base):
    __tablename__ = "workspace_accounts"

    username: Mapped[str] = mapped_column(String(48), primary_key=True)
    subject: Mapped[str] = mapped_column(String(52), unique=True)
    password_hash: Mapped[str] = mapped_column(String(150))
    can_bootstrap: Mapped[bool] = mapped_column(default=False)
    failures: Mapped[int] = mapped_column(default=0)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Session(Base):
    __tablename__ = "workspace_sessions"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(ForeignKey(Account.username), index=True)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Invitation(Base):
    __tablename__ = "workspace_invitations"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(52), default="")
    issuer: Mapped[str] = mapped_column(String(52), default="")
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _password(password, salt):
    if not _PASSWORD_SLOTS.acquire(blocking=False):
        raise ValueError("try_again_later")
    try:
        return hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1,
                              maxmem=192 * 1024 * 1024, dklen=32).hex()
    finally:
        _PASSWORD_SLOTS.release()


def _id(prefix):
    return f"{prefix}-{uuid4().hex}"


def _actor(subject, authorities=()):
    return service.AuthenticatedPrincipal(principal_id=subject, tenant_id=NAMESPACE,
                                          app_id=NAMESPACE, authenticated=True,
                                          authorities=frozenset(authorities))


def _members(db, subject):
    return list(db.scalars(select(Membership).join(Organization, Organization.org_id == Membership.org_id)
                           .where(Membership.principal_id == subject, Membership.tenant_id == NAMESPACE,
                                  Membership.app_id == NAMESPACE, Organization.enabled.is_(True))))


def identify(db, token):
    if not token or len(token) > 128:
        return None
    row = db.scalar(select(Account).join(Session, Session.username == Account.username)
                    .where(Session.digest == _digest(token), Session.expires > utcnow()))
    if not row:
        return None
    now = utcnow()
    return {"subject": row.subject, "username": row.username, "tenant": NAMESPACE, "admin": False,
            "tenant_id": NAMESPACE, "app_id": NAMESPACE,
            "can_bootstrap": row.can_bootstrap,
            "org_ids": sorted({member.org_id for member in _members(db, row.subject)
                               if service._live(member, now)})}


def issue_invitation(db, org_id="", actor=None):
    if org_id:
        if not actor:
            raise ValueError("organization_management_not_granted")
        service._manager(db, _actor(actor), org_id)
    token = secrets.token_urlsafe(32)
    db.execute(delete(Invitation).where(Invitation.expires <= utcnow()))
    db.add(Invitation(digest=_digest(token), org_id=org_id, issuer=actor or "",
                      expires=utcnow() + INVITE_DURATION))
    return token


def _new_session(db, username, previous):
    now = utcnow()
    db.execute(delete(Session).where(Session.expires <= now))
    logout(db, previous)
    token = secrets.token_urlsafe(32)
    db.add(Session(digest=_digest(token), username=username, expires=now + SESSION_DURATION))
    return token


def register(db, username, password, code, previous=""):
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,47}", username):
        raise ValueError("invalid_username")
    if not 14 <= len(password) <= 128:
        raise ValueError("invalid_password")
    code = code.strip()
    if len(code) != 43:
        raise ValueError("invitation_invalid")
    digest = _digest(code)
    invite = db.scalar(select(Invitation).where(Invitation.digest == digest, Invitation.expires > utcnow()))
    if invite is None:
        raise ValueError("invitation_invalid")
    if db.get(Account, username) is not None:
        raise ValueError("username_unavailable")
    salt = secrets.token_bytes(16)
    password_hash = salt.hex() + ":" + _password(password, salt)
    deleted = db.execute(delete(Invitation).where(Invitation.digest == digest, Invitation.expires > utcnow()))
    if deleted.rowcount != 1:
        raise ValueError("invitation_invalid")
    account = Account(username=username, subject=_id("web"), password_hash=password_hash,
                      can_bootstrap=not invite.org_id)
    db.add(account)
    db.flush()
    if invite.org_id:
        actor = _actor(invite.issuer)
        member = service.add_member(db, actor, org_id=invite.org_id, membership_id=_id("member"),
                                    principal_id=account.subject, tenant_id=NAMESPACE, app_id=NAMESPACE,
                                    grants=MEMBER_GRANTS, roles=("member",))
        service.create_employee(db, actor, org_id=invite.org_id, employee_id=_id("emp"),
                                membership_id=member.membership_id, display_name=username)
    return _new_session(db, username, previous)


def login(db, username, password, previous=""):
    if len(username) > 48 or not 1 <= len(password) <= 128:
        return None
    account = db.get(Account, username.strip().lower())
    now = utcnow()
    if account and account.retry_at and service._time(account.retry_at) > now:
        return None
    salt, expected = account.password_hash.split(":") if account else ("0" * 32, "0" * 64)
    valid = hmac.compare_digest(_password(password, bytes.fromhex(salt)), expected)
    if not account:
        return None
    if not valid:
        account.failures += 1
        if account.failures >= 5:
            account.failures, account.retry_at = 0, now + timedelta(minutes=1)
        return None
    account.failures, account.retry_at = 0, None
    return _new_session(db, account.username, previous)


def logout(db, token):
    if token and len(token) <= 128:
        db.execute(delete(Session).where(Session.digest == _digest(token)))


def create_org(db, subject, title):
    from evoharness.governance import budget

    actor = _actor(subject, OWNER_GRANTS | {"organization.bootstrap"})
    title = title.strip()
    if not 1 <= len(title) <= 80:
        raise ValueError("invalid_organization_name")
    org_id = _id("org")
    service.bootstrap_organization(db, actor, org_id=org_id, title=title,
                                   membership_id=_id("member"), budget_limit=100_000)
    space = service.create_space(db, actor, org_id=org_id, space_id=_id("space"), title="协作空间")
    unit = service.create_unit(db, actor, org_id=org_id, unit_id=_id("unit"), title="交付组")
    service.bind_space(db, actor, org_id=org_id, unit_id=unit.unit_id, space_id=space.space_id)
    budget.set_limit(db, scope_kind="org", scope_id=org_id, limit_tokens=100_000)
    budget.set_limit(db, scope_kind="space", scope_id=space.space_id, limit_tokens=100_000)
    return org_id


def add_ai(db, subject, org_id, name):
    actor = _actor(subject)
    name = name.strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("invalid_employee_name")
    service._manager(db, actor, org_id)
    unit = db.scalar(select(OrganizationUnit).where(OrganizationUnit.org_id == org_id)
                     .order_by(OrganizationUnit.unit_id).limit(1))
    space = db.scalar(select(BusinessSpace).where(BusinessSpace.org_id == org_id, BusinessSpace.enabled.is_(True))
                      .order_by(BusinessSpace.space_id).limit(1))
    if not unit or not space:
        raise ValueError("organization_setup_incomplete")
    position = service.create_position(db, actor, org_id=org_id, unit_id=unit.unit_id,
                                       position_id=_id("position"), title=name,
                                       mission="根据任务与证据交付", actions=MEMBER_GRANTS, budget_limit=10_000)
    member = service.add_member(db, actor, org_id=org_id, membership_id=_id("member"), principal_id=_id("ai"),
                                tenant_id=NAMESPACE, app_id=NAMESPACE, grants=MEMBER_GRANTS,
                                principal_kind="ai_service")
    employee = service.create_employee(db, actor, org_id=org_id, employee_id=_id("emp"),
                                       membership_id=member.membership_id, display_name=name)
    service.assign(db, actor, org_id=org_id, assignment_id=_id("assignment"),
                   position_id=position.position_id, employee_id=employee.employee_id,
                   space_ids={space.space_id}, actions=MEMBER_GRANTS, budget_limit=10_000)
    return member.principal_id


def organizations(db, principal):
    result = []
    for row in db.scalars(select(Organization).where(Organization.org_id.in_(principal["org_ids"]))
                          .order_by(Organization.title, Organization.org_id)):
        try:
            service._manager(db, _actor(principal["subject"]), row.org_id)
            manage = True
        except service.OrganizationError:
            manage = False
        result.append({"id": row.org_id, "name": row.title, "manage": manage})
    return result


def delegate(api, principal, task_id, employee):
    from evoharness.models import Task

    with api._session() as db:
        task = db.get(Task, task_id)
        if (not task or task.tenant != NAMESPACE or task.owner != principal["subject"]
                or task.org_id not in principal["org_ids"]):
            return api._fail("principal_target_not_authorized")
        org_id = task.org_id
        members = [member for member in _members(db, employee)
                   if member.org_id == org_id and member.principal_kind == "ai_service"
                   and service._live(member, utcnow())]
        if len(members) != 1:
            return api._fail("employee_not_available")
        space_id = task.space_id
    plan = api.run_request_payload(task_id, principal=principal)
    if not plan["ok"]:
        return plan
    approved = api.submit_approval(org_id=org_id, target_type="work_item", target_id=str(task_id),
                                   target_revision=plan["data"]["target_revision"], payload=plan["data"]["payload"],
                                   actions=["work.execute"], grantee_id=employee, principal=principal)
    if not approved["ok"]:
        return approved
    try:
        ensure_delivery_skill(employee, space_id)
    except OSError:
        return api._fail("workspace_skill_store_unavailable")
    return api.start_run(task_id, brief=plan["data"]["payload"], actor_id=employee,
                         principal={"subject": employee, "org_ids": [org_id], "tenant": NAMESPACE,
                                    "tenant_id": NAMESPACE, "app_id": NAMESPACE})


def ensure_delivery_skill(employee, space_id):
    if not re.fullmatch(r"ai-[0-9a-f]{32}", employee) or not re.fullmatch(r"space-[0-9a-f]{32}", space_id):
        raise OSError("invalid workspace skill scope")
    configured = os.getenv("EVOHARNESS_SKILLS_ROOT", "").strip()
    root = Path(configured) if configured else Path.home() / ".evoharness" / "skills"
    directory = root / f"delivery-{employee}-{space_id}"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "SKILL.md"
    if target.is_file():
        return
    scope = json.dumps({"tenant": NAMESPACE, "space": space_id, "owner": employee,
                        "producer": "workspace-starter", "source_revision": 1})
    staged = directory / f".delivery-{uuid4().hex}.tmp"
    stream = staged.open("x", encoding="utf-8")
    try:
        with stream:
            stream.write("---\nname: delivery-review\ndescription: 执行组织交付任务前读取的交付 SOP。\n"
                         f"version: 1.0.0\nscope-json: {scope}\n---\n"
                         "核对任务目标和验收标准。使用可核实的来源；区分已核实事实与假设。"
                         "输出可验收的答复并说明依据和缺口。\n")
        # Publish the complete file only if no other writer has installed it.
        try:
            os.link(staged, target)
        except FileExistsError:
            if not target.is_file():
                raise
    finally:
        staged.unlink(missing_ok=True)


def main():
    from evoharness.application import Application
    from evoharness.config import resolve_config

    parser = argparse.ArgumentParser(description="发放一次性工作区开通邀请码（管理员）")
    parser.add_argument("--db", default="")
    args = parser.parse_args()
    app = Application.open(args.db or resolve_config().database_url)
    with app.transaction() as db:
        invite = issue_invitation(db)
    print(invite)
