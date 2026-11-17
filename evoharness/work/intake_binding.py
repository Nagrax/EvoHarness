"""根 WorkResolver（DE-04/BP-03）：把已授权 intake 消息绑定到产品 Task。

- 输入是**已认证** IntakeScope（tenant/app/org/space/owner/...）与来源消息；
- 只读产品库，按租户+组织+空间+来源引用查找 Task，不猜归属；
- 显式 ``task_ref``（``work_item:<id>``）也必须在同一作用域内；
- 无候选/多候选/已收口/缺验收都返回结构化拒绝，不创建空任务或公共默认。
"""
from __future__ import annotations

import json

from sqlalchemy import select

from evoharness.models import Task


class WorkBindingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _scope_values(scope) -> tuple[str, str, str]:
    values = tuple(str(getattr(scope, name, "") or "").strip()
                   for name in ("tenant", "org_ref", "space_ref"))
    if not all(values):
        raise WorkBindingError("missing_context_binding")
    return values


def _source_refs(task: Task) -> list[str]:
    try:
        return [str(v) for v in json.loads(task.source_message_ids or "[]")]
    except ValueError:
        return []


def _scope_ok(task: Task, tenant: str, org: str, space: str) -> bool:
    return (task.tenant == tenant and task.org_id == org
            and task.space_id == space)


def _binding(task: Task) -> dict:
    acceptance = []
    try:
        acceptance = json.loads(task.acceptance_json or "[]")
    except ValueError:
        acceptance = []
    if not acceptance:
        raise WorkBindingError("missing_acceptance")
    if task.status in {"done", "cancelled"}:
        raise WorkBindingError("work_item_closed")
    return {"work_ref": f"work_item:{task.id}", "revision": int(task.revision or 1),
            "acceptance_ref": f"work_item:{task.id}@revision:{task.revision}",
            "task_id": task.id, "org_id": task.org_id, "space_id": task.space_id}


def resolve_work_binding(session, message: dict, scope) -> dict:
    """解析消息 → 唯一工作项绑定；歧义/缺失/越界一律拒绝。"""
    tenant, org, space = _scope_values(scope)
    message = dict(message or {})
    explicit = str(message.get("task_ref") or "").strip()
    if explicit:
        raw = explicit.split(":", 1)[1] if explicit.startswith("work_item:") else explicit
        try:
            task_id = int(raw)
        except (TypeError, ValueError):
            raise WorkBindingError("invalid_work_ref") from None
        task = session.get(Task, task_id)
        if task is None or not _scope_ok(task, tenant, org, space):
            raise WorkBindingError("work_binding_scope_mismatch")
        return _binding(task)

    message_id = str(message.get("message_id") or "").strip()
    source_id = message.get("source_id")
    needles = {value for value in (message_id, f"source:{source_id}"
                                  if source_id not in (None, "") else "") if value}
    if not needles:
        raise WorkBindingError("missing_work_binding")
    candidates = []
    for task in session.scalars(select(Task).where(Task.tenant == tenant,
                                                   Task.org_id == org,
                                                   Task.space_id == space)):
        if needles & set(_source_refs(task)):
            candidates.append(task)
    if not candidates:
        raise WorkBindingError("missing_work_binding")
    if len({task.id for task in candidates}) > 1:
        raise WorkBindingError("ambiguous_work_binding")
    return _binding(candidates[0])


def make_work_resolver(session_factory):
    """给 intake.run(work_resolver=...) 的装配适配器：每次调用独立短会话。"""
    def resolve(message: dict, scope) -> dict:
        with session_factory() as session:
            return resolve_work_binding(session, message, scope)
    return resolve
