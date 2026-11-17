"""工作项单一状态机：Task.status 单源，legacy 消费者取四态投影。"""
TERMINAL = frozenset({"done", "cancelled"})
ACTIVE = frozenset({"needs_confirmation", "open", "ready", "queued", "running",
                    "waiting_authorization", "waiting_dependency", "waiting_budget",
                    "waiting_resource", "waiting_external", "waiting_acceptance",
                    "rework", "paused", "failed"})
# 等待类状态必须记录原因、责任人与期限/唤醒条件（指南 §6.1）。
WAITING = frozenset({"waiting_authorization", "waiting_dependency", "waiting_budget",
                     "waiting_resource", "waiting_external"})

# 统一转换表：done 只能从 in_review 进入且委派交付必须经验收服务；
# done→queued 仅限携带新失败证据的显式重开。
# running→queued 是 R06 的恢复转换：Run 崩溃恢复对账通过后回队列重新认领，
# 只允许 execution.resume（actor=recovery，留 TaskEvent）触发，不得绕过状态机直写。
TRANSITIONS = {
    "needs_confirmation": {"open", "ready", "cancelled"},
    "open": {"ready", "queued", "cancelled", "needs_confirmation"},
    "ready": {"queued", "cancelled", "needs_confirmation"},
    "queued": {"running", "ready", "paused", "cancelled", "needs_confirmation"},
    "running": {"queued", "verifying", "waiting_acceptance", "failed", "paused",
                "cancelled", "waiting_authorization", "waiting_dependency",
                "waiting_budget", "waiting_resource", "waiting_external"},
    "verifying": {"in_review", "running", "failed", "cancelled"},
    "waiting_acceptance": {"in_review", "running", "cancelled"},
    "in_review": {"done", "running", "cancelled"},
    "rework": {"running", "cancelled"},
    "failed": {"queued", "cancelled"},
    "paused": {"queued", "running", "cancelled"},
    "waiting_authorization": {"queued", "cancelled"},
    "waiting_dependency": {"queued", "cancelled"},
    "waiting_budget": {"queued", "cancelled"},
    "waiting_resource": {"queued", "cancelled"},
    "waiting_external": {"queued", "cancelled"},
    "done": {"queued"},       # 明确重开：需新失败证据
    "cancelled": set(),
}


def legacy_status(task):
    """旧 API/日报只看四态；新状态不因旧 SQL 只查 open 而消失。"""
    if task.status in TERMINAL or task.status in ("needs_confirmation",):
        return task.status
    return "open"


def is_delegated(task):
    return task.workflow_kind == "delegated_delivery"


def can_transitions(task):
    return set(TRANSITIONS.get(task.status, set()))


def waiting_requires_reason(status):
    return status in WAITING
