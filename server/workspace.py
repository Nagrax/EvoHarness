import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from evoharness.organization import access


PREFIX = "/workspace"
ORIGIN = os.environ.get("EVOHARNESS_WORKSPACE_ORIGIN", "").rstrip("/")
PUBLIC_API = {"/api/access/login", "/api/access/register"}
ACCOUNT_API = {"/api/access/me", "/api/access/logout", "/api/access/organization",
               "/api/access/invitation", "/api/access/employee", "/api/access/delegate",
               "/api/access/artifact", "/api/access/memory", "/api/access/feishu/task"}


class Credentials(BaseModel):
    username: str = Field(max_length=48)
    password: str = Field(max_length=128)
    invitation: str = Field(default="", max_length=128)


class Setup(BaseModel):
    name: str = Field(default="", max_length=80)
    org_id: str = Field(default="", max_length=52)


class Delegation(BaseModel):
    task_id: int = Field(gt=0)
    employee: str = Field(max_length=52)


class Memory(BaseModel):
    task_id: int = Field(gt=0)
    subject: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=4000)


class FeishuTask(BaseModel):
    org_id: str = Field(max_length=52)
    space_id: str = Field(max_length=52)
    message_id: str = Field(max_length=100)
    title: str = Field(min_length=1, max_length=120)
    criteria: str = Field(min_length=1, max_length=1000)
    reviewer: str = Field(default="", max_length=52)


def read_artifact(artifact, run):
    if artifact.generation != run.generation or artifact.task_revision != run.task_revision:
        raise ValueError("artifact_stale")
    file = Path(artifact.path).resolve()
    if not file.is_relative_to(Path(run.output_root).resolve()):
        raise ValueError("artifact_scope_invalid")
    with file.open("rb") as stream:
        data = stream.read(256 * 1024 + 1)
    if len(data) > 256 * 1024 or hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError("artifact_size_or_integrity")
    return data.decode("utf-8", errors="replace")


def error(code, status):
    return JSONResponse({"ok": False, "reason_code": code}, status_code=status)


