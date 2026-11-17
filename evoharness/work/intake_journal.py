"""产品库事务内 intake 日志（DE-05/BP-03）。

- 事件、游标、投影意图与业务事实（由 ``projector`` 在同一 Session 写入）
  同事务提交/回滚；
- 事件唯一键 = ``(scope_key, message_id, revision)``：同一逻辑事件重放幂等，
  编辑/撤回产生新 revision 事件；
- 游标只前进；投影失败或进程中断由调用方事务回滚，事件与游标都不前进；
- 旧 JSON ledger 不在本模块写入，只作为可校验导入源由调用方显式处理。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column, Session

from evoharness.clock import utcnow
from evoharness.ids import sha256_text
from evoharness.models import Base


class IntakeEvent(Base):
    __tablename__ = "intake_events"
    __table_args__ = (UniqueConstraint("scope_key", "message_id", "revision", "recalled",
                                       name="uq_intake_scope_message_revision"),)
    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(128), index=True)
    message_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    recalled: Mapped[bool] = mapped_column(Boolean, default=False)
    action: Mapped[str] = mapped_column(String(16), default="upsert")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    projected_ref: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class IntakeCursor(Base):
    __tablename__ = "intake_cursors"
    scope_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    row_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class IntakeScopeLease(Base):
    """作用域扫描租约（DE-05/14 单写者本地语义）：同一 scope 同时只允许一个执行器。"""
    __tablename__ = "intake_scope_leases"
    scope_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    holder: Mapped[str] = mapped_column(String(128), default="")
    generation: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class IntakeJournalError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def event_key(scope_key: str, message_id: str, revision: int,
              recalled: bool = False) -> str:
    return sha256_text(f"{scope_key}|{message_id}|{int(revision)}|{int(bool(recalled))}")[:32]


def _order(message: dict) -> tuple[int, int]:
    return (int(message.get("create_time") or 0), int(message.get("id") or 0))


def advance_cursor(session: Session, scope_key: str, *, created_ms: int,
                   row_id: int) -> bool:
    """游标只前进；重复/回退的页不改变位置。"""
    cursor = session.get(IntakeCursor, scope_key)
    if cursor is None:
        session.add(IntakeCursor(scope_key=scope_key, created_ms=int(created_ms),
                                 row_id=int(row_id)))
        session.flush()
        return True
    if (int(created_ms), int(row_id)) <= (int(cursor.created_ms), int(cursor.row_id)):
        return False
    cursor.created_ms = int(created_ms)
    cursor.row_id = int(row_id)
    session.flush()
    return True


def ingest_page(session: Session, *, scope_key: str, messages: list[dict],
                projector, payload_of=None) -> dict:
    """页级原子处理：去重入账 → projector 写业务事实 → 标 projected → 前进游标。

    ``projector(session, message, event) -> projected_ref`` 必须使用同一
    Session 写业务事实；任何异常由调用方事务回滚，事件/游标一并回滚。
    """
    if not scope_key:
        raise IntakeJournalError("scope_key_required")
    ordered = sorted(messages or [], key=_order)
    recorded: list[str] = []
    skipped: list[str] = []
    projected: list[str] = []
    for message in ordered:
        message_id = str(message.get("message_id") or "")
        if not message_id:
            raise IntakeJournalError("message_id_required")
        revision = int(message.get("revision") or 1)
        recalled = bool(message.get("recalled"))
        key = event_key(scope_key, message_id, revision, recalled)
        row = session.scalar(select(IntakeEvent).where(
            IntakeEvent.scope_key == scope_key,
            IntakeEvent.message_id == message_id,
            IntakeEvent.revision == revision,
            IntakeEvent.recalled == recalled))
        if row is not None and row.state == "projected":
            skipped.append(key)
            continue
        if row is None:
            payload = dict(payload_of(message) if callable(payload_of) else {})
            row = IntakeEvent(
                event_key=key, scope_key=scope_key, message_id=message_id,
                revision=revision, recalled=recalled,
                action="recalled" if recalled else "upsert",
                payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True))
            session.add(row)
            session.flush()
            recorded.append(key)
        ref = projector(session, message, row)
        row.state = "projected"
        row.projected_ref = str(ref or "")
        session.flush()
        projected.append(key)
    if ordered:
        last = ordered[-1]
        advance_cursor(session, scope_key, created_ms=int(last.get("create_time") or 0),
                       row_id=int(last.get("id") or 0))
    return {"recorded": recorded, "skipped": skipped, "projected": projected,
            "cursor": cursor_of(session, scope_key)}


def acquire_scope_lease(session: Session, scope_key: str, *, holder: str,
                        lease_seconds: int = 300, now: datetime | None = None) -> bool:
    """CAS 获取扫描租约：未过期即拒绝；过期可接管并自增 generation。"""
    from datetime import timedelta
    now = now or utcnow()
    row = session.get(IntakeScopeLease, scope_key)
    expires = now + timedelta(seconds=max(1, int(lease_seconds)))
    if row is None:
        session.add(IntakeScopeLease(scope_key=scope_key, holder=str(holder),
                                     generation=1, lease_until=expires,
                                     updated_at=now))
        session.flush()
        return True
    if row.lease_until is not None and row.lease_until > now:
        return False
    from sqlalchemy import update
    result = session.execute(
        update(IntakeScopeLease)
        .where(IntakeScopeLease.scope_key == scope_key,
               IntakeScopeLease.lease_until <= now)
        .values(holder=str(holder), generation=int(row.generation or 0) + 1,
                lease_until=expires, updated_at=now))
    session.flush()
    return result.rowcount == 1


def release_scope_lease(session: Session, scope_key: str, *, holder: str) -> bool:
    row = session.get(IntakeScopeLease, scope_key)
    if row is None or str(row.holder) != str(holder):
        return False
    row.lease_until = utcnow()
    row.updated_at = utcnow()
    session.flush()
    return True


def cursor_of(session: Session, scope_key: str) -> tuple[int, int] | None:
    cursor = session.get(IntakeCursor, scope_key)
    return (int(cursor.created_ms), int(cursor.row_id)) if cursor else None


def enqueue_page(session: Session, *, scope_key: str, messages: list[dict],
                 payload_of=None) -> dict:
    """两阶段相 A：只落事件与游标（投影意图 pending），不执行业务投影。

    与 ``ingest_page`` 共用事件键/幂等/游标语义；已 projected 的事件跳过，
    pending 事件保留给下一阶段重放。
    """
    if not scope_key:
        raise IntakeJournalError("scope_key_required")
    ordered = sorted(messages or [], key=_order)
    recorded: list[str] = []
    skipped: list[str] = []
    for message in ordered:
        message_id = str(message.get("message_id") or "")
        if not message_id:
            raise IntakeJournalError("message_id_required")
        revision = int(message.get("revision") or 1)
        recalled = bool(message.get("recalled"))
        key = event_key(scope_key, message_id, revision, recalled)
        row = session.scalar(select(IntakeEvent).where(
            IntakeEvent.scope_key == scope_key,
            IntakeEvent.message_id == message_id,
            IntakeEvent.revision == revision,
            IntakeEvent.recalled == recalled))
        if row is not None:
            skipped.append(key)
            continue
        payload = dict(payload_of(message) if callable(payload_of) else {})
        session.add(IntakeEvent(
            event_key=key, scope_key=scope_key, message_id=message_id,
            revision=revision, recalled=recalled,
            action="recalled" if recalled else "upsert",
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            state="pending"))
        session.flush()
        recorded.append(key)
    if ordered:
        last = ordered[-1]
        advance_cursor(session, scope_key, created_ms=int(last.get("create_time") or 0),
                       row_id=int(last.get("id") or 0))
    return {"recorded": recorded, "skipped": skipped,
            "cursor": cursor_of(session, scope_key)}


def pending_events(session: Session, scope_key: str,
                   limit: int = 500) -> list[IntakeEvent]:
    return list(session.scalars(select(IntakeEvent).where(
        IntakeEvent.scope_key == scope_key,
        IntakeEvent.state == "pending").order_by(IntakeEvent.created_at)
        .limit(max(1, int(limit)))))


def mark_projected(session: Session, event_key: str, projected_ref: str) -> bool:
    """两阶段相 B：投影成功后落回执；pending → projected。"""
    row = session.get(IntakeEvent, str(event_key))
    if row is None or row.state == "projected":
        return False
    row.state = "projected"
    row.projected_ref = str(projected_ref or "")
    session.flush()
    return True


def replay_pending(session: Session, *, scope_key: str, projector,
                   limit: int = 500) -> dict:
    """崩溃恢复：按 pending 重放投影；投影器须幂等（按逻辑键），成功后回执。"""
    events = pending_events(session, scope_key, limit=limit)
    projected: list[str] = []
    for row in events:
        message = {"message_id": row.message_id, "revision": row.revision,
                   "recalled": row.recalled}
        ref = projector(session, message, row)
        mark_projected(session, row.event_key, ref)
        projected.append(row.event_key)
    return {"projected": projected, "remaining": len(pending_events(
        session, scope_key, limit=limit))}


def reset_cursor(session: Session, scope_key: str) -> bool:
    cursor = session.get(IntakeCursor, scope_key)
    if cursor is None:
        return False
    session.delete(cursor)
    session.flush()
    return True


class ProductIntakeJournal:
    """把产品库事务日志适配成 task_intake 的 journal 接口（DE-05 管线钩子）。

    - ``enqueue``：事件与游标在同一产品库事务提交（相 A）；
    - ``pending`` / ``mark_projected`` / ``mark_source_changed``：相 B 回执；
    - ``cursor`` 只前进；``close`` 无持久连接（每次短事务）。
    默认不启用；`task_intake.run(journal_factory=...)` 显式注入。
    """

    def __init__(self, session_factory, *, allow_write: bool = False):
        if not allow_write:
            raise PermissionError("intake_journal_write_not_authorized")
        self._factory = session_factory

    def pending(self, scope_key: str) -> list[dict]:
        with self._factory() as session:
            rows = pending_events(session, scope_key)
            return [{"event_key": row.event_key, "scope_key": row.scope_key,
                     "message_id": row.message_id, "revision": row.revision,
                     "action": row.action, "payload_json": row.payload_json,
                     "state": row.state, "item_id": row.projected_ref}
                    for row in rows]

    def enqueue(self, scope_key: str, msg: dict, event_key: str,
                payload: dict) -> None:
        recalled = bool(msg.get("recalled"))
        with self._factory.begin() as session:
            existing = session.get(IntakeEvent, str(event_key))
            if existing is None:
                session.add(IntakeEvent(
                    event_key=str(event_key), scope_key=scope_key,
                    message_id=str(msg["message_id"]),
                    revision=int(msg.get("revision") or 1), recalled=recalled,
                    action="recalled" if recalled else "upsert",
                    payload_json=json.dumps(payload, ensure_ascii=False,
                                            sort_keys=True), state="pending"))
                session.flush()
            advance_cursor(session, scope_key,
                           created_ms=int(msg.get("create_time") or 0),
                           row_id=int(msg.get("id") or 0))

    def mark_projected(self, event_key: str, item_id: str) -> bool:
        with self._factory.begin() as session:
            return mark_projected(session, event_key, item_id)

    def record_and_mark(self, event: dict, item_id: str) -> None:
        """同一产品库事务：写投影真源 + 事件回执（DE-05 相 B）。"""
        with self._factory.begin() as session:
            record_projection(session, event=event, item_id=item_id)
            mark_projected(session, event["event_key"], item_id)

    def mark_source_changed(self, event_key: str) -> bool:
        with self._factory.begin() as session:
            row = session.get(IntakeEvent, str(event_key))
            if row is None or row.state == "projected":
                return False
            row.state = "source_changed"
            session.flush()
            return True

    def acquire_lease(self, scope_key: str, holder: str,
                      lease_seconds: int = 300) -> bool:
        with self._factory.begin() as session:
            return acquire_scope_lease(session, scope_key, holder=holder,
                                       lease_seconds=lease_seconds)

    def release_lease(self, scope_key: str, holder: str) -> bool:
        with self._factory.begin() as session:
            return release_scope_lease(session, scope_key, holder=holder)

    def cursor(self, scope_key: str) -> tuple[int, int] | None:
        with self._factory() as session:
            return cursor_of(session, scope_key)

    def reset_cursor(self, scope_key: str) -> bool:
        with self._factory.begin() as session:
            return reset_cursor(session, scope_key)

    def close(self) -> None:
        return None


class IntakeProjection(Base):
    """技术投影的产品库真源（DE-05）：旧 BACKLOG JSON 可由本表重建。

    ``item_id`` 保存首次投影生成的 L 号；重建时强制复用，旧 ID/审计链不漂移。
    """
    __tablename__ = "intake_projections"
    logical_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    scope_key: Mapped[str] = mapped_column(String(128), index=True)
    source_event_key: Mapped[str] = mapped_column(String(64), index=True)
    message_id: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[int] = mapped_column(Integer, default=1)
    action: Mapped[str] = mapped_column(String(16), default="upsert")
    title: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    state: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


def record_projection(session: Session, *, event: dict, item_id: str) -> IntakeProjection:
    """把一次投影写入产品库（按 logical_key 幂等；撤回置 recalled）。"""
    payload = json.loads(event["payload_json"])
    logical = str(payload.get("logical_key") or "")
    if not logical:
        raise IntakeJournalError("logical_key_required")
    row = session.get(IntakeProjection, logical)
    if row is None:
        row = IntakeProjection(logical_key=logical)
    row.item_id = str(item_id)
    row.scope_key = str(event.get("scope_key") or "")
    row.source_event_key = str(event.get("event_key") or "")
    row.message_id = str(event.get("message_id") or "")
    row.source_revision = int(event.get("revision") or 1)
    row.action = str(event.get("action") or "upsert")
    row.title = str(payload.get("title") or "")
    row.payload_json = str(event["payload_json"])
    row.state = "recalled" if row.action == "recalled" else "active"
    session.add(row)
    session.flush()
    return row


def projections(session: Session, *, scope_key: str = "") -> list[IntakeProjection]:
    query = select(IntakeProjection).order_by(IntakeProjection.created_at,
                                              IntakeProjection.logical_key)
    if scope_key:
        query = query.where(IntakeProjection.scope_key == scope_key)
    return list(session.scalars(query))


def rebuild_ledger(session: Session, *, project, scope_key: str = "") -> dict:
    """从产品库投影重建旧 ledger：``project(event, forced_item_id=...)``。

    ``project`` 由调用方注入（integration.intake_store.project），避免产品层
    反向依赖渠道投影实现。重建是幂等的：同一 L 号/逻辑键不会重复生成。
    """
    rebuilt = []
    for row in projections(session, scope_key=scope_key):
        event = {"event_key": row.source_event_key, "scope_key": row.scope_key,
                 "message_id": row.message_id, "revision": row.source_revision,
                 "action": row.action, "payload_json": row.payload_json}
        item_id, created = project(event, forced_item_id=row.item_id)
        rebuilt.append({"logical_key": row.logical_key, "item_id": item_id,
                        "created": bool(created)})
    return {"rebuilt": len(rebuilt), "items": rebuilt}
