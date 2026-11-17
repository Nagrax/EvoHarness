"""Composition root for the existing product database and Task service.

No SDK, channel Settings, credentials or database are loaded at import time.
"""
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy.engine import make_url
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from evoharness import models
from evoharness.migrations import migrate, MigrationContext
from evoharness.work import service

class Application:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    @classmethod
    def open(cls, database_url, *, context=None):
        if not database_url:
            raise ValueError("database_url_required")
        url = make_url(database_url)
        if (url.get_backend_name() == "sqlite" and url.database
                and url.database != ":memory:" and not url.database.startswith("file:")):
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(database_url, future=True)
        try:
            migrate(engine, context=context or MigrationContext())
        except BaseException:
            engine.dispose()
            raise
        app = cls(sessionmaker(bind=engine, expire_on_commit=False))
        app.engine = engine
        return app

    @contextmanager
    def transaction(self):
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def create_from_extraction(self, message_id, *, tenant, owner, payload, source_id=None):
        if not tenant or not owner:
            raise ValueError("principal_binding_required")
        with self.transaction() as session:
            message = session.scalar(select(models.Message).where(
                models.Message.tenant == tenant, models.Message.message_id == message_id))
            if message is None:
                raise ValueError("message_not_found")
            source = session.get(models.Source, source_id) if source_id is not None else None
            if source is None or source.tenant != tenant or source.owner != owner or not source.enabled or source.revoked_at:
                raise ValueError("source_grant_required")
            if source.chat_id != message.chat_id or (source.thread_id and source.thread_id != message.thread_id):
                raise ValueError("source_scope_mismatch")
            tasks = service.extract_and_upsert(session, message, payload=payload,
                source=source, default_owner=owner)
            return [task.id for task in tasks]

    def tasks(self, *, tenant, owner):
        with self.transaction() as session:
            return [{"id": t.id, "status": t.status, "title": t.title, "owner": t.owner}
                    for t in service.open_tasks(session, tenant=tenant, owner=owner)]

    def task_status(self, task_id, *, tenant, owner, status, evidence_message_ids=None):
        with self.transaction() as session:
            task = session.get(models.Task, task_id)
            if task is None or task.tenant != tenant or task.owner != owner:
                raise ValueError("task_not_found")
            task = service.set_status(session, task_id, status=status, actor=owner,
                evidence_message_ids=evidence_message_ids)
            return {"id": task.id, "status": task.status}
