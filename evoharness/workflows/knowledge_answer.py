"""内部答疑 SOP 工作流（DE-19）：检索 → 冲突识别 → 有来源回答 → 草稿批准 → 受限自动回复 → 升级链。

统一一条可调用工作流（总体指南 §9.3 首批 SOP「内部知识答疑」：验证来源范围 →
检索 → 引用/冲突 → 草稿/受限回复）：

- 来源范围先行：受众 scope 由调用方显式传入；检索裁剪复用 DE-16
  ``evoharness.knowledge.service`` 的 owner_scope/include_private 语义，
  工作流层不放大任何权限（私有知识不因受众不同而扩散）。
- 找不到证据：返回"缺什么"的缺失清单并升级到授权配置的责任人，不杜撰答案；
  无人配置时明确等待（awaiting_escalation_target），不假回答。
- 冲突证据：同 subject 不同口径并存时全部带来源呈报（不猜一个），并请求责任人裁决。
- 草稿批准：承诺/敏感内容强制走批准（DE-12 governance 决定语义）；批准只绑定
  subject+受众+主张版本集合（payload hash），批准后原范围内复用（用户已确认语义）。
- 受限自动回复：仅在批准范围内 + 低风险 + 独立冻结阈值之上自动发送意图；
  低风险 FAQ 有独立留出口径（``ORG_FAQ_HOLDOUT``），与原 F08/F09 golden 阈值无关。
- 升级链：无证据/低置信 → 配置责任人（organization 授权配置，不从消息或群里猜
  owner）；授权人纠正以 knowledge 服务的 supersede/失效语义落库，旧版本不再复用。
- 回流：回复/纠正经 DE-10 work 服务的补充队列/事件回流原工作项（幂等去重）。

端口注入（不绑死飞书 SDK）：``AnswerSink`` 出站端口（draft / send_intent /
escalation 三个出口）与可注入的升级目标提供者；默认 ``RecordedSink`` 只记录事件
（离线/影子），不触达任何真实渠道。领域逻辑无飞书依赖，根组织可直接调用。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.governance import authorization
from evoharness.governance.authorization import AuthorizationError
from evoharness.ids import normalize_text, sha256_json, stable_id
from evoharness.knowledge import service as knowledge
from evoharness.models import SystemState, Task
from evoharness.organization.models import BusinessSpace, Organization
from evoharness.work import supplements
from evoharness.work import service as work_service


class KnowledgeAnswerError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# 确定性风险口径：与 feishu-daily-agent/app/domain/policy.py::answer_safety 的
# 敏感/承诺两条硬门保持同一词表（对齐由飞书侧回归保证）；工作流核心不依赖飞书，
# 因此在此维护等价正则，词表变更必须两侧同步。
_SECRET_RE = re.compile(
    r"(密码|口令|token|secret|api[_ -]?key|身份证|银行卡|信用卡|验证码|手机号|private key)",
    re.I,
)
_PROMISE_RE = re.compile(r"(保证|承诺|一定会|肯定能|绝对|包过|100%|百分百)", re.I)

# 低风险 FAQ 的独立冻结留出口径（组织答疑专用）。与 feishu golden F08/F09 的
# thresholds 互不读取、互不修改；调整此值必须连带更新回放与验收记录。
ORG_FAQ_HOLDOUT = {
    "auto_min_confidence": 0.92,
    "draft_min_confidence": 0.5,
    "note": ("组织答疑独立冻结阈值：与 feishu golden（F08/F09）无关，"
             "复用 eval/replay.py 的同一评估口径但不共阈值"),
}


# ── 确定性口径 ────────────────────────────────────────────────────────────────


def content_risk_flags(text: str) -> list[str]:
    """承诺/敏感内容识别（与 policy.answer_safety 的两条硬门口径一致）。"""
    flags: list[str] = []
    if _SECRET_RE.search(text or ""):
        flags.append("sensitive_content")
    if _PROMISE_RE.search(text or ""):
        flags.append("promise_or_commitment")
    return flags


def answer_confidence(claims: list[dict], conflicts: list[str]) -> float:
    """确定性置信：冲突/多口径=0.5；单源=0.92；多源一致=0.97；无证据=0.0。"""
    if conflicts or len({claim["value"] for claim in claims}) > 1:
        return 0.5
    if not claims:
        return 0.0
    return 0.92 if len(claims) == 1 else 0.97


def compose_sourced_answer(subject: str, claims: list[dict]) -> str:
    """有来源回答：答案文本由主张 value + 显式来源引用确定性合成，不调模型。"""
    lines: list[str] = []
    for claim in claims:
        line = f"{claim['value']}（来源：{', '.join(claim.get('source_refs') or [])}）"
        if line not in lines:
            lines.append(line)
    return f"关于「{subject}」：" + "；".join(lines)


def answer_payload(subject: str, audience_scope: str, claims: list[dict]) -> dict:
    """批准与复用绑定的载荷：subject + 受众 + 主张版本集合（hash 后进 Decision）。"""
    return {
        "subject": subject,
        "audience_scope": audience_scope,
        "claims": [{
            "claim_id": claim["claim_id"],
            "value": claim["value"],
            "source_revision": int(claim.get("source_revision") or 1),
        } for claim in claims],
    }


def reuse_target_id(org_id: str, space_id: str, subject: str) -> str:
    """批准对象按 subject 级建（受众隔离由 Decision.grantee_id 承担），
    授权人纠正时可以一次撤回该 subject 全部受众的旧批准。"""
    return stable_id("knowledge_answer", org_id, space_id, normalize_text(subject))


def payload_revision(payload: dict) -> int:
    claims = payload.get("claims") or []
    return max((int(claim.get("source_revision") or 1) for claim in claims), default=1)


# ── 出站端口 ──────────────────────────────────────────────────────────────────


@dataclass
class RecordedSink:
    """影子/离线出站端口：只记录事件供断言与审计，不触达真实渠道。

    适配层可实现同名三方法（draft / send_intent / escalation）接入
    ReplyDraft/outbox 等真实出口；工作流只做 duck-typing 调用。
    """
    events: list[dict] = field(default_factory=list)

    def _record(self, event: dict) -> str:
        self.events.append(dict(event))
        return stable_id("sink_event", json.dumps(event, ensure_ascii=False, sort_keys=True))

    def draft(self, event: dict) -> str:
        return self._record({**event, "kind": "draft"})

    def send_intent(self, event: dict) -> str:
        return self._record({**event, "kind": "send_intent"})

    def escalation(self, event: dict) -> str:
        return self._record({**event, "kind": "escalation"})


# ── 升级链 ────────────────────────────────────────────────────────────────────


def configured_escalation_target(session: Session, *, org_id: str = "",
                                 space_id: str, subject: str = "") -> dict | None:
    """升级目标读取授权配置（空间/组织责任人），不从消息或群里猜 owner。"""
    space = session.get(BusinessSpace, space_id)
    if (space is not None and space.owner_principal_id.strip()
            and (not org_id or space.org_id == org_id)):
        return {"kind": "space_owner", "principal_id": space.owner_principal_id}
    if org_id:
        org = session.get(Organization, org_id)
        if org is not None and org.owner_principal_id.strip():
            return {"kind": "org_owner", "principal_id": org.owner_principal_id}
    return None


# ── source event 去重 ─────────────────────────────────────────────────────────


def _seen_key(space_id: str, source_event_id: str) -> str:
    return f"ka:seen:{stable_id(space_id, source_event_id)}"


def _mark_seen(session: Session, *, key: str, value: str) -> bool:
    """幂等登记（SystemState 单键唯一）；并发重复插入按已存在处理。"""
    if session.get(SystemState, key) is not None:
        return False
    try:
        with session.begin_nested():
            session.add(SystemState(key=key, value=value))
            session.flush()
    except IntegrityError:
        return False
    return True


# ── 回流原工作项（DE-10 补充队列/事件） ───────────────────────────────────────


def reflow_to_work_item(session: Session, *, work_item_id: int, event_id: str,
                        actor: str, kind: str, summary: str) -> dict | None:
    """回复/纠正回流原工作项：补充队列 + 任务事件，双入口都按 source event 幂等。"""
    task = session.get(Task, work_item_id)
    if task is None:
        return None
    supplements.enqueue(session, work_item_id, event_id=event_id, event_kind="note",
                        payload={"kind": kind, "summary": summary[:500]})
    event = work_service.append_note(session, task, message_id=event_id,
                                     note=summary, actor=actor)
    duplicate = event.event_type == "note_ignored_duplicate"
    return {"work_item_id": work_item_id, "task_event": event.event_type,
            "duplicate": duplicate}


def _summary(status: str, subject: str, detail: str = "") -> str:
    body = f"[组织答疑:{status}] {subject}"
    return f"{body} {detail}".strip()


# ── 主工作流 ──────────────────────────────────────────────────────────────────


def answer_question(session: Session, *, space_id: str, subject: str,
                    source_event_id: str, org_id: str = "",
                    audience_scope: str = "", audience_id: str = "",
                    work_item_id: int | None = None,
                    escalation_target_provider: Callable | None = None,
                    sink=None, auto_enabled: bool = False,
                    min_confidence: float | None = None,
                    now=None) -> dict:
    """组织答疑 SOP 单次调用：检索→冲突→回答→批准→受限自动回复→升级。

    - audience_scope：提问者受众 scope（如成员 open_id/部门 scope）；为空表示
      只允许公开知识。工作流只透传给知识服务裁剪，不做任何权限放大。
    - auto_enabled：自动发送开关（默认 False = 保守出草稿）。即使开启，自动发送
      也只在已有 active 批准覆盖当前载荷（批准即原范围复用）且低风险、置信达标
      时发生；首答一律出草稿待批准。
    - min_confidence：低风险 FAQ 独立置信阈值；缺省取 ORG_FAQ_HOLDOUT 冻结值。
    """
    now = now or utcnow()
    space_id = (space_id or "").strip()
    subject = (subject or "").strip()
    source_event_id = (source_event_id or "").strip()
    if not space_id:
        raise KnowledgeAnswerError("space_required")
    if not subject:
        raise KnowledgeAnswerError("subject_required")
    if not source_event_id:
        raise KnowledgeAnswerError("source_event_required")

    answer_id = stable_id("knowledge_answer", org_id, space_id,
                          normalize_text(subject), source_event_id)
    sink = sink if sink is not None else RecordedSink()

    # 1) source event 去重：同一事件只产生一份草稿/一次发送意图
    key = _seen_key(space_id, source_event_id)
    previous = session.get(SystemState, key)
    if previous is not None:
        return {
            "answer_id": previous.value or answer_id, "status": "duplicate",
            "duplicate": True, "kind": "", "org_id": org_id, "space_id": space_id,
            "subject": subject, "audience_scope": audience_scope,
            "audience_id": audience_id, "source_event_id": source_event_id,
            "answer": None, "missing": [], "escalation": None,
            "delivery": {"mode": "none", "reasons": ["duplicate_source_event"],
                         "sink_event_ids": [], "decision_id": ""},
            "reflow": None,
        }
    _mark_seen(session, key=key, value=answer_id)

    # 2) 检索（受众裁剪；复用 DE-16，不放大权限）
    result = knowledge.search(session, space_id=space_id, subject=subject,
                              owner_scope=audience_scope,
                              include_private=bool(audience_scope), now=now)
    claims = result["claims"]
    # 受众内冲突判定：只对该受众可见的主张集合判冲突；不可见的私有口径
    # 既不影响本受众的答案，也不会经冲突呈报泄漏（不放大、也不缩小可见权限）。
    conflicts = ([claim["claim_id"] for claim in claims]
                 if len({claim["value"] for claim in claims}) > 1 else [])

    identity = {
        "org_id": org_id, "space_id": space_id, "subject": subject,
        "audience_scope": audience_scope, "audience_id": audience_id,
        "source_event_id": source_event_id, "answer_id": answer_id,
    }

    def escalation_target() -> dict | None:
        if escalation_target_provider is not None:
            return escalation_target_provider(session, org_id=org_id,
                                              space_id=space_id, subject=subject)
        return configured_escalation_target(session, org_id=org_id,
                                            space_id=space_id, subject=subject)

    def reflow(kind: str, detail: str) -> dict | None:
        if work_item_id is None:
            return None
        return reflow_to_work_item(session, work_item_id=work_item_id,
                                   event_id=source_event_id,
                                   actor=audience_id or "knowledge_answer",
                                   kind=kind, summary=_summary(status="", subject=subject) + " " + detail)

    # 3) 无证据：缺失清单 + 升级，不杜撰
    if not claims:
        missing = [{
            "missing": "authoritative_claim", "subject": subject,
            "space_id": space_id, "audience_scope": audience_scope,
            "need": "带显式来源引用的权威口径（knowledge.service.record_claim 登记）",
        }]
        if result.get("expired_hint"):
            missing[0]["expired_hint"] = result["expired_hint"]
            missing[0]["need"] = "既有主张已过有效期：回源核实后重新登记，不沿用旧值"
        target = escalation_target()
        event_ids: list[str] = []
        if target is not None:
            event_ids.append(sink.escalation({
                **identity, "kind": "escalation", "reason": "no_evidence",
                "missing": missing, "target": target, "confidence": 0.0,
            }))
            status = "escalated"
        else:
            # 无人配置升级目标：明确等待，不假回答
            status = "awaiting_escalation_target"
        outcome = {
            "answer_id": answer_id, "status": status, "duplicate": False,
            "kind": "no_evidence", **identity, "answer": None,
            "missing": missing,
            "escalation": ({"reason": "no_evidence", "target": target,
                            "missing": missing, "confidence": 0.0}
                           if target is not None else None),
            "delivery": {"mode": "none", "reasons": ["no_evidence"],
                         "sink_event_ids": event_ids, "decision_id": ""},
        }
        outcome["reflow"] = reflow("escalation", "缺少证据，已升级责任人" if target else "缺少证据，等待升级责任人")
        return outcome

    # 4) 冲突：全部带来源呈报，不猜一个；请求责任人裁决
    if conflicts or len({claim["value"] for claim in claims}) > 1:
        confidence = answer_confidence(claims, conflicts)
        missing = [{
            "missing": "conflict_resolution",
            "claim_ids": [claim["claim_id"] for claim in claims],
            "detail": "同一规则存在并存口径，需授权责任人裁决并 supersede，不自动裁决",
        }]
        target = escalation_target()
        event_ids = [sink.draft({
            **identity, "kind": "draft", "requires": "review",
            "reasons": ["conflicting_claims"], "text": "",
            "claims": claims, "confidence": confidence, "risk_flags": [],
        })]
        if target is not None:
            event_ids.append(sink.escalation({
                **identity, "kind": "escalation", "reason": "conflict_unresolved",
                "missing": missing, "target": target, "confidence": confidence,
            }))
        outcome = {
            "answer_id": answer_id, "status": "conflict", "duplicate": False,
            "kind": "conflict", **identity, "answer": None, "missing": missing,
            "escalation": ({"reason": "conflict_unresolved", "target": target,
                            "missing": missing, "confidence": confidence}
                           if target is not None else None),
            "delivery": {"mode": "conflict_report",
                         "reasons": ["conflicting_claims"],
                         "sink_event_ids": event_ids, "decision_id": ""},
        }
        outcome["reflow"] = reflow("conflict", "冲突口径已呈报，等待责任人裁决")
        return outcome

    # 5) 有来源回答：确定性合成 + 批准/自动门
    confidence = answer_confidence(claims, conflicts)
    answer_text = compose_sourced_answer(subject, claims)
    risk_flags = content_risk_flags(answer_text)
    payload = answer_payload(subject, audience_scope, claims)
    payload_hash = sha256_json(payload)
    target_id = reuse_target_id(org_id, space_id, subject)
    threshold = (ORG_FAQ_HOLDOUT["auto_min_confidence"]
                 if min_confidence is None else float(min_confidence))

    decision_id = ""
    try:
        decision = authorization.validate(
            session, target_type="knowledge_answer", target_id=target_id,
            target_revision=payload_revision(payload), payload_hash=payload_hash,
            action="send_as_bot", grantee_id=audience_scope, now=now)
        decision_id = decision.decision_id
    except AuthorizationError:
        decision_id = ""

    event_ids = []
    if decision_id:
        # 批准范围内复用（原范围、同载荷）；发送开关仍单独生效
        reasons = ["approved_scope_reuse"]
        if auto_enabled:
            event_ids.append(sink.send_intent({
                **identity, "kind": "send_intent", "text": answer_text,
                "claims": claims, "confidence": confidence,
                "risk_flags": risk_flags, "reasons": reasons,
                "decision_id": decision_id,
            }))
            mode, status = "auto_send", "answered"
        else:
            event_ids.append(sink.draft({
                **identity, "kind": "draft", "requires": "none", "text": answer_text,
                "claims": claims, "confidence": confidence,
                "risk_flags": risk_flags, "reasons": reasons + ["auto_send_disabled"],
                "decision_id": decision_id,
            }))
            mode, status = "draft_reusable", "answered"
    elif risk_flags:
        # 承诺/敏感内容必须走批准，不自动发送
        event_ids.append(sink.draft({
            **identity, "kind": "draft", "requires": "approval", "text": answer_text,
            "claims": claims, "confidence": confidence, "risk_flags": risk_flags,
            "reasons": risk_flags + ["risk_content_requires_approval"],
        }))
        mode, status = "draft_pending_approval", "pending_approval"
    elif confidence < threshold:
        # 低置信：升级到配置责任人，带具体缺失项；无人配置则明确等待
        missing = [{
            "missing": "higher_confidence_source",
            "claim_ids": [claim["claim_id"] for claim in claims],
            "detail": f"置信 {confidence} 低于冻结阈值 {threshold}，需权威来源复核",
        }]
        target = escalation_target()
        event_ids.append(sink.draft({
            **identity, "kind": "draft", "requires": "review", "text": answer_text,
            "claims": claims, "confidence": confidence, "risk_flags": risk_flags,
            "reasons": ["below_auto_threshold"],
        }))
        if target is not None:
            event_ids.append(sink.escalation({
                **identity, "kind": "escalation", "reason": "low_confidence",
                "missing": missing, "target": target, "confidence": confidence,
            }))
            status = "escalated"
        else:
            status = "awaiting_escalation_target"
        outcome = {
            "answer_id": answer_id, "status": status, "duplicate": False,
            "kind": "sourced", **identity,
            "answer": {"text": answer_text, "claims": claims,
                       "confidence": confidence, "risk_flags": risk_flags,
                       "payload": payload, "payload_hash": payload_hash,
                       "reuse_target_id": target_id},
            "missing": missing,
            "escalation": ({"reason": "low_confidence", "target": target,
                            "missing": missing, "confidence": confidence}
                           if target is not None else None),
            "delivery": {"mode": "escalation_review",
                         "reasons": ["below_auto_threshold"],
                         "sink_event_ids": event_ids, "decision_id": ""},
        }
        outcome["reflow"] = reflow("escalation", "低置信答案已升级责任人")
        return outcome
    else:
        # 首答低风险 FAQ：出草稿待批准（批准后原范围内自动复用）
        event_ids.append(sink.draft({
            **identity, "kind": "draft", "requires": "approval", "text": answer_text,
            "claims": claims, "confidence": confidence, "risk_flags": risk_flags,
            "reasons": ["first_answer_requires_approval"],
        }))
        mode, status = "draft_pending_approval", "pending_approval"

    outcome = {
        "answer_id": answer_id, "status": status, "duplicate": False,
        "kind": "sourced", **identity,
        "answer": {"text": answer_text, "claims": claims, "confidence": confidence,
                   "risk_flags": risk_flags, "payload": payload,
                   "payload_hash": payload_hash, "reuse_target_id": target_id},
        "missing": [], "escalation": None,
        "delivery": {"mode": mode,
                     "reasons": (["approved_scope_reuse"] if decision_id
                                 else ["first_answer_requires_approval"]),
                     "sink_event_ids": event_ids, "decision_id": decision_id},
    }
    outcome["reflow"] = reflow("answer", f"delivery={mode}")
    return outcome


# ── 批准与纠正 ────────────────────────────────────────────────────────────────


def approve_answer(session: Session, *, actor_id: str, space_id: str, subject: str,
                   answer_payload_dict: dict, org_id: str = "",
                   audience_scope: str = "", expires_at=None) -> dict:
    """授权人对草稿答案的批准：绑定 subject+受众+主张版本（payload hash）。

    批准后同载荷（同 subject、同受众可见主张集合）自动复用；载荷任何变化
    （知识修订/supersede/受众不同）都会使旧批准失效，重新走批准。
    """
    claims = answer_payload_dict.get("claims") or []
    if not claims:
        raise KnowledgeAnswerError("approve_requires_claims")
    if not actor_id.strip():
        raise KnowledgeAnswerError("approve_actor_required")
    decision = authorization.approve(
        session, actor_id, org_id=org_id, target_type="knowledge_answer",
        target_id=reuse_target_id(org_id, space_id, subject),
        target_revision=payload_revision(answer_payload_dict),
        payload_hash=sha256_json(answer_payload_dict),
        actions={"send_as_bot"}, grantee_id=audience_scope, expires_at=expires_at)
    return {"decision_id": decision.decision_id, "grantee_id": decision.grantee_id,
            "payload_hash": decision.payload_hash,
            "target_revision": decision.target_revision, "actions": ["send_as_bot"]}


def apply_correction(session: Session, *, actor_id: str, space_id: str, subject: str,
                     value: str, source_refs: list[str], org_id: str = "",
                     owner_scope: str = "public", predicate: str = "",
                     supersedes: str = "", invalidate_source_ref: str = "",
                     revoke_approval: bool = True) -> dict:
    """授权人纠正：以显式来源登记新主张（可 supersede 旧主张）。

    - supersede / invalidate 走 DE-16 knowledge 服务的既有语义：旧版本不再出现在
      检索结果中，因此旧答案不再被复用；旧批准同时撤回，未发送意图一并取消。
    - 冲突证据保留与来源引用要求与知识服务一致（不捏造权威）。
    """
    if not actor_id.strip():
        raise KnowledgeAnswerError("correction_actor_required")
    claim = knowledge.record_claim(
        session, org_id=org_id, space_id=space_id, owner_scope=owner_scope,
        subject=subject, predicate=predicate, value=value,
        source_refs=list(source_refs or []), supersedes=supersedes)
    invalidated: list[str] = []
    if invalidate_source_ref:
        invalidated = knowledge.invalidate_for_source(
            session, source_ref=invalidate_source_ref, reason="authorized_correction")
    cancelled_intents: list[str] = []
    if revoke_approval:
        cancelled_intents = authorization.revoke(
            session, actor_id, target_type="knowledge_answer",
            target_id=reuse_target_id(org_id, space_id, subject), org_id=org_id)
    return {"claim_id": claim.claim_id, "state": claim.state,
            "invalidated_claims": invalidated,
            "cancelled_intents": cancelled_intents}
