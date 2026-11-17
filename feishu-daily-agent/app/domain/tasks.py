"""Channel extraction adapter; all Task writes use the product core service."""
from evoharness.work.service import apply_completion_signal, set_status, open_tasks
from evoharness.work.service import extract_and_upsert as _upsert
from evoharness.work.objectives import milestones_for_task, objective_progress
from evoharness.work.objectives import record_task_evidence as _record_task_evidence
from evoharness.work.plans import plan_overview, plan_staleness
from app.core.config import get_settings
from app.llm.service import extract_tasks

def extract_and_upsert(session, message, *, source=None):
    payload = extract_tasks(message.content_text, message.message_id)
    return _upsert(session, message, payload=payload, source=source,
                   default_owner=get_settings().feishu_owner_open_id)


# ── DE-39 最小接线：目标/计划服务经本适配器暴露给通道层 ─────────────────────
# 只读投影与证据回写入口；四态语义与 done 唯一入口（DE-13 验收服务）不变。

def task_objective_refs(session, task):
    """只读：任务覆盖的里程碑与目标引用；不写任何状态，不改四态语义。"""
    return [{"milestone_id": milestone.milestone_id,
             "objective_id": milestone.objective_id,
             "milestone_status": milestone.status}
            for milestone in milestones_for_task(session, task_id=task.id)]


def record_acceptance_evidence(session, *, task, actor_id, artifact_refs=(), note=""):
    """验收服务 done 之后的证据回写（结果返回目标）。

    聊天完成信号与 Run 退出不得调用：task 必须已是 DE-13 验收通过的 done；
    只更新该任务覆盖的里程碑，不关闭上层目标。
    """
    return _record_task_evidence(session, task_id=task.id, actor_id=actor_id,
                                 artifact_refs=artifact_refs, note=note)
