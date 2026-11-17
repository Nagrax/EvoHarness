"""模型侧协调提案（DE-10/BP-03）：只生成事件，不改状态/合同/权限。

模型在最终回答中以围栏 JSON 输出 CoordinationProposal：

```coordination_proposal
{"kind": "clarify", "payload": {...}, "evidence_refs": ["..."], "expected_effect": "..."}
```

适配器解析后交给本服务；应用服务用**可信主体**（Run principal）与目标
revision 校验，写入 TaskEvent(type=proposal)。提案不能 done、发信、改授权
或启动工具；重复提案按内容键幂等，过期 revision 拒绝，模型自报 actor 被忽略。
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select

from evoharness.ids import sha256_json
from evoharness.models import Task, TaskEvent
from evoharness.work import service
from evoharness.work.models import ExecutionRun

PROPOSAL_KINDS = frozenset({"relate", "clarify", "plan", "progress", "block"})
_BLOCK = re.compile(r"```(?:coordination_proposal|proposal)\s*(\{.*?\})\s*```", re.S)


def extract_proposal(text: str) -> dict | None:
    """从模型最终回答提取围栏 JSON；无块返回 None，坏 JSON 标记 malformed。"""
    match = _BLOCK.search(text or "")
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(1))
    except ValueError:
        return {"_malformed": True}
    return parsed if isinstance(parsed, dict) else {"_malformed": True}


class CoordinationProposalSink:
    """把模型提案写入业务事件流；只记录，不执行。"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def submit(self, *, run_id: str, task_id, actor: str,
               proposal: dict) -> dict:
        if not isinstance(proposal, dict):
            return {"ok": False, "reason": "invalid_proposal"}
        if proposal.get("_malformed"):
            return {"ok": False, "reason": "malformed_proposal"}
        kind = str(proposal.get("kind") or "").strip()
        if kind not in PROPOSAL_KINDS:
            return {"ok": False, "reason": f"invalid_proposal_kind:{kind}"}
        payload = proposal.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        refs = [str(v) for v in (proposal.get("evidence_refs") or [])
                if str(v).strip()]
        if not refs:
            return {"ok": False, "reason": "proposal_evidence_required"}
        effect = str(proposal.get("expected_effect") or "").strip()
        key = sha256_json({"task": int(task_id), "kind": kind,
                           "payload": payload, "refs": sorted(refs)})[:24]
        with self.session_factory.begin() as session:
            run = session.get(ExecutionRun, str(run_id))
            task = session.get(Task, int(task_id))
            if run is None or task is None:
                return {"ok": False, "reason": "proposal_target_missing"}
            if run.task_revision != task.revision:
                return {"ok": False, "reason": "stale_task_revision"}
            for row in session.scalars(select(TaskEvent).where(
                    TaskEvent.task_id == task.id,
                    TaskEvent.event_type == "proposal")):
                try:
                    body = json.loads(row.evidence_message_ids or "[]")
                except ValueError:
                    body = {}
                if isinstance(body, dict) and body.get("_key") == key:
                    return {"ok": True, "reason": "proposal_duplicate",
                            "event_id": row.id}
            body = {"_key": key, "proposal": payload,
                    "evidence_refs": refs, "expected_effect": effect}
            # 可信主体：Run principal（模型自报 actor 不参与身份判定）
            event = service.propose(session, task.id,
                                    actor=str(actor or run.actor_id),
                                    kind=kind, payload=body)
            return {"ok": True, "reason": "proposal_recorded",
                    "event_id": event.id, "kind": kind,
                    "evidence_refs": refs}
