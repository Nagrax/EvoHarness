"""任务生命周期：来源证据、待确认、跨天保留、完成/重开。"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.ids import normalize_text, stable_id
from evoharness.models import Message, Source, Task, TaskEvent, TaskStatus
from evoharness.work.state import (ACTIVE, TRANSITIONS, WAITING, is_delegated,
                                   waiting_requires_reason)

_DONE_RE = re.compile(r"(已完成|完成了|已处理|搞定了|已解决|done|finished)", re.I)


def _task_key(message: Message, action: str, due: str) -> str:
    return stable_id(
        message.tenant, message.chat_id, message.thread_id,
        normalize_text(action)[:80], normalize_text(due),
    )


def extract_and_upsert(
    session: Session, message: Message, *, payload: dict, default_owner: str, source: Source | None = None
) -> list[Task]:
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
                owner=source.owner if source else default_owner,
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
        Task.status.in_(sorted(ACTIVE)),
    )
    if message.thread_id:
        query = query.where(Task.thread_id == message.thread_id)
    for task in session.scalars(query):
        if is_delegated(task):
            if message.sender_id in {task.owner, task.assignee_id}:
                session.add(TaskEvent(task_id=task.id, event_type="acceptance_requested",
                    actor=message.sender_id, detail="completion signal requires delivery evidence",
                    evidence_message_ids=json.dumps([message.message_id])))
            continue
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
    if is_delegated(task):
        if actor not in {task.owner, task.assignee_id}:
            raise ValueError("task_actor_not_authorized")
        if status == TaskStatus.DONE:
            raise ValueError("delivery_acceptance_required")
        if status not in ACTIVE | {TaskStatus.CANCELLED}:
            raise ValueError("invalid_task_state")
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
                    sorted(ACTIVE | {TaskStatus.DONE})
                ),
            )
            .order_by(Task.status.desc(), Task.created_at)
        )
    )


# ── DE-10：统一状态机、澄清流、合同修订与协调提案 ───────────────────────────


def ready_gate(task: Task) -> None:
    """委派交付进入可执行前必须有目标、责任人与验收；空验收不能自动放行。"""
    if is_delegated(task):
        try:
            acceptance = json.loads(task.acceptance_json or "[]")
        except ValueError:
            acceptance = []
        if not task.goal.strip() or not task.owner or not acceptance:
            raise ValueError("incomplete_work_contract")


def transition_to(session: Session, task_id: int, *, to_state: str, actor: str,
                  reason: str = "", wake_condition: str = "",
                  evidence_message_ids: list[str] | None = None,
                  expected_revision: int | None = None,
                  _accepted_delivery: bool = False) -> Task:
    """状态机唯一转换入口：非法转换、缺因等待、委派直 done、过期 revision 全部拒绝。

    委派交付的 done 只能由验收服务（evoharness.work.acceptance.review）在
    证据核对通过后以 `_accepted_delivery` 进入；聊天信号与其他入口均收敛拒绝。
    """
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError("task_not_found")
    if expected_revision is not None and task.revision != expected_revision:
        raise ValueError("stale_task_revision")
    allowed = TRANSITIONS.get(task.status, set())
    if to_state not in allowed:
        raise ValueError("invalid_task_transition")
    if waiting_requires_reason(to_state) and not reason.strip():
        raise ValueError("waiting_reason_required")
    if to_state == TaskStatus.DONE:
        if is_delegated(task) and not _accepted_delivery:
            raise ValueError("delivery_acceptance_required")
        if not evidence_message_ids and actor != task.owner and not _accepted_delivery:
            raise ValueError("completion_requires_traceable_signal")
    if to_state == "queued" and task.status == TaskStatus.DONE and not (reason.strip() or evidence_message_ids):
        raise ValueError("reopen_requires_new_evidence")
    if to_state in {"ready", "queued"}:
        ready_gate(task)
    task.status = to_state
    task.waiting_reason = reason.strip() if waiting_requires_reason(to_state) else ""
    if wake_condition:
        task.next_action = wake_condition
    session.add(
        TaskEvent(
            task_id=task.id,
            event_type=to_state,
            actor=actor,
            detail=(reason or wake_condition or "").strip(),
            evidence_message_ids=json.dumps(evidence_message_ids or [], ensure_ascii=False),
        )
    )
    session.flush()
    return task


def append_note(session: Session, task: Task, *, message_id: str, note: str, actor: str) -> TaskEvent:
    """普通补充说明：同一事件去重，保留目标与 revision，不重开需求确认。"""
    seen = set(task.source_ids())
    if message_id in seen:
        return TaskEvent(task_id=task.id, event_type="note_ignored_duplicate",
                         actor=actor, detail="duplicate source event")
    task.source_message_ids = json.dumps(sorted(seen | {message_id}), ensure_ascii=False)
    session.add(task)
    event = TaskEvent(task_id=task.id, event_type="note_appended", actor=actor,
                      detail=note[:500],
                      evidence_message_ids=json.dumps([message_id], ensure_ascii=False))
    session.add(event)
    session.flush()
    return event


def revise_contract(session: Session, task_id: int, *, actor: str,
                    goal: str | None = None, acceptance: list | None = None,
                    expected_revision: int, reason: str,
                    evidence_message_ids: list[str] | None = None) -> Task:
    """范围/验收/目标的语义变更：revision+1 使旧授权与在途 Run 失效；普通说明不走此入口。"""
    if not reason.strip():
        raise ValueError("contract_change_reason_required")
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError("task_not_found")
    if task.revision != expected_revision:
        raise ValueError("stale_task_revision")
    changed = []
    if goal is not None and goal.strip() and goal != task.goal:
        task.goal = goal
        changed.append("goal")
    if acceptance is not None:
        new_json = json.dumps(acceptance, ensure_ascii=False)
        if new_json != task.acceptance_json:
            task.acceptance_json = new_json
            changed.append("acceptance")
    if not changed:
        return task
    task.revision += 1
    session.add(TaskEvent(task_id=task.id, event_type="contract_revised", actor=actor,
                          detail=f"{','.join(changed)}: {reason[:400]}",
                          evidence_message_ids=json.dumps(evidence_message_ids or [], ensure_ascii=False)))
    session.flush()
    return task


def request_clarification(session: Session, task_id: int, *, actor: str, question: str,
                          blocking: bool = True) -> Task:
    """只追问阻碍行动的缺项；非阻塞问题不改变工作状态。"""
    if not question.strip():
        raise ValueError("clarification_question_required")
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError("task_not_found")
    session.add(TaskEvent(task_id=task.id, event_type="clarification_requested", actor=actor,
                          detail=question[:500]))
    if blocking and task.status in {"open", "ready", "queued", TaskStatus.NEEDS_CONFIRMATION}:
        task.status = "needs_confirmation"
        task.waiting_reason = question[:500]
    session.flush()
    return task


def resolve_clarification(session: Session, task_id: int, *, actor: str,
                          answer_message_id: str) -> Task:
    """澄清补齐后经 ready_gate 放行；合同仍不完整时保持待澄清。"""
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError("task_not_found")
    if task.status != TaskStatus.NEEDS_CONFIRMATION:
        raise ValueError("task_not_awaiting_clarification")
    task.status = "open"
    task.waiting_reason = ""
    session.add(TaskEvent(task_id=task.id, event_type="clarification_resolved", actor=actor,
                          detail="", evidence_message_ids=json.dumps([answer_message_id], ensure_ascii=False)))
    session.flush()
    # 合同是否齐备由显式 transition_to(ready) 的 ready_gate 把关，这里不伪装可执行
    return task


def propose(session: Session, task_id: int, *, actor: str, kind: str, payload: dict) -> TaskEvent:
    """协调提案只产生事件记录；任何状态或合同修改必须走显式受校验入口。"""
    if kind not in {"relate", "clarify", "plan", "progress", "block"}:
        raise ValueError("invalid_proposal_kind")
    event = TaskEvent(task_id=task_id, event_type="proposal", actor=actor,
                      detail=kind,
                      evidence_message_ids=json.dumps(payload, ensure_ascii=False))
    session.add(event)
    session.flush()
    return event
