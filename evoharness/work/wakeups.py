"""DE-21：事件唤醒、去重催办与责任路由（有界规则引擎）。

原则（总体指南 §9.1、§1.3；实施契约 §1）：
- 规则评估是确定性代码：闲置时不占 LLM 会话，不自动调模型；
  唤醒规则是有界集合（依赖恢复/新回复/CI 结束/期限临近），未知事件类型被拒绝。
- 提醒按 task+reason+revision 去重与冷却；只有有效进展（状态/证据变化）
  才绕过冷却重新通知；连续空检查合并，只留本地记录，不产生发信意图。
- 任务执行中（Run 在途或状态 running）不催同一动作。
- 提醒必须带负责人、原因、截止时间与处理入口；各责任人收各自事项，
  不推给部署 owner；无责任人或责任人不在组织内时不提醒、不运行。
- 发信意图幂等键 task+reason+revision+recipient+notifySeq，接 outbox 意图去重。
- 来源读取失败标 unknown、积压尾部过老标 stale 并告警，不与业务空态混同。
- 长期协调职责：基于在途变更生成结构化"有效下一步"提案，经 revision 与
  授权核验后才执行；提案无授权不执行，且不能只输出提醒。

本地审计复用 TaskEvent（event_type=wakeup_notified / wakeup_coalesced），
不新增表；evidence JSON 携带 kind/revision/notifySeq/taskUpdatedAt。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.models import Outbox, Task, TaskEvent, TaskStatus
from evoharness.outbox import enqueue as outbox_enqueue
from evoharness.work import execution, service, state, supplements
from evoharness.work.models import ExecutionRun

__all__ = [
    "WakeEvent", "WakeupError", "WAKE_KINDS", "PROPOSAL_KINDS", "REASON_TEXT",
    "evaluate", "deadline_events", "source_health", "membership_active",
    "coordinate", "propose_next_steps", "resolve_recipient",
]

# 有界唤醒规则集：未知事件类型不是规则，不会被评估。
WAKE_KINDS = frozenset({"dependency_resolved", "new_reply", "ci_finished", "deadline_near"})

REASON_TEXT = {
    "dependency_resolved": "依赖已完成，可以推进",
    "new_reply": "有新回复需要处理",
    "ci_finished": "CI 已结束，需要验收或返工决定",
    "deadline_near": "任务临近截止",
}

# 契约 §1 的协调提案种类（request_run 由 Run 服务复核授权）。
PROPOSAL_KINDS = frozenset({"link_work", "clarify_goal", "propose_plan",
                            "request_run", "reconcile_progress", "raise_blocker"})

# request_run 不在 service.propose 的五类之内，单独落 TaskEvent；其余复用原入口。
_PROPOSE_EVENT_KIND = {
    "link_work": "relate",
    "clarify_goal": "clarify",
    "propose_plan": "plan",
    "reconcile_progress": "progress",
    "raise_blocker": "block",
}

DEFAULT_COOLDOWN_SECONDS = 3600
DEFAULT_STALE_AFTER_SECONDS = 48 * 3600
DEFAULT_DEADLINE_WINDOW_HOURS = 48

ACTIVE_RUN_STATES = frozenset({
    "queued", "claimed", "running", "waiting_recovery", "waiting_acceptance",
    "waiting_authorization", "waiting_budget", "waiting_resource", "waiting_external",
})


class WakeupError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class WakeEvent:
    """外部确定性事件：由事件源（渠道订阅、CI 回调、扫描器）产生，引擎只评估。"""
    kind: str
    task_key: str = ""
    tenant: str = "default"
    message_id: str = ""
    actor: str = ""
    observed_at: datetime | None = None


# ── 端口与 helpers ────────────────────────────────────────────────────────────

EnqueuePort = Callable[..., tuple[object, bool]]


def _default_enqueue(session: Session, *, intent: str, tenant: str, payload: dict,
                     target_id: str, chat_id: str = "") -> tuple[object, bool]:
    """outbox 意图端口默认实现：同 intent_key 只建一条，重复触发不再发信。"""
    existing = session.scalar(select(Outbox).where(Outbox.intent_key == intent))
    if existing is not None:
        return existing, False
    row = outbox_enqueue(
        session, intent=intent, tenant=tenant, kind="notice",
        target_type="task", target_id=target_id, payload=payload, chat_id=chat_id,
    )
    return row, True


def _load_json(raw: str, fallback: dict) -> dict:
    try:
        data = json.loads(raw) if raw else fallback
    except ValueError:
        return fallback
    return data if isinstance(data, dict) else fallback


def _load_list(raw: str) -> list:
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _stamp(task: Task) -> str:
    return task.updated_at.isoformat() if task.updated_at is not None else ""


def _find_task(session: Session, event: WakeEvent) -> Task | None:
    if not event.task_key:
        return None
    return session.scalar(
        select(Task).where(Task.tenant == event.tenant, Task.task_key == event.task_key)
    )


def resolve_recipient(task: Task) -> str:
    """责任路由：直接责任人优先（assignee），其次任务 owner；绝不落到部署 owner。"""
    return (task.assignee_id or "").strip() or (task.owner or "").strip()


def _due_text(task: Task) -> str:
    if task.due_text:
        return task.due_text
    if task.due_at is not None:
        return task.due_at.date().isoformat()
    return "未设置"  # 不虚构日期


def _entry(task: Task) -> str:
    entry = f"task:{task.task_key}"
    if task.thread_id:
        entry += f" thread:{task.thread_id}"
    return entry


def _active_run(session: Session, task_id: int) -> ExecutionRun | None:
    return session.scalar(select(ExecutionRun).where(
        ExecutionRun.task_id == task_id,
        ExecutionRun.state.in_(sorted(ACTIVE_RUN_STATES))).limit(1))


def _last_event(session: Session, task_id: int, event_type: str,
                kind: str, revision: int) -> TaskEvent | None:
    rows = session.scalars(select(TaskEvent).where(
        TaskEvent.task_id == task_id, TaskEvent.event_type == event_type
    ).order_by(TaskEvent.id.desc()).limit(50))
    for row in rows:
        data = _load_json(row.evidence_message_ids, {})
        if data.get("kind") == kind and data.get("revision") == revision:
            return row
    return None


# ── 事件评估（确定性，无模型调用） ────────────────────────────────────────────

def evaluate(
    session: Session,
    *,
    events: list[WakeEvent],
    now: datetime | None = None,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    validate_context: Callable | None = None,
    recipient_ok: Callable[[str], bool] | None = None,
    enqueue: EnqueuePort | None = None,
    output_root: str = "out",
) -> dict:
    """评估一批唤醒事件；返回可审计报告，不自动调模型。

    - 空事件轮 = 一次空检查：idleChecks 计数，只留本地记录。
    - 无责任人 / 责任人不在组织 / 授权对象缺失 → 不提醒、不运行。
    - 执行中 → 合并为本地记录，不催同一动作。
    - 冷却窗内且无有效进展 → 合并；有效进展（状态/证据变化）→ 允许重新通知。
    - 依赖恢复 + 委派合同齐备 → 经同一 Run 服务幂等起 Run（授权在服务内复核）。
    """
    now = now or utcnow()
    enqueue_port = enqueue or _default_enqueue
    report: dict = {"wakeups": [], "coalesced": [], "skipped": [],
                    "notifications": 0, "idleChecks": 0}
    if not events:
        report["idleChecks"] = 1
        return report
    for event in events:
        _evaluate_one(session, event, report, now=now,
                      cooldown_seconds=cooldown_seconds,
                      validate_context=validate_context,
                      recipient_ok=recipient_ok,
                      enqueue_port=enqueue_port, output_root=output_root)
    report["idleChecks"] = len(report["skipped"])
    session.flush()
    return report


def _evaluate_one(session: Session, event: WakeEvent, report: dict, *, now: datetime,
                  cooldown_seconds: int, validate_context: Callable | None,
                  recipient_ok: Callable[[str], bool] | None,
                  enqueue_port: EnqueuePort, output_root: str) -> None:
    def skip(reason: str) -> None:
        report["skipped"].append({"kind": event.kind, "taskKey": event.task_key,
                                  "reason": reason})

    if event.kind not in WAKE_KINDS:
        skip("unknown_wake_kind")
        return
    task = _find_task(session, event)
    if task is None:
        skip("no_matching_task")
        return
    if task.status in state.TERMINAL:
        skip("terminal_task")
        return
    # 自己的消息不无限唤醒：actor 即责任人，或消息已是任务证据。
    if event.actor and event.actor in {task.owner, task.assignee_id}:
        skip("self_message")
        return
    if event.message_id and event.message_id in set(task.source_ids()):
        skip("known_evidence")
        return
    # 执行中不催同一动作。
    if task.status == "running" or _active_run(session, task.id) is not None:
        _coalesce(session, task, event, report, now=now,
                  reason="run_in_progress_no_duplicate_nudge")
        return
    # 责任路由。
    recipient = resolve_recipient(task)
    if not recipient:
        skip("no_responsible_owner")
        return
    if recipient_ok is not None and not recipient_ok(recipient):
        skip("recipient_inactive")  # 离开组织：旧定时委派与提醒一并拒绝
        return
    # 去重冷却：只有有效进展（状态/证据变化）绕过冷却。
    last = _last_event(session, task.id, "wakeup_notified", event.kind, task.revision)
    seq = 0
    if last is not None:
        data = _load_json(last.evidence_message_ids, {})
        last_seq = int(data.get("notifySeq", 0))
        progressed = bool(data.get("taskUpdatedAt")) and data["taskUpdatedAt"] != _stamp(task)
        within = (now - last.created_at) < timedelta(seconds=cooldown_seconds)
        if within and not progressed:
            _coalesce(session, task, event, report, now=now, reason="cooldown")
            return
        seq = last_seq + 1 if progressed else last_seq
    # 依赖恢复：先经唯一状态机入口放行，再经同一 Run 服务幂等起 Run。
    run_info = None
    if event.kind == "dependency_resolved" and state.is_delegated(task):
        run_info = _maybe_start_run(session, task, event, report,
                                    validate_context=validate_context,
                                    output_root=output_root)
    # 发信意图：task+reason+revision+recipient+notifySeq，outbox 层幂等。
    intent = f"wakeup|{task.task_key}|{event.kind}|{task.revision}|{recipient}|{seq}"
    payload = {
        "text": (f"【{task.task_key}】负责人 {recipient}：{REASON_TEXT[event.kind]}。"
                 f"截止：{_due_text(task)}。处理入口：{_entry(task)}"),
        "owner": recipient,
        "reason": event.kind,
        "due": _due_text(task),
        "entry": _entry(task),
        "task_key": task.task_key,
        "revision": task.revision,
        "notifySeq": seq,
    }
    row, created = enqueue_port(session, intent=intent, tenant=task.tenant,
                                payload=payload, target_id=str(task.id),
                                chat_id=task.chat_id)
    if not created:
        # 重复触发：意图已存在，不重复发信，也不追加本地通知记录。
        entry = {"taskKey": task.task_key, "kind": event.kind,
                 "recipient": recipient, "intent": intent,
                 "reminderCreated": False, "deduped": True}
        if run_info is not None:
            entry["run"] = run_info
        report["wakeups"].append(entry)
        return
    session.add(TaskEvent(
        task_id=task.id, event_type="wakeup_notified", actor="wakeup-engine",
        detail=event.kind, created_at=now,
        evidence_message_ids=json.dumps(
            {"kind": event.kind, "revision": task.revision, "notifySeq": seq,
             "taskUpdatedAt": _stamp(task), "message_id": event.message_id,
             "recipient": recipient, "intent": intent}, ensure_ascii=False),
    ))
    session.flush()
    report["notifications"] += 1
    wakeup = {"taskKey": task.task_key, "kind": event.kind, "recipient": recipient,
              "intent": intent, "reminderCreated": True,
              "reason": REASON_TEXT[event.kind], "due": _due_text(task),
              "entry": _entry(task)}
    if run_info is not None:
        wakeup["run"] = run_info
    report["wakeups"].append(wakeup)


def _coalesce(session: Session, task: Task, event: WakeEvent, report: dict, *,
              now: datetime, reason: str) -> None:
    """连续空检查/执行中合并：只留一条本地记录，不产生发信意图。"""
    if _last_event(session, task.id, "wakeup_coalesced", event.kind, task.revision) is None:
        session.add(TaskEvent(
            task_id=task.id, event_type="wakeup_coalesced", actor="wakeup-engine",
            detail=reason, created_at=now,
            evidence_message_ids=json.dumps(
                {"kind": event.kind, "revision": task.revision}, ensure_ascii=False),
        ))
        session.flush()
    report["coalesced"].append({"taskKey": task.task_key, "kind": event.kind,
                                "reason": reason})


def _maybe_start_run(session: Session, task: Task, event: WakeEvent, report: dict, *,
                     validate_context: Callable | None, output_root: str) -> dict | None:
    """依赖恢复 → waiting_dependency 经状态机放行 → 幂等 Run 意图（授权在 Run 服务复核）。"""
    if task.status == "waiting_dependency":
        try:
            service.transition_to(
                session, task.id, to_state="queued", actor="wakeup-engine",
                reason=f"dependency_resolved:{event.message_id or 'event'}")
        except ValueError as exc:
            report["skipped"].append({"kind": event.kind, "taskKey": task.task_key,
                                      "reason": f"run_not_started:{exc}"})
            return None
    if validate_context is None:
        # 无授权端口不启动 Run：提醒仍会发出，由责任人补授权。
        report["skipped"].append({"kind": event.kind, "taskKey": task.task_key,
                                  "reason": "run_not_started:authorization_adapter_required"})
        return None
    idempotency_key = f"wake|{task.task_key}|{task.revision}"
    existing = session.scalar(select(ExecutionRun).where(
        ExecutionRun.tenant == task.tenant,
        ExecutionRun.idempotency_key == idempotency_key))
    try:
        run = execution.request_run(
            session, task_id=task.id,
            context={"task_key": task.task_key, "revision": task.revision},
            actor_id="wakeup-engine", output_root=output_root,
            idempotency_key=idempotency_key, validate_context=validate_context)
    except ValueError as exc:
        report["skipped"].append({"kind": event.kind, "taskKey": task.task_key,
                                  "reason": f"run_not_started:{exc}"})
        return None
    return {"runId": run.id, "idempotencyKey": idempotency_key,
            "alreadyExisted": existing is not None}


def deadline_events(session: Session, *, now: datetime | None = None,
                    within_hours: int = DEFAULT_DEADLINE_WINDOW_HOURS) -> list[WakeEvent]:
    """时间唤醒补偿漏事件：扫描 ACTIVE 且 due_at 落在窗口内的任务（确定性）。"""
    now = now or utcnow()
    horizon = now + timedelta(hours=within_hours)
    events = []
    for task in session.scalars(select(Task).where(
            Task.status.in_(sorted(state.ACTIVE)),
            Task.due_at.is_not(None), Task.due_at <= horizon).order_by(Task.due_at)):
        events.append(WakeEvent(kind="deadline_near", task_key=task.task_key,
                                tenant=task.tenant, observed_at=now))
    return events


# ── 来源健康：读取失败不与业务空态混同 ────────────────────────────────────────

def source_health(*, reads: list[dict], now: datetime | None = None,
                  stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
                  alert: Callable | None = None) -> dict:
    """汇总信号源健康：ok=False → unknown；尾部年龄超阈 → stale；否则 clean。

    alert 端口注入后按意图键去重（重复触发不重复告警）；不注入时只返回报告。
    """
    entries: list[dict] = []
    alerts: list[dict] = []
    for read in reads:
        name = str(read.get("source") or "unknown-source")
        ok = bool(read.get("ok"))
        tail = read.get("tail_age_seconds")
        error = str(read.get("error") or "")
        if not ok:
            source_state = "unknown"
        elif tail is not None and float(tail) > stale_after_seconds:
            source_state = "stale"
        else:
            source_state = "clean"
        entry = {"source": name, "state": source_state, "error": error,
                 "tailAgeSeconds": None if tail is None else round(float(tail), 1)}
        entries.append(entry)
        if source_state in {"unknown", "stale"}:
            payload = {"source": name, "state": source_state,
                       "error": error[:200], "tailAgeSeconds": entry["tailAgeSeconds"]}
            intent = f"wakeup|source|{name}|{source_state}"
            if alert is not None:
                if alert(intent=intent, payload=payload):
                    alerts.append({"intent": intent, **payload})
            else:
                alerts.append({"intent": intent, **payload})
    tails = [entry["tailAgeSeconds"] for entry in entries
             if entry["tailAgeSeconds"] is not None]
    return {"sources": entries,
            "healthy": bool(entries) and all(e["state"] == "clean" for e in entries),
            "tailAgeSeconds": max(tails) if tails else None,
            "alerts": alerts}


def membership_active(session: Session, *, org_id: str, principal_id: str,
                      now: datetime | None = None) -> bool:
    """组织成员资格 guard：撤权/过期/未入职的成员不接收提醒与定时委派。"""
    from evoharness.organization.models import Membership

    now = now or utcnow()
    row = session.scalar(select(Membership).where(
        Membership.org_id == org_id, Membership.principal_id == principal_id))
    if row is None or row.status != "active":
        return False
    if row.valid_from is not None and row.valid_from > now:
        return False
    if row.valid_until is not None and now >= row.valid_until:
        return False
    return True


# ── 长期协调职责：结构化提案，核验后执行 ─────────────────────────────────────

def coordinate(session: Session, task_id: int, *, actor: str, kind: str,
               based_on_revision: int, payload: dict | None = None,
               proposal_id: str | None = None, evidence_refs: list[str] | None = None,
               validate_context: Callable | None = None,
               recipient_ok: Callable[[str], bool] | None = None,
               output_root: str = "out", now: datetime | None = None) -> dict:
    """协调提案：先核验 revision 与授权主体，再执行；无授权不执行、不只发提醒。

    - clarify_goal → 复用 service.request_clarification（非阻塞默认，不擅自改状态）。
    - request_run  → 同一 Run 服务（execution.request_run）复核授权/版本/幂等。
    - 其余种类     → 提案记录即执行，无外部副作用。
    同一 proposalId 重放安全：返回 duplicate，不重复执行。
    """
    now = now or utcnow()
    payload = payload or {}
    if kind not in PROPOSAL_KINDS:
        raise WakeupError("invalid_proposal_kind")
    task = session.get(Task, task_id)
    if task is None:
        raise WakeupError("task_not_found")
    if int(based_on_revision) != task.revision:
        raise WakeupError("stale_task_revision")
    # 授权主体核验在记录之前：无授权的提案不留执行痕迹。
    if recipient_ok is not None and not recipient_ok(actor):
        raise WakeupError("proposal_not_authorized")
    if recipient_ok is None and validate_context is None:
        raise WakeupError("authorization_adapter_required")
    pid = proposal_id or f"proposal|{task.task_key}|{kind}|{task.revision}"
    existing = _find_proposal_event(session, task_id, pid)
    if existing is not None:
        return {"proposalId": pid, "kind": kind, "executed": False, "duplicate": True}
    record = {"proposalId": pid, "basedOnRevision": int(based_on_revision),
              "payload": payload, "evidenceRefs": list(evidence_refs or [])}
    if kind == "request_run":
        session.add(TaskEvent(task_id=task.id, event_type="proposal", actor=actor,
                              detail="request_run", created_at=now,
                              evidence_message_ids=json.dumps(record, ensure_ascii=False)))
        session.flush()
    else:
        service.propose(session, task_id, actor=actor,
                        kind=_PROPOSE_EVENT_KIND[kind], payload=record)
    result: dict = {"proposalId": pid, "kind": kind, "executed": True}
    if kind == "clarify_goal":
        question = str(payload.get("question") or "").strip()
        if not question:
            raise WakeupError("clarification_question_required")
        service.request_clarification(session, task_id, actor=actor, question=question,
                                      blocking=bool(payload.get("blocking", False)))
    elif kind == "request_run":
        if validate_context is None:
            raise WakeupError("authorization_adapter_required")
        run = execution.request_run(
            session, task_id=task.id,
            context={"task_key": task.task_key, "revision": task.revision,
                     "proposal_id": pid},
            actor_id=actor, output_root=output_root, idempotency_key=pid,
            validate_context=validate_context)
        result["runId"] = run.id
    session.flush()
    return result


def _find_proposal_event(session: Session, task_id: int, pid: str) -> TaskEvent | None:
    rows = session.scalars(select(TaskEvent).where(
        TaskEvent.task_id == task_id, TaskEvent.event_type == "proposal"
    ).order_by(TaskEvent.id.desc()).limit(50))
    for row in rows:
        data = _load_json(row.evidence_message_ids, {})
        if data.get("proposalId") == pid:
            return row
    return None


def propose_next_steps(session: Session, *, actor: str, now: datetime | None = None,
                       validate_context: Callable | None = None,
                       recipient_ok: Callable[[str], bool] | None = None,
                       output_root: str = "out") -> list[dict]:
    """长期协调职责：基于在途变更生成结构化"有效下一步"，经核验后执行。

    确定性规则（有界）：
    - needs_confirmation 有阻塞问题 → clarify_goal；
    - waiting_authorization/budget/resource → raise_blocker（升级责任人，不自动派 Agent）；
    - waiting_external/waiting_acceptance → reconcile_progress；
    - waiting_dependency 且依赖全部终态 → request_run（Run 服务复核授权）；
    - 存在未消费的在途取消 → reconcile_progress。
    """
    now = now or utcnow()
    results: list[dict] = []
    for task in session.scalars(select(Task).where(
            Task.status.in_(sorted(state.ACTIVE)))):
        for kind, payload, evidence in _next_step_proposals(session, task):
            try:
                outcome = coordinate(session, task.id, actor=actor, kind=kind,
                                     based_on_revision=task.revision, payload=payload,
                                     proposal_id=payload.get("proposalId"),
                                     evidence_refs=evidence,
                                     validate_context=validate_context,
                                     recipient_ok=recipient_ok,
                                     output_root=output_root, now=now)
            except WakeupError as exc:
                results.append({"taskId": task.id, "taskKey": task.task_key,
                                "kind": kind, "executed": False, "rejected": True,
                                "reason": exc.code})
                continue
            except ValueError as exc:  # Run 服务等入口的错误码原样上报
                results.append({"taskId": task.id, "taskKey": task.task_key,
                                "kind": kind, "executed": False, "rejected": True,
                                "reason": str(exc)})
                continue
            outcome.update({"taskId": task.id, "taskKey": task.task_key})
            results.append(outcome)
    session.flush()
    return results


def _next_step_proposals(session: Session, task: Task) -> list[tuple[str, dict, list[str]]]:
    proposals: list[tuple[str, dict, list[str]]] = []
    waiting_reason = (task.waiting_reason or "").strip()
    rev = task.revision
    if task.status == TaskStatus.NEEDS_CONFIRMATION and waiting_reason:
        proposals.append(("clarify_goal",
                          {"question": waiting_reason,
                           "proposalId": f"proposal|{task.task_key}|clarify_goal|{rev}"},
                          task.source_ids()[:5]))
    if task.status in {"waiting_authorization", "waiting_budget", "waiting_resource"}:
        proposals.append(("raise_blocker",
                          {"blocker": waiting_reason or task.status,
                           "proposalId": f"proposal|{task.task_key}|raise_blocker|{rev}"},
                          []))
    if task.status in {"waiting_external", "waiting_acceptance"}:
        proposals.append(("reconcile_progress",
                          {"subject": waiting_reason or task.status,
                           "proposalId": f"proposal|{task.task_key}|reconcile_progress|{rev}"},
                          []))
    if task.status == "waiting_dependency" and state.is_delegated(task):
        deps = [str(d) for d in _load_list(task.dependencies_json) if str(d).strip()]
        if deps:
            pending = []
            for dep_key in deps:
                dep = session.scalar(select(Task).where(
                    Task.tenant == task.tenant, Task.task_key == dep_key))
                if dep is None or dep.status not in state.TERMINAL:
                    pending.append(dep_key)
            if pending:
                proposals.append(("raise_blocker",
                                  {"blocker": f"dependency_pending:{','.join(pending)}",
                                   "proposalId": f"proposal|{task.task_key}|raise_blocker|dep-{rev}"},
                                  []))
            else:
                proposals.append(("request_run",
                                  {"trigger": "dependency_resolved",
                                   "proposalId": f"proposal|{task.task_key}|request_run|{rev}"},
                                  []))
    if supplements.has_pending_cancellation(session, task.id):
        proposals.append(("reconcile_progress",
                          {"subject": "cancellation_pending",
                           "proposalId": f"proposal|{task.task_key}|reconcile_progress|cancel-{rev}"},
                          []))
    return proposals