def install(app, get_api, frontend: Path):
    url = urlsplit(ORIGIN)
    if url.scheme != "https" or not url.hostname or url.path or url.query or url.fragment or url.username:
        raise ValueError("EVOHARNESS_WORKSPACE_ORIGIN must be an HTTPS origin")

    def identity(token):
        if not token:
            return None
        with get_api()._session() as db:
            return access.identify(db, token)

    @app.middleware("http")
    async def workspace_boundary(request: Request, call_next):
        path, method = request.url.path, request.method
        if path not in {"/", "/access", "/api/health"} | PUBLIC_API | ACCOUNT_API and not path.startswith("/api/work/"):
            return error("workspace_route_not_found", 404)
        if method != "GET" and method != "HEAD" and (
            request.headers.get("origin") != ORIGIN
            or request.headers.get("content-type", "").split(";", 1)[0] != "application/json"
        ):
            return error("workspace_same_origin_required", 403)
        if path not in PUBLIC_API and path != "/api/health":
            request.state.workspace_principal = await run_in_threadpool(identity, request.cookies.get(access.COOKIE))
            if request.state.workspace_principal is None:
                if path == "/":
                    return RedirectResponse(PREFIX + "/access", status_code=303)
                if path != "/access":
                    return error("workspace_login_required", 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/access", include_in_schema=False)
    def access_page():
        return FileResponse(frontend / "workspace-access.html")

    def authenticate(request, credentials, *, signup):
        try:
            with get_api()._session() as db:
                args = (db, credentials.username, credentials.password)
                previous = request.cookies.get(access.COOKIE, "")
                token = (access.register(*args, credentials.invitation, previous)
                         if signup else access.login(*args, previous))
        except IntegrityError:
            return error("registration_conflict", 409)
        except ValueError as exc:
            code = getattr(exc, "code", str(exc))
            return error(code, 429 if code == "try_again_later" else 400)
        if not token:
            return error("invalid_credentials", 401)
        response = JSONResponse({"ok": True})
        response.set_cookie(access.COOKIE, token, path=PREFIX + "/", httponly=True, secure=True,
                            samesite="strict", max_age=int(access.SESSION_DURATION.total_seconds()))
        return response

    @app.post("/api/access/login", include_in_schema=False)
    def login(request: Request, body: Credentials):
        return authenticate(request, body, signup=False)

    @app.post("/api/access/register", include_in_schema=False)
    def register(request: Request, body: Credentials):
        return authenticate(request, body, signup=True)

    @app.get("/api/access/me", include_in_schema=False)
    def me(request: Request):
        from server import workspace_feishu

        principal = request.state.workspace_principal
        with get_api()._session() as db:
            organizations = access.organizations(db, principal)
            for org in organizations:
                org["feishu_spaces"] = workspace_feishu.spaces(db, org["id"]) if org["manage"] else []
            return {"ok": True, "data": {**principal, "organizations": organizations}}

    @app.post("/api/access/logout", include_in_schema=False)
    def logout(request: Request):
        with get_api()._session() as db:
            access.logout(db, request.cookies.get(access.COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(access.COOKIE, path=PREFIX + "/", secure=True, httponly=True, samesite="strict")
        return response

    def setup(request, body, op):
        subject = request.state.workspace_principal["subject"]
        try:
            with get_api()._session() as db:
                if op == "organization":
                    if not request.state.workspace_principal["can_bootstrap"]:
                        return error("organization_bootstrap_not_granted", 403)
                    data = {"org_id": access.create_org(db, subject, body.name)}
                elif op == "invitation":
                    if not body.org_id:
                        return error("organization_required", 400)
                    data = {"invitation": access.issue_invitation(db, body.org_id, actor=subject)}
                else:
                    data = {"employee": access.add_ai(db, subject, body.org_id, body.name)}
        except ValueError as exc:
            code = getattr(exc, "code", str(exc))
            return error(code, 403 if code.endswith(("not_granted", "not_active")) else 400)
        return {"ok": True, "data": data}

    @app.post("/api/access/organization", include_in_schema=False)
    def create_org(request: Request, body: Setup):
        return setup(request, body, "organization")

    @app.post("/api/access/invitation", include_in_schema=False)
    def invite(request: Request, body: Setup):
        return setup(request, body, "invitation")

    @app.post("/api/access/employee", include_in_schema=False)
    def add_ai(request: Request, body: Setup):
        return setup(request, body, "employee")

    @app.post("/api/access/delegate", include_in_schema=False)
    def delegate(request: Request, body: Delegation):
        return access.delegate(get_api(), request.state.workspace_principal, body.task_id, body.employee)

    @app.post("/api/access/feishu/task", include_in_schema=False)
    def feishu_task(request: Request, body: FeishuTask):
        from server import workspace_feishu

        try:
            return workspace_feishu.import_task(get_api(), request.state.workspace_principal, **body.model_dump())
        except ValueError as exc:
            return error(getattr(exc, "code", str(exc)), 403)

    @app.get("/api/access/artifact", include_in_schema=False)
    def artifact(request: Request, artifact_id: str):
        from evoharness.models import Task
        from evoharness.work.models import Artifact, ExecutionRun

        api = get_api()
        with api._session() as db:
            row = db.get(Artifact, artifact_id)
            run = db.get(ExecutionRun, row.run_id) if row else None
            task = db.get(Task, run.task_id) if run else None
            if (not task or row.kind not in {"answer", "evidence"}
                    or api._principal_task_denial(db, request.state.workspace_principal, task)):
                return error("artifact_not_available", 404)
            try:
                return {"ok": True, "data": {"text": read_artifact(row, run), "sha256": row.sha256}}
            except (ValueError, OSError):
                return error("artifact_not_available", 409)

    @app.post("/api/access/memory", include_in_schema=False)
    def memory(request: Request, body: Memory):
        from sqlalchemy import select
        from evoharness.knowledge.models import KnowledgeClaim
        from evoharness.knowledge.service import record_claim
        from evoharness.models import Task, TaskEvent
        from evoharness.work.models import Artifact, ExecutionRun

        api = get_api()
        subject, value = body.subject.strip(), body.value.strip()
        principal = request.state.workspace_principal
        if not subject or not value:
            return error("memory_subject_and_value_required", 400)
        with api._session() as db:
            task = db.get(Task, body.task_id)
            if not task or api._principal_task_denial(db, principal, task, write=True):
                return error("principal_target_not_authorized", 403)
            if task.status != "done":
                return error("memory_requires_accepted_task", 409)
            event = db.scalar(select(TaskEvent).where(TaskEvent.task_id == task.id,
                              TaskEvent.event_type == "acceptance_passed").order_by(TaskEvent.id.desc()).limit(1))
            accepted = json.loads(event.evidence_message_ids) if event else []
            run = db.get(ExecutionRun, accepted[0][4:]) if len(accepted) == 1 and accepted[0].startswith("run:") else None
            if run is None or run.task_id != task.id or run.task_revision != task.revision:
                return error("memory_requires_accepted_run", 409)
            rows = list(db.scalars(select(Artifact).where(Artifact.run_id == run.id,
                                         Artifact.kind.in_(("answer", "evidence"))).order_by(Artifact.kind)))
            if {row.kind for row in rows} != {"answer", "evidence"}:
                return error("memory_requires_delivery_evidence", 409)
            try:
                for row in rows:
                    read_artifact(row, run)
            except (ValueError, OSError):
                return error("memory_requires_delivery_evidence", 409)
            refs = [f"artifact:{row.id}@{row.sha256}" for row in rows]
            current = db.scalar(select(KnowledgeClaim).where(KnowledgeClaim.org_id == task.org_id,
                                KnowledgeClaim.space_id == task.space_id, KnowledgeClaim.owner_scope == "public",
                                KnowledgeClaim.subject == subject, KnowledgeClaim.predicate == "交付复盘",
                                KnowledgeClaim.value == value, KnowledgeClaim.state == "active"))
            if current is None:
                current = record_claim(db, org_id=task.org_id, space_id=task.space_id, owner_scope="public",
                                       subject=subject, predicate="交付复盘", value=value, source_refs=refs,
                                       source_revision=task.revision, confidence_basis="human_reviewed_delivery")
            return {"ok": True, "data": {"claim_id": current.claim_id}}
