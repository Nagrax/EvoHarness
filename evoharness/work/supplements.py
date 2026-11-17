"""在途追加与取消：持久有序队列，执行器在安全边界读取（DE-14）。

同一工作项的新说明按 source event ID 去重进入有序队列；取消优先于普通
补充传播。目标不因普通补充丢失；取消必须尽快可见，不等模型自己读懂。
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.work.models import TaskSupplement


class SupplementError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def enqueue(session: Session, task_id: int, *, event_id: str, event_kind: str,
            payload: dict | None = None) -> TaskSupplement:
    """同一 (task, event) 幂等：重复投递返回已有行，不重复执行。"""
    if event_kind not in {"note", "cancellation"}:
        raise SupplementError("invalid_supplement_kind")
    if not event_id:
        raise SupplementError("supplement_event_id_required")
    existing = session.scalar(select(TaskSupplement).where(
        TaskSupplement.task_id == task_id, TaskSupplement.event_id == event_id))
    if existing is not None:
        return existing
    row = TaskSupplement(task_id=task_id, event_id=event_id, event_kind=event_kind,
                         payload_json=json.dumps(payload or {}, ensure_ascii=False))
    session.add(row)
    session.flush()
    return row


def pending(session: Session, task_id: int, *, limit: int = 50) -> list[TaskSupplement]:
    """执行器安全边界读取：取消优先，普通补充按到达顺序。"""
    rows = list(session.scalars(select(TaskSupplement).where(
        TaskSupplement.task_id == task_id,
        TaskSupplement.consumed_at.is_(None)).order_by(TaskSupplement.id).limit(limit * 2)))
    cancellations = [r for r in rows if r.event_kind == "cancellation"]
    notes = [r for r in rows if r.event_kind == "note"]
    return (cancellations + notes)[:limit]


def consume(session: Session, supplement_id: int) -> TaskSupplement:
    row = session.get(TaskSupplement, supplement_id)
    if row is None:
        raise SupplementError("supplement_not_found")
    if row.consumed_at is None:
        row.consumed_at = utcnow()
        session.flush()
    return row


def has_pending_cancellation(session: Session, task_id: int) -> bool:
    return session.scalar(select(TaskSupplement.id).where(
        TaskSupplement.task_id == task_id,
        TaskSupplement.event_kind == "cancellation",
        TaskSupplement.consumed_at.is_(None)).limit(1)) is not None
