"""任务生命周期：来源证据、待确认、跨天保留、完成/重开。"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.ids import normalize_text, stable_id
from app.llm.service import extract_tasks
from app.models import Message, Source, Task, TaskEvent, TaskStatus

_DONE_RE = re.compile(r"(已完成|完成了|已处理|搞定了|已解决|done|finished)", re.I)


def _task_key(message: Message, action: str, due: str) -> str:
    return stable_id(
        message.tenant, message.chat_id, message.thread_id,
        normalize_text(action)[:80], normalize_text(due),
    )


def extract_and_upsert(
    session: Session, message: Message, *, source: Source | None = None
) -> list[Task]:
    settings = get_settings()
    payload = extract_tasks(message.content_text, message.message_id)
    tasks: list[Task] = []
    for item in payload.get("tasks", []):
        action = str(item.get("action") or "").strip()
        if not action:
            continue
        evidence = [str(value) for value in item.get("evidence") or [] if str(value).strip()]
        if message.message_id not in evidence:
            evidence.append(message.message_id)
        assignee = str(item.get("assignee") or "").strip()
        due = str(item.get("due") or "").strip()
        needs_confirmation = bool(item.get("needs_confirmation", True)) or not assignee
        key = _task_key(message, action, due)
        task = session.scalar(
            select(Task).where(Task.tenant == message.tenant, Task.task_key == key)
        )
        if task is None:
            task = Task(
                task_key=key,
                tenant=message.tenant,
                owner=source.owner if source else settings.feishu_owner_open_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                title=action[:255],
                action=action,
                assignee_id="",
                assignee_name=assignee,
                due_text=due,
                status=TaskStatus.NEEDS_CONFIRMATION if needs_confirmation else TaskStatus.OPEN,
                confidence=float(item.get("confidence") or 0.0),
                needs_confirmation=needs_confirmation,
                evidence_json=json.dumps(item.get("evidence") or evidence, ensure_ascii=False),
                source_message_ids=json.dumps(sorted(set(evidence)), ensure_ascii=False),
            )
            session.add(task)
            session.flush()
            session.add(
                TaskEvent(
                    task_id=task.id,
                    event_type="created",
                    actor="system",
                    detail="从消息提取",
                    evidence_message_ids=json.dumps(evidence, ensure_ascii=False),
                )
            )
        else:
            source_ids = set(task.source_ids()) | set(evidence)
            task.source_message_ids = json.dumps(sorted(source_ids), ensure_ascii=False)
            if task.status == TaskStatus.DONE:
                task.status = TaskStatus.NEEDS_CONFIRMATION
                task.needs_confirmation = True
            session.add(
                TaskEvent(
                    task_id=task.id,
                    event_type="updated",
                    actor="system",
                    detail="收到后续更新",
                    evidence_message_ids=json.dumps(evidence, ensure_ascii=False),
                )
            )
        tasks.append(task)
    session.flush()
    return tasks


def apply_completion_signal(session: Session, message: Message) -> list[Task]:
    if not _DONE_RE.search(message.content_text or ""):
        return []
    completed: list[Task] = []
    query = select(Task).where(
        Task.tenant == message.tenant,
        Task.chat_id == message.chat_id,
        Task.status.in_([TaskStatus.OPEN, TaskStatus.NEEDS_CONFIRMATION]),
    )
    if message.thread_id:
        query = query.where(Task.thread_id == message.thread_id)
    for task in session.scalars(query):
        task.status = TaskStatus.DONE
        task.needs_confirmation = False
        session.add(
            TaskEvent(
                task_id=task.id,
                event_type="completed",
                actor="system",
                detail="收到完成信号",
                evidence_message_ids=json.dumps([message.message_id], ensure_ascii=False),
            )
        )
        completed.append(task)
    session.flush()
    return completed


def set_status(
    session: Session,
    task_id: int,
    *,
    status: str,
    actor: str,
    evidence_message_ids: list[str] | None = None,
    detail: str = "",
) -> Task | None:
    task = session.get(Task, task_id)
    if task is None:
        return None
    if status == TaskStatus.DONE and not evidence_message_ids and actor != task.owner:
        raise ValueError("completion_requires_traceable_signal")
    task.status = status
    task.needs_confirmation = status == TaskStatus.NEEDS_CONFIRMATION
    session.add(
        TaskEvent(
            task_id=task.id,
            event_type=status,
            actor=actor,
            detail=detail,
            evidence_message_ids=json.dumps(evidence_message_ids or [], ensure_ascii=False),
        )
    )
    session.flush()
    return task


def open_tasks(session: Session, *, tenant: str, owner: str) -> list[Task]:
    return list(
        session.scalars(
            select(Task)
            .where(
                Task.tenant == tenant,
                Task.owner == owner,
                Task.status.in_(
                    [TaskStatus.OPEN, TaskStatus.NEEDS_CONFIRMATION, TaskStatus.DONE]
                ),
            )
            .order_by(Task.status.desc(), Task.created_at)
        )
    )
