"""统一 API 契约层（DE-28）：工作台 / CLI / 服务端共用同一应用服务。

本层**不做业务判定**：状态机在 evoharness.work.state/service，验收在
evoharness.work.acceptance，授权与预算在 evoharness.governance，知识检索在
evoharness.knowledge.service，Run 租约在 evoharness.work.execution。
三条入口只负责：翻译参数 → 调应用服务 → 统一信封输出。

契约（指南 §11）：
- 每个操作返回统一信封 ``{ok, data, reason_code, retryable, next_action, evidence_refs}``；
- 错误码稳定可读，等待/冲突类区分可重试与否并给下一步动作；
- 长输出落产物目录并回引用（内联只留预览），Agent 可按 path + sha256 精确读取；
- API schema 先行声明（API_SCHEMA），页面/命令不得宣称 schema 之外的能力。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from evoharness.clock import utcnow
from evoharness.governance import authorization, budget
from evoharness.governance.authorization import AuthorizationError
from evoharness.governance.budget import BudgetError
from evoharness.governance.models import BudgetLimit, Decision
from evoharness.ids import sha256_json, stable_id
from evoharness.knowledge import service as knowledge_service
from evoharness.knowledge.service import KnowledgeError
from evoharness.models import Outbox, OutboxStatus, Task, TaskEvent
from evoharness.work import acceptance, execution, service as work_service, supplements
from evoharness.work.acceptance import AcceptanceError
from evoharness.work.models import Artifact, ExecutionRun
from evoharness.work.state import can_transitions
from evoharness.work.supplements import SupplementError

# ── 信封契约 ────────────────────────────────────────────────────────────────

ENVELOPE_KEYS = ("ok", "data", "reason_code", "retryable", "next_action", "evidence_refs")

# 内联上限：超出即落产物回引用（Agent 与页面都拿 ref，不内联超长内容）
MAX_INLINE_CHARS = 4000
PREVIEW_CHARS = 240
PAYLOAD_PREVIEW_CHARS = 400

# 可重试：等待类（授权到达/预算恢复后同一请求可成功）
RETRYABLE_TRUE = frozenset({
    "no_active_decision", "waiting_authorization",
    "budget_exhausted", "budget_not_configured",
})

# 错误码 → 下一步动作（没有就不给，不编造）
NEXT_ACTIONS = {
    "no_active_decision": "submit_approval",
    "waiting_authorization": "submit_approval",
    "decision_target_revision_changed": "refresh_target_and_reapprove",
    "decision_payload_changed": "refresh_target_and_reapprove",
    "decision_expired": "refresh_target_and_reapprove",
    "action_not_in_decision": "request_wider_decision",
    "decision_grantee_required": "act_as_bound_grantee_or_reapprove",
    "decision_grantee_mismatch": "act_as_bound_grantee_or_reapprove",
    "run_actor_required": "pass_actor_id",
    "actor_required": "pass_actor_id",
    "principal_required": "authenticate_with_registered_token",
    "principal_actor_mismatch": "authenticate_as_declared_actor",
    "principal_org_mismatch": "use_target_organization",
    "principal_org_not_authorized": "join_organization_or_use_admin_token",
    "principal_target_not_authorized": "request_target_permission",
    "idempotency_scope_conflict": "use_new_idempotency_key_for_this_scope",
    "budget_exhausted": "raise_budget_limit_or_wait",
    "budget_not_configured": "configure_budget_limit",
    "task_not_found": "check_object_id",
    "run_not_found": "check_object_id",
    "decision_not_found": "check_object_id",
    "stale_task_revision": "refresh_task_and_resubmit",
    "work_revision_changed": "refresh_task_and_resubmit",
    "run_intent_conflict": "refresh_task_and_resubmit",
    "invalid_task_transition": "check_allowed_transitions",
    "invalid_run_transition": "check_run_state",
    "run_lease_active": "worker_must_release_lease",
    "delivery_acceptance_required": "submit_acceptance_review",
    "acceptance_evidence_missing": "submit_acceptance_review",
    "acceptance_evidence_stale_revision": "submit_acceptance_review",
    "incomplete_work_contract": "complete_goal_acceptance_owner",
    "artifact_path_outside_output_root": "fix_artifact_path",
    "artifact_file_missing": "fix_artifact_path",
    "reviewer_not_authorized": "use_authorized_reviewer",
    "reviewer_must_differ_from_executor": "use_authorized_reviewer",
    "database_not_configured": "configure_evo_harness_database_url",
}

# 状态区分（验收场景 2）：每个展示态一个稳定 reason_code，等待类不得混入完成类
REASON_LABELS = {
    "ok": "正常",
    "awaiting_clarification": "待澄清",
    "ready_to_execute": "待启动",
    "run_queued": "已排队",
    "executing": "执行中",
    "verifying": "验证中",
    "rework": "返工中",
    "paused": "已暂停",
    "dryrun_not_live": "dryrun 试运行（未真实发送）",
    "waiting_authorization": "等待授权",
    "waiting_budget": "等待预算",
    "waiting_dependency": "等待依赖",
    "waiting_resource": "等待资源",
    "waiting_external": "等待外部",
    "awaiting_acceptance": "执行结束待验收",
    "awaiting_acceptance_review": "验收审阅中",
    "failed_needs_decision": "失败待处理",
    "receipt_unknown": "回执未知（未确认送达）",
    "delivered": "已交付",
    "cancelled": "已取消",
}

# 等待/未决类：任何投影都不得显示为已发送/已完成
NON_FINAL_CODES = frozenset({
    "awaiting_clarification", "ready_to_execute", "run_queued", "executing", "verifying",
    "rework", "paused", "dryrun_not_live", "waiting_authorization", "waiting_budget",
    "waiting_dependency", "waiting_resource", "waiting_external", "awaiting_acceptance",
    "awaiting_acceptance_review", "failed_needs_decision", "receipt_unknown",
})

TERMINAL_TASK_STATES = {"done", "cancelled"}

# 受信 Principal（R01/R02）：只由可信入口构造（hosted HTTP 层的 token→主体
# 映射注册表），形状 {"subject": str, "org_ids": [str], "admin": bool}。
# 正文 actor_id/grantee_id 只作为声明与受信主体比对，不作为权限主体。

# start_run 允许的任务状态：待澄清（合同未确认）不接受 Run
RUN_STARTABLE_STATES = {"open", "ready", "queued", "waiting_authorization"}


# ── API schema：先写清楚再实现，页面/命令按此对齐 ────────────────────────────

API_SCHEMA = {
    "envelope": {
        "keys": list(ENVELOPE_KEYS),
        "ok": "bool；false 时 reason_code 必读",
        "data": "操作结果对象；失败时可能带 detail",
        "reason_code": "稳定错误/状态码；成功为 ok",
        "retryable": "等待类 true（同一请求可重试）；冲突/输入类 false",
        "next_action": "建议下一步（submit_approval / refresh_task_and_resubmit …）",
        "evidence_refs": "可审计引用列表（run id、decision id、artifact path+sha256、消息 id）",
    },
    "display": {
        "buckets": ["pending", "in_progress", "awaiting_me", "delivered", "cancelled"],
        "reason_codes": sorted(REASON_LABELS),
        "note": "dryrun/等待授权/待验收/回执未知是独立 reason_code，不得显示为已发送或已完成",
    },
    "operations": {
        "set_budget_limit": {
            "params": ["org_id", "space_id", "limit_tokens", "actor"],
            "returns": ["scope_kind", "scope_id", "limit_tokens", "revision"],
            "note": "当前组织管理员设置组织或所属空间的累计 token 限额；零额度暂停新预留",
        },
        "skills_view": {
            "params": ["org_id", "space_id", "tenant", "owner", "legacy"],
            "returns": ["mode", "capabilities", "scope", "spaces", "has_report", "skills", "events"],
            "note": "成员身份取受信主体与当前成员资格；完整作用域、源哈希绑定评测，无全局报告回退",
        },
        "create_work_item": {
            "params": ["tenant", "owner", "title", "action", "chat_id", "goal", "acceptance",
                       "assignee_id", "assignee_name", "workflow_kind", "org_id", "space_id",
                       "due_text", "source_refs", "idempotency_key"],
            "returns": ["id", "task_key", "revision", "state", "next_action", "created",
                        "source_refs"],
        },
        "get_work_item": {
            "params": ["task_id"],
            "note": "hosted 模式按目标归属组织/当事人校验主体可见性，跨组织 ID 拒绝且不泄露正文（N01）",
            "returns": ["id", "revision", "state", "display", "goal", "acceptance", "owner",
                        "assignee", "source_refs", "next_action", "waiting_reason", "runs",
                        "artifacts", "receipts"],
        },
        "work_view": {
            "params": ["tenant", "owner", "org_id"],
            "note": "hosted 模式 owner 只取受信主体、org_id 限注册组织（N01）",
            "returns": ["scope", "buckets.pending", "buckets.in_progress",
                        "buckets.awaiting_me", "buckets.delivered", "buckets.cancelled",
                        "counts"],
        },
        "transition_work_item": {
            "params": ["task_id", "to_state", "actor", "reason", "wake_condition",
                       "expected_revision"],
            "note": "hosted 模式 actor 只取受信主体，写操作限任务当事人（N01）",
        },
        "pause_work_item": {"params": ["task_id", "actor", "reason"]},
        "complete_work_item": {
            "params": ["task_id", "actor", "evidence_message_ids"],
            "note": "仅普通待办；委派交付 done 唯一入口是 review_acceptance",
        },
        "cancel_work_item": {"params": ["task_id", "actor", "reason"],
                             "note": "hosted 模式 actor 只取受信主体，取消限任务当事人（N01）"},
        "request_clarification": {"params": ["task_id", "actor", "question", "blocking"]},
        "resolve_clarification": {"params": ["task_id", "actor", "answer_message_id"]},
        "submit_supplement": {
            "params": ["task_id", "event_id", "note", "actor"],
            "returns": ["dedup", "revision", "delivery"],
        },
        "run_request_payload": {
            "params": ["task_id", "brief"],
            "returns": ["target_type", "target_id", "target_revision", "payload",
                        "payload_hash"],
        },
        "start_run": {
            "params": ["task_id", "actor_id", "brief", "mode(live|dryrun)", "output_root",
                       "idempotency_key"],
            "returns": ["operation_id", "generation", "state", "task_state", "mode",
                        "operator_id", "decision_id"],
            "note": "Run 授权按 主体(grantee=操作者)×组织×目标×版本×载荷×动作 重验；"
                    "批准绑定 grantee 时操作者必须一致（R02）",
        },
        "pause_run": {"params": ["run_id"], "returns": ["run_id", "state"]},
        "request_acceptance": {
            "params": ["task_id", "run_id", "requester"],
            "note": "hosted 模式 requester 只取受信主体（N01）",
            "returns": ["task_state", "run_id"],
        },
        "review_acceptance": {
            "params": ["task_id", "run_id", "reviewer", "verdict(pass|fail)", "notes"],
            "note": "hosted 模式 reviewer 只取受信主体，owner/负责人资格由验收服务复核（N01）",
            "returns": ["task_state", "verdict"],
        },
        "learning_action": {
            "params": ["task_id", "evidence_id", "action(build|adopt)", "actor"],
            "returns": ["evidence_id", "action", "result"],
            "note": "从同一任务的验收意见生成候选；显式采用复用原技能版本与部署分区。",
        },
        "sync_knowledge": {
            "params": ["org_id", "space_id", "tenant", "actor"],
            "returns": ["completed", "revision", "build_sha256", "inputs", "processed", "changed"],
            "note": "固定本人的已批准来源清单，经来源校验后原子发布派生视图。",
        },
        "submit_approval": {
            "params": ["org_id", "actor_id", "target_type", "target_id", "target_revision",
                       "payload", "actions", "grantee_id", "expires_at"],
            "returns": ["decision_id", "payload_hash", "target_revision", "actions",
                        "actor_id"],
            "note": "hosted 模式 actor 只取受信 Principal（正文 actor_id 仅作声明校验），"
                    "并复核组织/owner/负责人/成员关系（R01）",
        },
        "get_approval": {
            "params": ["decision_id"],
            "returns": ["decision_id", "target_type", "target_id", "target_revision",
                        "actions", "identity", "payload_hash", "payload_diff",
                        "expires_at", "revoked", "superseded"],
        },
        "approvals_view": {"params": ["org_id"], "returns": ["approvals[]"],
                           "note": "hosted 模式限注册组织成员/管理员（N01）"},
        "revoke_approval": {
            "params": ["org_id", "actor_id", "target_type", "target_id"],
            "returns": ["revoked", "cancelled_intents"],
        },
        "search_knowledge": {
            "params": ["space_id", "subject", "owner_scope", "include_private"],
            "returns": ["claims", "conflicts", "expired_hint"],
        },
        "view_evidence": {
            "params": ["task_id"],
            "note": "hosted 模式按目标归属组织/当事人校验主体可见性（N01）",
            "returns": ["artifacts", "events", "runs", "receipts", "source_refs"],
        },
        "view_cost": {
            "params": ["org_id", "space_id", "principal_id"],
            "note": "hosted 模式成员口径收敛为本人 principal、org 限注册组织；管理员全量（N01）",
            "returns": ["reserved_open", "used_total", "by_category", "limits", "unknown"],
        },
        "view_health": {
            "params": ["org_id"],
            "returns": ["database", "runs", "receipts", "waiting", "budget", "knowledge"],
            "note": "分项事实+未知项+时间，不给裸 ok:true；hosted 模式成员视图收敛到注册组织（N01）",
        },
    },
}


def _parse_dt(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


def _preview_rows(full: str) -> int:
    """内联预览行数：截到 MAX_INLINE_CHARS 内的完整元素个数。"""
    try:
        rows = json.loads(full)
    except ValueError:
        return 0
    count, used = 0, 2
    for row in rows:
        chunk = json.dumps(row, ensure_ascii=False)
        if used + len(chunk) > MAX_INLINE_CHARS:
            break
        used += len(chunk) + 1
        count += 1
    return count


def _trim(value, limit: int = PREVIEW_CHARS):
    """内联预览用：超长字符串截断，结构保留。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, list):
        return [_trim(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _trim(item, limit) for key, item in value.items()}
    return value


class WorkApi:
    """应用服务的统一入口：构造一次，三条入口共用。"""

    def __init__(self, application, *, artifacts_dir=None, authorize=None,
                 acceptance_policy=None):
        self._app = application
        self._artifacts_dir = Path(artifacts_dir) if artifacts_dir else None
        self._authorize = authorize  # None → 默认授权适配器（走 governance.validate）
        # DE-13：部署模式决定验收回执策略（hosted=strict）；不污染全局环境
        self._acceptance_policy = acceptance_policy

    # ── 基础设施 ──

    @property
    def artifacts_dir(self) -> Path | None:
        return self._artifacts_dir

    def _session(self):
        return self._app.transaction()

    @staticmethod
    def _envelope(ok, data=None, reason_code="ok", retryable=False, next_action="",
                  evidence_refs=None) -> dict:
        return {
            "ok": bool(ok),
            "data": data if data is not None else {},
            "reason_code": reason_code,
            "retryable": bool(retryable),
            "next_action": next_action,
            "evidence_refs": evidence_refs or [],
        }

    @classmethod
    def _ok(cls, data=None, evidence_refs=None) -> dict:
        return cls._envelope(True, data, "ok", False, "", evidence_refs)

    @classmethod
    def _fail(cls, reason_code, detail=None, evidence_refs=None) -> dict:
        data = {}
        if detail is not None:
            data["detail"] = detail
        return cls._envelope(False, data, reason_code,
                             reason_code in RETRYABLE_TRUE,
                             NEXT_ACTIONS.get(reason_code, ""), evidence_refs)

    @classmethod
    def _convert(cls, exc: Exception) -> dict:
        """服务层异常 → 统一信封：code 原样透出（稳定错误码），冒号后缀进 detail。"""
        code = getattr(exc, "code", None) or str(exc) or "internal_error"
        primary = code.split(":", 1)[0]
        detail = None
        if ":" in code:
            detail = {"rest": code.split(":", 1)[1]}
        scope = getattr(exc, "scope", "")
        if isinstance(exc, BudgetError) and scope:
            detail = {"scope": scope}
        return cls._fail(primary, detail)

    # ── 受信 Principal 绑定（R01/R02）：操作者只取可信主体 ────────────────────

    @staticmethod
    def _principal_subject(principal) -> str:
        return str((principal or {}).get("subject") or "").strip()

    def _bind_principal_actor(self, principal, claimed) -> "str | dict":
        """操作者一律取受信主体；正文声明与受信主体不一致时显式拒绝。

        返回 str=绑定的操作者；返回 dict=拒绝信封（调用方直接 return）。
        """
        subject = self._principal_subject(principal)
        if not subject:
            return self._fail("principal_required")
        claimed = str(claimed or "").strip()
        if claimed and claimed != subject:
            return self._fail("principal_actor_mismatch",
                              {"claimed": claimed, "subject": subject})
        return subject

    def _principal_target_denial(self, session, principal, *, org_id: str,
                                 target_type: str, target_id: str) -> "dict | None":
        """受信 Principal 对目标的最小权限复核（R01-2）。

        管理员主体放行（部署运维身份）；其余主体要求：组织匹配，且为
        目标 owner。组织成员资格和被指派执行均不产生签发或撤回授权的权利。
        返回 None=放行；返回 dict=拒绝信封。
        """
        principal = principal or {}
        if principal.get("admin"):
            return None
        subject = str(principal.get("subject") or "").strip()
        org_id = str(org_id or "")
        if str(target_type or "") == "work_item":
            try:
                task = session.get(Task, int(target_id))
            except (TypeError, ValueError):
                task = None
            if task is None:
                return self._fail("task_not_found")
            task_org = str(task.org_id or "")
            if task_org != org_id:
                return self._fail("principal_org_mismatch",
                                  {"decision_org": org_id, "target_org": task_org})
            if principal.get("tenant"):
                denial = self._principal_task_denial(session, principal, task, write=True)
                if denial is not None:
                    return denial
            if subject and subject == str(task.owner or ""):
                return None
            return self._fail("principal_target_not_authorized",
                              {"target_type": str(target_type), "target_id": str(target_id)})
        # 未接入归属解析的目标不根据组织成员资格猜测审批权。
        return self._fail("principal_target_not_authorized",
                          {"target_type": str(target_type), "target_id": str(target_id)})

    def _principal_org_denial(self, principal, org_id) -> "dict | None":
        """组织级读/操作授权（N01）：管理员放行；其余主体要求 org ∈ 注册 org_ids。

        组织成员资格只授予该组织范围内的可见性，不自动产生管理/代发权；
        返回 None=放行；返回 dict=拒绝信封。
        """
        principal = principal or {}
        if principal.get("admin"):
            return None
        org = str(org_id or "")
        org_ids = {str(o) for o in (principal.get("org_ids") or []) if str(o).strip()}
        if org and org in org_ids:
            return None
        return self._fail("principal_org_not_authorized", {"org_id": org})

    def _principal_task_denial(self, session, principal, task,
                               *, write: bool = False) -> "dict | None":
        """任务目标授权（N01）：读=主体可见性；写=任务当事人（owner/负责人）grant。

        管理员放行；读操作放行 owner/负责人或目标组织注册成员；写操作只放行
        owner/负责人——同组织成员资格不自动产生取消/暂停/推进等操作权
        （动作级 grant 走批准决定通道，见 start_run 的 authorization.validate）。
        返回 None=放行；返回 dict=拒绝信封（不含任务正文，防跨组织内容泄露）。
        """
        principal = principal or {}
        if principal.get("admin"):
            return None
        subject = self._principal_subject(principal)
        if principal.get("tenant") and (task.tenant != principal["tenant"]
                                        or (task.org_id and task.org_id not in principal.get("org_ids", ()))):
            return self._fail("principal_target_not_authorized")
        if subject and subject in {str(task.owner or ""), str(task.assignee_id or "")}:
            return None
        if not write:
            org_ids = {str(o) for o in (principal.get("org_ids") or []) if str(o).strip()}
            task_org = str(task.org_id or "")
            if task_org and task_org in org_ids:
                return None
        return self._fail("principal_target_not_authorized",
                          {"target_type": "work_item", "target_id": str(task.id)})

    def _spill(self, key: str, rows: list) -> tuple[list, dict | None, bool]:
        """长输出落产物回引用：返回（内联预览行, 产物引用, 是否截断）。"""
        full = json.dumps(rows, ensure_ascii=False)
        if len(full) <= MAX_INLINE_CHARS or self._artifacts_dir is None:
            return rows, None, False
        digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self._artifacts_dir / f"{key}-{digest[:16]}.json"
        path.write_text(full, encoding="utf-8")
        ref = {"kind": key, "path": str(path.resolve()), "sha256": digest,
               "bytes": path.stat().st_size, "preview": full[:PREVIEW_CHARS]}
        inline = rows[:_preview_rows(full)]
        if not inline and rows:
            # 单条即超限：内联只留截断预览，全文走产物引用
            inline = [_trim(rows[0])]
        return inline, ref, True

    # ── 工作项 ──

    @staticmethod
    def _parse_acceptance(raw) -> list:
        if raw is None:
            return []
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                raise ValueError("invalid_acceptance_json") from None
        else:
            parsed = raw
        return parsed if isinstance(parsed, list) else []

    def create_work_item(self, *, tenant, owner="", title="", action="", chat_id="",
                         goal="", acceptance=None, assignee_id="", assignee_name="",
                         workflow_kind="legacy_todo", org_id="", space_id="",
                         due_text="", source_refs=None, idempotency_key="",
                         principal=None) -> dict:
        if workflow_kind not in {"legacy_todo", "delegated_delivery"}:
            return self._fail("invalid_workflow_kind")
        if principal is not None:
            # R01：hosted 模式建单 owner/org 只来自受信主体（管理员可代 declare）
            subject = self._principal_subject(principal)
            claimed_owner = str(owner or "").strip()
            if principal.get("admin"):
                owner = claimed_owner or subject
            else:
                if claimed_owner and claimed_owner != subject:
                    return self._fail("principal_actor_mismatch",
                                      {"claimed": claimed_owner, "subject": subject})
                owner = subject
                allowed_orgs = {str(o) for o in (principal.get("org_ids") or [])
                                if str(o).strip()}
                if str(org_id or "") and str(org_id) not in allowed_orgs:
                    return self._fail("principal_org_not_authorized",
                                      {"org_id": str(org_id)})
        if not tenant or not owner or not str(title).strip():
            return self._fail("title_tenant_owner_required")
        acceptance = self._parse_acceptance(acceptance)
        source_refs = [str(r) for r in (source_refs or []) if str(r).strip()]
        key = idempotency_key or stable_id("api", tenant, owner, org_id,
                                           str(title).strip(), str(action).strip(), due_text)
        try:
            with self._session() as session:
                if principal and principal.get("tenant"):
                    from evoharness.organization.models import BusinessSpace, Membership
                    from evoharness.organization.service import _live

                    space = session.get(BusinessSpace, space_id) if space_id else None
                    if (tenant != principal["tenant"] or not space or not space.enabled
                            or space.org_id != org_id):
                        return self._fail("resource_not_in_organization")
                    if assignee_id:
                        member = session.scalar(select(Membership).where(
                            Membership.org_id == org_id, Membership.tenant_id == tenant,
                            Membership.app_id == tenant, Membership.principal_id == assignee_id))
                        if not member or not _live(member, utcnow()):
                            return self._fail("assignee_not_available")
                existing = session.scalar(select(Task).where(
                    Task.tenant == tenant, Task.task_key == key))
                if existing is not None:
                    # 幂等键由调用者提供；命中本身不能授予另一主体/空间的读写权。
                    if (existing.owner != owner or str(existing.org_id or "") != str(org_id or "")
                            or str(existing.space_id or "") != str(space_id or "")):
                        return self._fail("idempotency_scope_conflict")
                    item = self._item_brief(existing)
                    item["created"] = False
                    return self._ok(item, item["source_refs"])
                contract_complete = bool(goal.strip()) and bool(acceptance)
                needs_confirmation = (not contract_complete) if workflow_kind == "delegated_delivery" \
                    else (not assignee_id)
                task = Task(
                    task_key=key, tenant=tenant, owner=owner, chat_id=chat_id,
                    thread_id="", title=str(title).strip(), action=str(action).strip(),
                    assignee_id=assignee_id, assignee_name=assignee_name,
                    due_text=due_text,
                    status="needs_confirmation" if needs_confirmation else "open",
                    needs_confirmation=needs_confirmation,
                    workflow_kind=workflow_kind, org_id=org_id, space_id=space_id,
                    revision=1, goal=goal.strip(),
                    acceptance_json=json.dumps(acceptance, ensure_ascii=False),
                    source_message_ids=json.dumps(source_refs, ensure_ascii=False),
                )
                session.add(task)
                session.flush()
                session.add(TaskEvent(task_id=task.id, event_type="created", actor=owner,
                                      detail="via unified api",
                                      evidence_message_ids=json.dumps(source_refs, ensure_ascii=False)))
                session.flush()
                brief = self._item_brief(task)
                brief["created"] = True
                brief["next_action"] = self._next_action_for_code(
                    self.display(task)["reason_code"])
                return self._ok(brief, source_refs)
        except ValueError as exc:
            return self._convert(exc)

    @staticmethod
    def _item_brief(task: Task) -> dict:
        return {
            "id": task.id,
            "task_key": task.task_key,
            "revision": task.revision,
            "state": task.status,
            "workflow_kind": task.workflow_kind,
            "org_id": task.org_id,
            "space_id": task.space_id,
            "owner": task.owner,
            "assignee": task.assignee_id or task.assignee_name,
            "chat_id": task.chat_id,
            "title": task.title,
            "goal": task.goal,
            "next_action": "",
            "source_refs": task.source_ids(),
        }

    # ── 展示态：状态机事实 → 桶 + 稳定 reason_code（不做二次业务判定） ──

    @staticmethod
    def display(task: Task) -> dict:
        """唯一展示态映射：三条入口共用，等待类绝不显示为已交付。"""
        status = task.status
        if status == "needs_confirmation":
            code, bucket = "awaiting_clarification", "awaiting_me"
        elif status in {"open", "ready"}:
            code, bucket = "ready_to_execute", "pending"
        elif status == "queued":
            code, bucket = "run_queued", "in_progress"
        elif status == "running":
            code, bucket = "executing", "in_progress"
        elif status == "verifying":
            code, bucket = "verifying", "in_progress"
        elif status == "rework":
            code, bucket = "rework", "in_progress"
        elif status == "paused":
            code, bucket = "paused", "in_progress"
        elif status == "failed":
            code, bucket = "failed_needs_decision", "awaiting_me"
        elif status == "waiting_authorization":
            code, bucket = "waiting_authorization", "awaiting_me"
        elif status == "waiting_budget":
            code, bucket = "waiting_budget", "in_progress"
        elif status == "waiting_dependency":
            code, bucket = "waiting_dependency", "in_progress"
        elif status == "waiting_resource":
            code, bucket = "waiting_resource", "in_progress"
        elif status == "waiting_external":
            code, bucket = "waiting_external", "in_progress"
        elif status == "waiting_acceptance":
            code, bucket = "awaiting_acceptance", "awaiting_me"
        elif status == "in_review":
            code, bucket = "awaiting_acceptance_review", "awaiting_me"
        elif status == "done":
            code, bucket = "delivered", "delivered"
        else:
            code, bucket = "cancelled", "cancelled"
        return {"bucket": bucket, "reason_code": code, "label": REASON_LABELS.get(code, code)}

    @staticmethod
    def _next_action_for_code(code: str) -> str:
        return {
            "awaiting_clarification": "answer_clarification",
            "ready_to_execute": "start_run",
            "run_queued": "claim_run",
            "waiting_authorization": "submit_approval",
            "awaiting_acceptance": "review_acceptance",
            "awaiting_acceptance_review": "review_acceptance",
            "failed_needs_decision": "restart_or_cancel",
            "receipt_unknown": "reconcile_receipt",
        }.get(code, "")

    def _receipts(self, session, task_id: int) -> list[dict]:
        rows = list(session.scalars(select(Outbox).where(
            Outbox.target_id == str(task_id)).order_by(Outbox.id.desc())))
        return [{"intent_key": r.intent_key, "kind": r.kind, "status": r.status,
                 "receipt_message_id": r.receipt_message_id or "",
                 "last_error": (r.last_error or "")[:200]} for r in rows[:10]]

    def _has_unknown_receipt(self, session, task_id: int) -> bool:
        return session.scalar(select(func.count(Outbox.id)).where(
            Outbox.target_id == str(task_id),
            Outbox.status == OutboxStatus.DELIVERY_UNKNOWN)) > 0

    def _latest_run_is_dryrun(self, session, task_id: int) -> bool:
        run = session.scalar(select(ExecutionRun).where(
            ExecutionRun.task_id == task_id).order_by(ExecutionRun.created_at.desc(),
                                                      ExecutionRun.id.desc()).limit(1))
        if run is None:
            return False
        try:
            context = json.loads(run.context_json)
        except ValueError:
            return False
        return context.get("mode") == "dryrun"

    def _display(self, session, task: Task) -> dict:
        view = self.display(task)
        # 展示修正叠加：回执未知 > dryrun > 状态机。等待/未知类绝不落到 delivered。
        if self._has_unknown_receipt(session, task.id):
            view = {"bucket": "awaiting_me", "reason_code": "receipt_unknown",
                    "label": REASON_LABELS["receipt_unknown"]}
        elif task.status not in TERMINAL_TASK_STATES and self._latest_run_is_dryrun(session, task.id):
            view = {"bucket": "in_progress", "reason_code": "dryrun_not_live",
                    "label": REASON_LABELS["dryrun_not_live"]}
        view["next_action"] = self._next_action_for_code(view["reason_code"])
        return view

    def _view_row(self, session, task: Task) -> dict:
        row = self._item_brief(task)
        row["display"] = self._display(session, task)
        row["waiting_reason"] = task.waiting_reason
        row["due_text"] = task.due_text
        return row

    def work_view(self, *, tenant, owner, org_id="", principal=None) -> dict:
        """工作视图四桶 + 已取消；个人（org_id=""）与组织空间分域。"""
        if principal is not None:
            # N01：hosted 模式视图 owner 只取受信主体（正文 owner 仅作声明被校验）；
            # 组织视图范围必须落在主体注册的组织内，跨组织/他人视图一律拒绝。
            subject = self._principal_subject(principal)
            if principal.get("admin"):
                owner = str(owner or "").strip() or subject
            else:
                claimed = str(owner or "").strip()
                if claimed and claimed != subject:
                    return self._fail("principal_actor_mismatch",
                                      {"claimed": claimed, "subject": subject})
                owner = subject
                if str(org_id or ""):
                    denial = self._principal_org_denial(principal, org_id)
                    if denial is not None:
                        return denial
        if not tenant or not owner:
            return self._fail("tenant_owner_required")
        try:
            with self._session() as session:
                query = select(Task).where(Task.tenant == tenant, Task.org_id == org_id)
                if not (principal and principal.get("tenant") and org_id):
                    query = query.where(Task.owner == owner)
                rows = [self._view_row(session, t)
                        for t in session.scalars(query.order_by(Task.id))]
                buckets = {key: [] for key in ("pending", "in_progress", "awaiting_me",
                                               "delivered", "cancelled")}
                for row in rows:
                    buckets[row["display"]["bucket"]].append(row)
                inline, ref, truncated = self._spill("work-view", rows)
                data = {"scope": "org" if org_id else "personal", "org_id": org_id,
                        "buckets": buckets,
                        "counts": {k: len(v) for k, v in buckets.items()},
                        "total": len(rows)}
                if ref is not None:
                    data["artifact"] = ref
                    data["truncated"] = truncated
                refs = sorted({r for row in rows for r in row["source_refs"]})
                return self._ok(data, refs)
        except ValueError as exc:
            return self._convert(exc)

    def get_work_item(self, task_id, principal=None) -> dict:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        try:
            with self._session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    return self._fail("task_not_found")
                if principal is not None:
                    # N01：按目标归属组织/当事人校验主体可见性；拒绝信封不携带任务正文
                    denial = self._principal_task_denial(session, principal, task)
                    if denial is not None:
                        return denial
                row = self._view_row(session, task)
                row["acceptance"] = self._parse_acceptance(task.acceptance_json)
                row["events_preview"] = [
                    {"event": e.event_type, "actor": e.actor, "detail": (e.detail or "")[:200],
                     "at": e.created_at.isoformat() if e.created_at else ""}
                    for e in session.scalars(select(TaskEvent).where(
                        TaskEvent.task_id == task.id).order_by(TaskEvent.id.desc()).limit(8))]
                row["runs"] = self._run_briefs(session, task.id)
                row["artifacts"] = self._artifact_refs(session, task.id)
                row["receipts"] = self._receipts(session, task.id)
                row["allowed_transitions"] = sorted(can_transitions(task))
                from evoharness.knowledge.task_learning import learning_view
                row["learning"] = learning_view(session,task.id)
                refs = list(row["source_refs"]) + [a["path"] for a in row["artifacts"]] \
                    + [r["run_id"] for r in row["runs"]]
                return self._ok(row, refs)
        except ValueError as exc:
            return self._convert(exc)

    # ── 状态推进 / 澄清 ──

    def transition_work_item(self, task_id, *, to_state, actor="", reason="",
                             wake_condition="", expected_revision=None,
                             evidence_message_ids=None, principal=None) -> dict:
        try:
            task_id = int(task_id)
            expected = int(expected_revision) if expected_revision is not None else None
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        if principal is not None:
            # N01：操作者只取受信主体，正文 actor 仅作声明被校验（缺省自动补主体）
            bound = self._bind_principal_actor(principal, actor)
            if isinstance(bound, dict):
                return bound
            actor = bound
        elif not str(actor or "").strip():
            return self._fail("actor_required")
        try:
            with self._session() as session:
                if principal is not None:
                    task = session.get(Task, task_id)
                    if task is None:
                        return self._fail("task_not_found")
                    denial = self._principal_task_denial(session, principal, task,
                                                         write=True)
                    if denial is not None:
                        return denial
                task = work_service.transition_to(
                    session, task_id, to_state=to_state, actor=actor, reason=reason,
                    wake_condition=wake_condition, expected_revision=expected,
                    evidence_message_ids=evidence_message_ids)
                return self._ok({"id": task.id, "revision": task.revision,
                                 "state": task.status,
                                 "display": self._display(session, task)},
                                [str(task.id)])
        except ValueError as exc:
            return self._convert(exc)

    def pause_work_item(self, task_id, *, actor="", reason="", principal=None) -> dict:
        return self.transition_work_item(task_id, to_state="paused", actor=actor,
                                         reason=reason, principal=principal)

    def complete_work_item(self, task_id, *, actor="", evidence_message_ids=None,
                           principal=None) -> dict:
        """普通待办的既有完成行为（service.set_status 既有守卫）；
        委派交付不适用——done 的唯一合法入口仍是验收服务。"""
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        if principal is not None:
            bound = self._bind_principal_actor(principal, actor)
            if isinstance(bound, dict):
                return bound
            actor = bound
        elif not str(actor or "").strip():
            return self._fail("actor_required")
        try:
            with self._session() as session:
                if principal is not None:
                    task = session.get(Task, task_id)
                    if task is None:
                        return self._fail("task_not_found")
                    denial = self._principal_task_denial(session, principal, task,
                                                         write=True)
                    if denial is not None:
                        return denial
                task = work_service.set_status(session, task_id, status="done", actor=actor,
                                               evidence_message_ids=evidence_message_ids)
                if task is None:
                    return self._fail("task_not_found")
                return self._ok({"id": task.id, "state": task.status,
                                 "display": self._display(session, task)},
                                list(evidence_message_ids or []) or [str(task.id)])
        except ValueError as exc:
            return self._convert(exc)

    def cancel_work_item(self, task_id, *, actor="", reason="", principal=None) -> dict:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        if principal is not None:
            # N01：取消者只取受信主体——正文伪造 actor=bob 与主体不一致即拒
            bound = self._bind_principal_actor(principal, actor)
            if isinstance(bound, dict):
                return bound
            actor = bound
        elif not str(actor or "").strip():
            return self._fail("actor_required")
        try:
            with self._session() as session:
                if principal is not None:
                    task = session.get(Task, task_id)
                    if task is None:
                        return self._fail("task_not_found")
                    denial = self._principal_task_denial(session, principal, task,
                                                         write=True)
                    if denial is not None:
                        return denial
                task = work_service.transition_to(session, task_id, to_state="cancelled",
                                                  actor=actor, reason=reason)
                cancelled_runs, fenced = [], []
                for run in session.scalars(select(ExecutionRun).where(
                        ExecutionRun.task_id == task_id)):
                    if run.state in {"queued", "paused"}:
                        run.state = "cancelled"
                        cancelled_runs.append(run.id)
                    elif run.state in {"claimed", "running"}:
                        # worker 侧租约校验会因任务取消而拒绝继续（work_revision_changed）
                        fenced.append(run.id)
                if cancelled_runs or fenced:
                    supplements.enqueue(session, task_id,
                                        event_id=f"cancel:{task.id}:{task.revision}",
                                        event_kind="cancellation",
                                        payload={"actor": actor, "reason": reason[:200]})
                session.flush()
                return self._ok(
                    {"id": task.id, "state": task.status,
                     "cancelled_runs": cancelled_runs, "fenced_runs": fenced,
                     "note": "queued sends need approval revoke to cancel intents"},
                    cancelled_runs or [str(task.id)])
        except ValueError as exc:
            return self._convert(exc)

    def request_clarification(self, task_id, *, question, actor="", blocking=True,
                              principal=None) -> dict:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        if principal is not None:
            bound = self._bind_principal_actor(principal, actor)
            if isinstance(bound, dict):
                return bound
            actor = bound
        elif not str(actor or "").strip():
            return self._fail("actor_required")
        try:
            with self._session() as session:
                if principal is not None:
                    task = session.get(Task, task_id)
                    if task is None:
                        return self._fail("task_not_found")
                    denial = self._principal_task_denial(session, principal, task,
                                                         write=True)
                    if denial is not None:
                        return denial
                task = work_service.request_clarification(session, task_id, actor=actor,
                                                          question=question, blocking=blocking)
                return self._ok({"id": task.id, "state": task.status,
                                 "waiting_reason": task.waiting_reason,
                                 "display": self._display(session, task)}, [str(task.id)])
        except ValueError as exc:
            return self._convert(exc)

    def resolve_clarification(self, task_id, *, actor="", answer_message_id,
                              principal=None) -> dict:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        if principal is not None:
            bound = self._bind_principal_actor(principal, actor)
            if isinstance(bound, dict):
                return bound
            actor = bound
        elif not str(actor or "").strip():
            return self._fail("actor_required")
        try:
            with self._session() as session:
                if principal is not None:
                    task = session.get(Task, task_id)
                    if task is None:
                        return self._fail("task_not_found")
                    denial = self._principal_task_denial(session, principal, task,
                                                         write=True)
                    if denial is not None:
                        return denial
                task = work_service.resolve_clarification(session, task_id, actor=actor,
                                                          answer_message_id=answer_message_id)
                return self._ok({"id": task.id, "state": task.status,
                                 "display": self._display(session, task)},
                                [answer_message_id])
        except ValueError as exc:
            return self._convert(exc)

    def submit_supplement(self, task_id, *, event_id, note, actor="", principal=None) -> dict:
        """追加说明：source event 去重、绑定当前 revision、如实回报在途交付状态。"""
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        if not str(event_id or "").strip():
            return self._fail("supplement_event_id_required")
        if principal is not None:
            bound = self._bind_principal_actor(principal, actor)
            if isinstance(bound, dict):
                return bound
            actor = bound
        elif not str(actor or "").strip():
            return self._fail("actor_required")
        try:
            with self._session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    return self._fail("task_not_found")
                if principal is not None:
                    denial = self._principal_task_denial(session, principal, task,
                                                         write=True)
                    if denial is not None:
                        return denial
                event = work_service.append_note(session, task, message_id=event_id,
                                                 note=str(note or ""), actor=actor)
                dedup = event.event_type == "note_ignored_duplicate"
                active_run = session.scalar(select(ExecutionRun).where(
                    ExecutionRun.task_id == task_id,
                    ExecutionRun.state.in_(["queued", "claimed", "running",
                                            "waiting_acceptance"])).limit(1))
                if active_run is not None and not dedup:
                    supplements.enqueue(session, task_id, event_id=event_id, event_kind="note",
                                        payload={"note": str(note or "")[:500], "actor": actor})
                    delivery = "queued_for_inflight_run"
                else:
                    delivery = "duplicate_ignored" if dedup else "recorded_no_active_run"
                return self._ok(
                    {"task_id": task.id, "dedup": dedup, "revision": task.revision,
                     "state": task.status, "delivery": delivery,
                     "display": self._display(session, task)},
                    [event_id])
        except (ValueError, SupplementError) as exc:
            return self._convert(exc)

    # ── Run：启动 / 暂停 / 授权载荷 ──

    def _authorize_adapter(self, matched: "dict | None" = None):
        """默认授权适配器：context.authorization 绑定
        主体(grantee)×组织×目标×版本×载荷×动作（R02）。

        matched 可选：把本次校验命中的 decision_id 交回调用方落证据。
        """
        if self._authorize is not None:
            return self._authorize

        def validate(session, context, action):
            auth = context.get("authorization")
            if not isinstance(auth, dict):
                raise AuthorizationError("no_active_decision")
            decision = authorization.validate(
                session,
                target_type=auth.get("target_type", "work_item"),
                target_id=str(auth.get("target_id", "")),
                target_revision=int(auth.get("target_revision", 0)),
                payload_hash=str(auth.get("payload_hash", "")),
                action=action,
                grantee_id=str(auth.get("grantee_id", "")),
                org_id=str(auth.get("org_id", "")),
            )
            if matched is not None:
                matched["decision_id"] = decision.decision_id
            return True

        return validate

    def run_request_payload(self, task_id, brief=None, principal=None) -> dict:
        """批准前的稳定载荷：payload_hash 绑定 brief，正文变化旧批准即失效。"""
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        try:
            with self._session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    return self._fail("task_not_found")
                if principal is not None:
                    denial = self._principal_task_denial(session, principal, task)
                    if denial is not None:
                        return denial
                payload = dict(brief) if isinstance(brief, dict) else {
                    "task_id": str(task.id), "revision": task.revision,
                    "goal": task.goal,
                    "acceptance": self._parse_acceptance(task.acceptance_json),
                }
                return self._ok({
                    "target_type": "work_item",
                    "target_id": str(task.id),
                    "target_revision": task.revision,
                    "payload": payload,
                    "payload_hash": sha256_json(payload),
                }, [str(task.id)])
        except ValueError as exc:
            return self._convert(exc)

    def start_run(self, task_id, *, actor_id="", brief=None, mode="live",
                  output_root=None, idempotency_key=None, principal=None) -> dict:
        if mode not in {"live", "dryrun"}:
            return self._fail("invalid_run_mode")
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        root = Path(output_root) if output_root else (
            (self._artifacts_dir or Path.cwd()) / "runs" / f"task-{task_id}")
        if principal is not None and not root.resolve().is_relative_to(
                (self._artifacts_dir or Path.cwd()).resolve()):
            return self._fail("output_root_outside_artifacts")
        try:
            with self._session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    return self._fail("task_not_found")
                if task.status not in RUN_STARTABLE_STATES:
                    return self._fail("invalid_task_transition", {"state": task.status})
                if principal is not None:
                    # N01：Run 目标先按组织/当事人校验可见性（动作 grant 由
                    # authorization.validate 的主体×组织×目标×版本×载荷×动作绑定把关）
                    denial = self._principal_task_denial(session, principal, task)
                    if denial is not None:
                        return denial
                    # R02：Run 操作者只取受信主体；正文 actor_id 只作声明被校验
                    bound = self._bind_principal_actor(principal, actor_id)
                    if isinstance(bound, dict):
                        return bound
                    actor_id = bound
                if not str(actor_id or "").strip():
                    return self._fail("run_actor_required")
                payload = dict(brief) if isinstance(brief, dict) else {
                    "task_id": str(task.id), "revision": task.revision,
                    "goal": task.goal,
                    "acceptance": self._parse_acceptance(task.acceptance_json),
                }
                payload_hash = sha256_json(payload)
                key = idempotency_key or stable_id("run", task.tenant, task_id,
                                                   task.revision, payload_hash, mode)
                matched: dict = {}
                context = {"task_id": str(task_id), "revision": task.revision,
                           "mode": mode, "brief": payload,
                           "authorization": {"target_type": "work_item",
                                             "target_id": str(task_id),
                                             "target_revision": task.revision,
                                             "payload_hash": payload_hash,
                                             # R02：grantee=实际操作者、org=任务组织，
                                             # 与批准决定做主体×组织×目标×版本×载荷×动作绑定
                                             "grantee_id": str(actor_id or ""),
                                             "org_id": str(task.org_id or "")}}
                run = execution.request_run(
                    session, task_id=task_id, context=context, actor_id=actor_id,
                    output_root=str(root), idempotency_key=key,
                    validate_context=self._authorize_adapter(matched))
                if run.state == "paused":
                    # 同一意图重新启动 = 恢复排队（幂等键复用，不新建 Run）
                    run.state = "queued"
                    session.flush()
                elif run.state in {"cancelled", "failed"}:
                    return self._fail("run_intent_conflict", {"state": run.state,
                                                              "run_id": run.id})
                # Preserve the successful start contract after authorization;
                # denied requests never create caller-selected directories.
                Path(run.output_root).mkdir(parents=True, exist_ok=True)
                if task.status != "queued":
                    work_service.transition_to(session, task_id, to_state="queued",
                                               actor=actor_id)
                    session.refresh(task)
                decision_id = str(matched.get("decision_id") or "")
                return self._ok(
                    {"operation_id": run.id, "generation": run.generation,
                     "state": run.state, "task_state": task.status, "mode": mode,
                     "idempotency_key": key, "output_root": run.output_root,
                     "operator_id": actor_id, "decision_id": decision_id},
                    [run.id, run.context_hash] + ([decision_id] if decision_id else []))
        except ValueError as exc:
            return self._convert(exc)

    def _run_briefs(self, session, task_id: int) -> list[dict]:
        accepted = set()
        for evidence in session.scalars(select(TaskEvent.evidence_message_ids).where(
                TaskEvent.task_id == task_id, TaskEvent.event_type == "acceptance_passed")):
            try:
                references = json.loads(evidence or "[]")
            except ValueError:
                continue
            if isinstance(references, list):
                accepted.update(reference for reference in references if isinstance(reference, str))
        rows = []
        for run in session.scalars(select(ExecutionRun).where(
                ExecutionRun.task_id == task_id).order_by(ExecutionRun.created_at.desc())):
            try:
                mode = json.loads(run.context_json).get("mode", "live")
            except ValueError:
                mode = "?"
            rows.append({"run_id": run.id, "state": run.state,
                         "accepted": f"run:{run.id}" in accepted,
                         "generation": run.generation, "attempts": run.attempts,
                         "mode": mode, "error": (run.error or "")[:200]})
        return rows

    def pause_run(self, run_id, principal=None) -> dict:
        try:
            with self._session() as session:
                run = session.get(ExecutionRun, str(run_id))
                if run is None:
                    return self._fail("run_not_found")
                if principal is not None:
                    # N01：暂停 Run 属于任务写操作——任务当事人或该 Run 的操作者可暂停
                    task = session.get(Task, run.task_id)
                    if task is None:
                        return self._fail("task_not_found")
                    principal = principal or {}
                    subject = self._principal_subject(principal)
                    if not principal.get("admin") and subject \
                            and subject not in {str(task.owner or ""),
                                                str(task.assignee_id or ""),
                                                str(run.actor_id or "")}:
                        return self._fail("principal_target_not_authorized",
                                          {"target_type": "execution_run",
                                           "target_id": str(run.id)})
                if run.state == "paused":
                    return self._ok({"run_id": run.id, "state": run.state}, [run.id])
                if run.state == "queued":
                    run.state = "paused"
                    task = session.get(Task, run.task_id)
                    if task is not None and task.status == "queued":
                        # 排队暂停 = 退回待处理（状态机 queued→ready），不伪装进行中
                        work_service.transition_to(session, task.id, to_state="ready",
                                                   actor="api:pause_run",
                                                   reason="run paused before claim")
                    session.flush()
                    return self._ok({"run_id": run.id, "state": run.state}, [run.id])
                if run.state in {"claimed", "running"}:
                    return self._fail("run_lease_active",
                                      {"lease_owner": run.lease_owner,
                                       "lease_until": run.lease_until.isoformat()
                                       if run.lease_until else ""})
                return self._fail("invalid_run_transition", {"state": run.state})
        except ValueError as exc:
            return self._convert(exc)

    # ── 验收：done 的唯一合法入口在验收服务，API 只翻译参数 ──

    def request_acceptance(self, task_id, *, run_id, requester="", principal=None) -> dict:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        if principal is not None:
            # N01：requester 只取受信主体，正文声明不一致即拒（缺省自动补主体）
            bound = self._bind_principal_actor(principal, requester)
            if isinstance(bound, dict):
                return bound
            requester = bound
        elif not str(requester or "").strip():
            return self._fail("actor_required")
        try:
            with self._session() as session:
                if principal is not None:
                    task = session.get(Task, task_id)
                    if task is None:
                        return self._fail("task_not_found")
                    denial = self._principal_task_denial(session, principal, task,
                                                         write=True)
                    if denial is not None:
                        return denial
                task = acceptance.request_acceptance(session, task_id, run_id=str(run_id),
                                                     requester=requester)
                return self._ok({"task_id": task.id, "run_id": str(run_id),
                                 "state": task.status,
                                 "display": self._display(session, task)},
                                [str(run_id)])
        except (ValueError, AcceptanceError) as exc:
            return self._convert(exc)

    def review_acceptance(self, task_id, *, run_id, verdict, reviewer="", notes="",
                          principal=None) -> dict:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        if principal is not None:
            # N01：reviewer 只取受信主体——伪造 reviewer 与主体不一致即拒；
            # owner/负责人资格仍由验收服务既有守卫复核（缺省自动补主体）
            bound = self._bind_principal_actor(principal, reviewer)
            if isinstance(bound, dict):
                return bound
            reviewer = bound
        elif not str(reviewer or "").strip():
            return self._fail("actor_required")
        try:
            with self._session() as session:
                if principal is not None:
                    task = session.get(Task, task_id)
                    if task is None:
                        return self._fail("task_not_found")
                    denial = self._principal_task_denial(session, principal, task,
                                                         write=True)
                    if denial is not None:
                        return denial
                task = acceptance.review(session, task_id, run_id=str(run_id),
                                         reviewer=reviewer, verdict=verdict, notes=notes,
                                         policy=self._acceptance_policy)
                return self._ok({"task_id": task.id, "verdict": verdict,
                                 "state": task.status,
                                 "display": self._display(session, task)},
                                [str(run_id), str(task.id)])
        except (ValueError, AcceptanceError) as exc:
            return self._convert(exc)

    def learning_action(self, task_id, *, evidence_id, action, actor="", principal=None,
                        _side_query=None) -> dict:
        from evoharness.knowledge import task_learning
        from evoharness.work.models import SkillLearningEvidence
        if action not in {"build", "adopt"}:
            return self._fail("learning_action_invalid")
        if principal is not None:
            bound = self._bind_principal_actor(principal,actor)
            if isinstance(bound,dict):
                return bound
            actor = bound
        if not str(actor or "").strip():
            return self._fail("actor_required")
        try:
            task_id = int(task_id)
            with self._session() as session:
                task = session.get(Task,task_id)
                row = session.get(SkillLearningEvidence,str(evidence_id))
                if task is None or row is None or row.task_id != task_id:
                    return self._fail("learning_evidence_not_found")
                if principal is not None:
                    denial = self._principal_task_denial(session,principal,task,write=True)
                    if denial is not None:
                        return denial
                    from evoharness.organization.models import Membership
                    member = session.scalar(select(Membership).where(
                        Membership.org_id == task.org_id,
                        Membership.principal_id == actor,
                        Membership.tenant_id == str(principal.get("tenant_id") or ""),
                        Membership.app_id == str(principal.get("app_id") or "")))
                    if member is None or member.tenant_id != task.tenant:
                        return self._fail("principal_namespace_mismatch")
                if actor not in {task.owner,task.assignee_id}:
                    return self._fail("learning_actor_not_authorized")
                if action == "adopt":
                    result = task_learning.adopt_candidate(session,str(evidence_id),actor=actor)
                    return self._ok({"evidence_id":str(evidence_id),"action":action,"result":result},
                                    [str(evidence_id)])
            import asyncio
            result = asyncio.run(task_learning.build_candidate(self._app.session_factory,
                str(evidence_id),actor=actor,side_query=_side_query))
            if not result.get("ok"):
                return self._fail(str(result.get("error") or "learning_candidate_failed"))
            return self._ok({"evidence_id":str(evidence_id),"action":action,"result":result},
                            [str(evidence_id)])
        except (ValueError,task_learning.LearningError) as exc:
            return self._convert(exc)
        except Exception as exc:
            return self._fail("learning_model_error",{"error_type":type(exc).__name__})

    def sync_knowledge(self, *, org_id, space_id, tenant="", actor="", principal=None) -> dict:
        from evoharness.knowledge.compiler import prepare_manifest,publish_manifest,scope_key
        from evoharness.knowledge.models import KnowledgePublication
        from evoharness.organization.models import Membership,BusinessSpace
        from evoharness.organization.service import _live
        from evoharness.clock import utcnow
        if principal is not None:
            bound=self._bind_principal_actor(principal,actor)
            if isinstance(bound,dict):
                return bound
            actor=bound
            tenant=str(principal.get("tenant_id") or "")
            denial=self._principal_org_denial(principal,org_id)
            if denial is not None:
                return denial
        if not actor or not tenant:
            return self._fail("actor_required")
        try:
            with self._session() as session:
                members=list(session.scalars(select(Membership).where(Membership.org_id==org_id,
                    Membership.tenant_id==tenant,Membership.principal_id==actor)))
                space=session.get(BusinessSpace,space_id)
                if (len(members)!=1 or not _live(members[0],utcnow())
                        or "knowledge.read" not in json.loads(members[0].grants_json)
                        or space is None or not space.enabled or space.org_id!=org_id):
                    return self._fail("knowledge_not_authorized")
                if principal is not None and principal.get("app_id") and principal["app_id"]!=members[0].app_id:
                    return self._fail("principal_namespace_mismatch")
                manifest=prepare_manifest(session,org_id=org_id,space_id=space_id,tenant=tenant,owner=actor)
                publication=session.get(KnowledgePublication,scope_key(org_id,space_id,actor))
                result=publish_manifest(session,org_id=org_id,space_id=space_id,tenant=tenant,owner=actor,
                    manifest=manifest,expected_revision=publication.revision if publication else 0)
                return self._ok(result,[result["build_sha256"]])
        except ValueError as exc:
            return self._convert(exc)

    # ── 批准 / 撤回 ──

    def submit_approval(self, *, org_id, actor_id="", target_type="", target_id="",
                        target_revision=None, payload=None, actions=None, grantee_id="",
                        allow_bot_fallback=False, expires_at=None,
                        principal=None) -> dict:
        try:
            revision = int(target_revision)
            if revision < 1:
                raise ValueError("decision_revision_required")
        except (TypeError, ValueError):
            return self._fail("decision_revision_required")
        if not isinstance(payload, dict):
            return self._fail("decision_payload_required")
        action_set = {str(a).strip() for a in (actions or []) if str(a).strip()}
        payload_hash = sha256_json(payload)
        try:
            with self._session() as session:
                if principal is not None:
                    # R01：批准方 actor 只取受信主体；正文 actor_id 只作声明被校验
                    bound = self._bind_principal_actor(principal, actor_id)
                    if isinstance(bound, dict):
                        return bound
                    actor_id = bound
                    # R01-2：对目标做组织/owner/负责人/成员关系复核（管理员除外）
                    denial = self._principal_target_denial(
                        session, principal, org_id=str(org_id or ""),
                        target_type=str(target_type or ""), target_id=str(target_id))
                    if denial is not None:
                        return denial
                decision = authorization.approve(
                    session, actor_id, org_id=org_id, target_type=target_type,
                    target_id=str(target_id), target_revision=revision,
                    payload_hash=payload_hash, actions=action_set,
                    grantee_id=grantee_id, allow_bot_fallback=allow_bot_fallback,
                    expires_at=_parse_dt(expires_at))
                session.flush()
                return self._ok(
                    {"decision_id": decision.decision_id, "payload_hash": payload_hash,
                     "target_type": target_type, "target_id": str(target_id),
                     "target_revision": revision, "actions": sorted(action_set),
                     "actor_id": decision.actor_id,
                     "expires_at": decision.expires_at.isoformat() if decision.expires_at else ""},
                    [decision.decision_id, payload_hash])
        except ValueError as exc:
            return self._convert(exc)

    @staticmethod
    def _identity_of(actions: set[str]) -> str:
        if "send_as_user" in actions:
            return "user"
        if "send_as_bot" in actions:
            return "bot"
        if "work.execute" in actions:
            return "runner"
        return ",".join(sorted(actions))[:60]

    def _payload_diff(self, session, decision) -> dict:
        """正文差异：批准绑定的 payload hash vs 该目标最新 outbox 正文。"""
        rows = list(session.scalars(select(Outbox).where(
            Outbox.target_id == decision.target_id).order_by(Outbox.id.desc()).limit(20)))
        current = rows[0] if rows else None
        approved_row = next((r for r in rows if r.payload_hash == decision.payload_hash), None)

        def _body(row):
            if row is None:
                return ""
            try:
                return json.dumps(json.loads(row.payload_json), ensure_ascii=False)[:PAYLOAD_PREVIEW_CHARS]
            except ValueError:
                return (row.payload_json or "")[:PAYLOAD_PREVIEW_CHARS]

        changed = bool(current is not None and current.payload_hash != decision.payload_hash)
        return {
            "changed": changed,
            "approved_hash": decision.payload_hash,
            "current_hash": (current.payload_hash if current else ""),
            "approved_body": _body(approved_row) if changed else _body(current),
            "current_body": _body(current) if changed else "",
        }

    def _approval_view(self, session, decision) -> dict:
        actions = set(json.loads(decision.actions_json or "[]"))
        return {
            "decision_id": decision.decision_id,
            "org_id": decision.org_id,
            "actor_id": decision.actor_id,
            "grantee_id": decision.grantee_id,
            "target_type": decision.target_type,
            "target_id": decision.target_id,
            "target_revision": decision.target_revision,
            "actions": sorted(actions),
            "identity": self._identity_of(actions),
            "payload_hash": decision.payload_hash,
            "payload_diff": self._payload_diff(session, decision),
            "expires_at": decision.expires_at.isoformat() if decision.expires_at else "",
            "revoked": decision.revoked_at.isoformat() if decision.revoked_at else "",
            "superseded_by": decision.superseded_by,
            "created_at": decision.created_at.isoformat() if decision.created_at else "",
        }

    def get_approval(self, decision_id, principal=None) -> dict:
        try:
            with self._session() as session:
                decision = session.get(Decision, str(decision_id))
                if decision is None:
                    return self._fail("decision_not_found")
                if principal is not None:
                    # N01：批准详情只对管理员、批准方/被授权主体或同组织注册成员可见
                    principal = principal or {}
                    if not principal.get("admin"):
                        subject = self._principal_subject(principal)
                        org_ids = {str(o) for o in (principal.get("org_ids") or [])
                                   if str(o).strip()}
                        actor_grantee = {str(decision.actor_id or ""),
                                         str(decision.grantee_id or "")}
                        org_member = str(decision.org_id or "") \
                            and str(decision.org_id) in org_ids
                        if subject not in actor_grantee and not org_member:
                            return self._fail("principal_target_not_authorized",
                                              {"target_type": str(decision.target_type),
                                               "target_id": str(decision.decision_id)})
                return self._ok(self._approval_view(session, decision),
                                [decision.decision_id, decision.payload_hash])
        except ValueError as exc:
            return self._convert(exc)

    def approvals_view(self, org_id, principal=None) -> dict:
        if principal is not None:
            # N01：组织批准台账按注册组织成员资格校验（管理员放行）
            denial = self._principal_org_denial(principal, org_id)
            if denial is not None:
                return denial
        try:
            with self._session() as session:
                rows = list(session.scalars(select(Decision).where(
                    Decision.org_id == str(org_id), Decision.decision == "approve",
                    Decision.revoked_at.is_(None), Decision.superseded_by == ""
                ).order_by(Decision.created_at.desc())))
                views = [self._approval_view(session, d) for d in rows]
                return self._ok({"approvals": views}, [d.decision_id for d in rows])
        except ValueError as exc:
            return self._convert(exc)

    def revoke_approval(self, *, org_id="", actor_id="", target_type="", target_id="",
                        principal=None) -> dict:
        try:
            with self._session() as session:
                if principal is not None:
                    # R01：撤回方 actor 只取受信主体，并复核目标权限（管理员除外）
                    bound = self._bind_principal_actor(principal, actor_id)
                    if isinstance(bound, dict):
                        return bound
                    actor_id = bound
                    denial = self._principal_target_denial(
                        session, principal, org_id=str(org_id or ""),
                        target_type=str(target_type or ""), target_id=str(target_id))
                    if denial is not None:
                        return denial
                revoked_before = len(list(session.scalars(select(Decision).where(
                    Decision.org_id == str(org_id), Decision.target_type == target_type,
                    Decision.target_id == str(target_id), Decision.decision == "approve",
                    Decision.revoked_at.is_(None)))))
                cancelled = authorization.revoke(session, actor_id, target_type=target_type,
                                                 target_id=str(target_id), org_id=org_id)
                return self._ok({"cancelled_intents": cancelled,
                                 "count": len(cancelled),
                                 "revoked_decisions": revoked_before,
                                 "note": "sent rows keep their real status; unknown receipts stay unknown"},
                                cancelled)
        except ValueError as exc:
            return self._convert(exc)

    # ── 知识检索 ──

    def search_knowledge(self, *, space_id, subject, owner_scope="",
                         include_private=False, principal=None) -> dict:
        try:
            with self._session() as session:
                viewer_scopes = None
                if principal is not None:
                    if principal.get("tenant"):
                        from evoharness.organization.models import BusinessSpace

                        space = session.get(BusinessSpace, space_id) if space_id else None
                        if not space or not space.enabled or space.org_id not in principal["org_ids"]:
                            return self._fail("space_not_authorized")
                    # R01：受信主体模式下可见性只从 Principal 派生，
                    # 查询参数 owner_scope/include_private 不再作为可见性依据。
                    subject_scope = self._principal_subject(principal)
                    viewer_scopes = {subject_scope} if subject_scope else set()
                result = knowledge_service.search(
                    session, space_id=space_id, subject=subject,
                    owner_scope=owner_scope, include_private=include_private,
                    viewer_scopes=viewer_scopes)
                inline, ref, truncated = self._spill("knowledge", result["claims"])
                data = dict(result)
                data["claims"] = inline
                if ref is not None:
                    data["artifact"] = ref
                    data["truncated"] = truncated
                refs = sorted({src for claim in result["claims"] for src in claim["source_refs"]})
                return self._ok(data, refs)
        except (ValueError, KnowledgeError) as exc:
            return self._convert(exc)

    # ── 证据 / 成本 / 健康 ──

    def _artifact_refs(self, session, task_id: int) -> list[dict]:
        rows = []
        run_ids = [r.id for r in session.scalars(select(ExecutionRun).where(
            ExecutionRun.task_id == task_id))]
        for run_id in run_ids:
            for artifact in session.scalars(select(Artifact).where(
                    Artifact.run_id == run_id)):
                rows.append({"artifact_id": artifact.id, "kind": artifact.kind, "path": artifact.path,
                             "sha256": artifact.sha256,
                             "task_revision": artifact.task_revision,
                             "run_id": run_id})
        return rows

    def view_evidence(self, task_id, principal=None) -> dict:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return self._fail("task_not_found")
        try:
            with self._session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    return self._fail("task_not_found")
                if principal is not None:
                    # N01：证据视图按目标归属组织/当事人校验主体可见性（不泄露正文）
                    denial = self._principal_task_denial(session, principal, task)
                    if denial is not None:
                        return denial
                events = [{"event": e.event_type, "actor": e.actor,
                           "detail": (e.detail or "")[:300],
                           "evidence_message_ids": e.evidence_message_ids,
                           "at": e.created_at.isoformat() if e.created_at else ""}
                          for e in session.scalars(select(TaskEvent).where(
                              TaskEvent.task_id == task.id).order_by(TaskEvent.id))]
                inline, ref, truncated = self._spill("evidence-events", events)
                receipts = self._receipts(session, task.id)
                data = {
                    "task_id": task.id,
                    "revision": task.revision,
                    "artifacts": self._artifact_refs(session, task.id),
                    "runs": self._run_briefs(session, task.id),
                    "events": inline,
                    "receipts": receipts,
                    "source_refs": task.source_ids(),
                    "unknown": [r["intent_key"] for r in receipts
                                if r["status"] == OutboxStatus.DELIVERY_UNKNOWN],
                }
                if ref is not None:
                    data["artifact"] = ref
                    data["truncated"] = truncated
                refs = list(data["source_refs"]) + [a["path"] for a in data["artifacts"]]
                return self._ok(data, refs)
        except ValueError as exc:
            return self._convert(exc)

    def set_budget_limit(self, *, org_id, limit_tokens, space_id="", actor="", principal=None) -> dict:
        from evoharness.organization.models import BusinessSpace, Membership, Organization
        from evoharness.organization.service import _live

        if type(limit_tokens) is not int or limit_tokens < 0:
            return self._fail("invalid_budget_limit")
        if principal is not None:
            actor = self._bind_principal_actor(principal, actor)
            if isinstance(actor, dict):
                return actor
            denial = self._principal_org_denial(principal, org_id)
            if denial is not None:
                return denial
        if not actor:
            return self._fail("actor_required")
        try:
            with self._session() as session:
                organization = session.get(Organization, org_id)
                if organization is None or not organization.enabled:
                    return self._fail("organization_not_available")
                members = list(session.scalars(select(Membership).where(
                    Membership.org_id == org_id, Membership.principal_id == actor)))
                if principal is not None:
                    members = [m for m in members
                               if m.tenant_id == str(principal.get("tenant_id") or "")
                               and m.app_id == str(principal.get("app_id") or "")]
                if (len(members) != 1 or not _live(members[0], utcnow())
                        or "organization.manage" not in json.loads(members[0].grants_json)):
                    return self._fail("organization_management_not_granted")
                if space_id:
                    space = session.get(BusinessSpace, space_id)
                    if space is None or not space.enabled or space.org_id != org_id:
                        return self._fail("resource_not_in_organization")
                row = budget.set_limit(session, scope_kind="space" if space_id else "org",
                                       scope_id=space_id or org_id, limit_tokens=limit_tokens)
                return self._ok({"scope_kind": row.scope_kind, "scope_id": row.scope_id,
                                 "limit_tokens": row.limit_tokens, "revision": row.revision}, [])
        except ValueError as exc:
            return self._convert(exc)

    def view_cost(self, *, org_id="", space_id="", principal_id="", principal=None) -> dict:
        if principal is not None:
            # N01：成本口径按主体收敛——成员只能看自己的用量与所在组织的限额，
            # 正文 principal_id 声明与受信主体不一致即拒（管理员放行全量口径）
            principal = principal or {}
            subject = self._principal_subject(principal)
            if not principal.get("admin"):
                claimed = str(principal_id or "").strip()
                if claimed and claimed != subject:
                    return self._fail("principal_actor_mismatch",
                                      {"claimed": claimed, "subject": subject})
                if str(org_id or ""):
                    denial = self._principal_org_denial(principal, org_id)
                    if denial is not None:
                        return denial
                principal_id = subject
        try:
            with self._session() as session:
                summary = budget.usage_summary(session, org_id=org_id, space_id=space_id,
                                               principal_id=principal_id)
                limit_query = select(BudgetLimit)
                if principal is not None and not principal.get("admin"):
                    from sqlalchemy import or_, and_
                    from evoharness.organization.models import BusinessSpace
                    allowed_orgs = list(principal.get("org_ids") or [])
                    allowed_spaces = select(BusinessSpace.space_id).where(
                        BusinessSpace.org_id.in_(allowed_orgs))
                    limit_query = limit_query.where(or_(
                        and_(BudgetLimit.scope_kind == "org", BudgetLimit.scope_id.in_(allowed_orgs)),
                        and_(BudgetLimit.scope_kind == "space", BudgetLimit.scope_id.in_(allowed_spaces)),
                        and_(BudgetLimit.scope_kind == "principal", BudgetLimit.scope_id == subject)))
                limits = [{"scope_kind": l.scope_kind, "scope_id": l.scope_id,
                           "limit_tokens": l.limit_tokens, "revision": l.revision}
                          for l in session.scalars(limit_query)]
                data = dict(summary)
                data["limits"] = limits
                # 未知项如实列出：没有配置限额的空间不假装有额度口径
                data["unknown"] = {
                    "limits_configured": bool(limits),
                    "price_basis": "token reservations and settled usage; no price estimate",
                }
                data["at"] = utcnow().isoformat()
                return self._ok(data, [])
        except (ValueError, BudgetError) as exc:
            return self._convert(exc)

    def skills_view(self, *, org_id="", space_id="", tenant="", owner="",
                    legacy=False, principal=None) -> dict:
        from evoharness.knowledge.scoped_skills import skills_view
        try:
            with self._session() as session:
                return self._ok(skills_view(session, org_id=org_id, space_id=space_id,
                    tenant=tenant, owner=owner, legacy=legacy, principal=principal))
        except ValueError as exc:
            return self._convert(exc)

    def view_health(self, org_id="", principal=None) -> dict:
        from evoharness.knowledge.models import KnowledgeClaim
        try:
            with self._session() as session:
                # N01：组织成员的健康视图收敛到其注册组织（管理员/CLI 保持全量口径）
                member_org = ""
                if principal is not None:
                    denial = self._principal_org_denial(principal, org_id)
                    if denial is not None:
                        return denial
                    principal = principal or {}
                    if not principal.get("admin"):
                        member_org = str(org_id or "")
                org_task_ids: list[int] = []
                org_space_ids: list[str] = []
                if member_org:
                    org_task_ids = list(session.scalars(select(Task.id).where(
                        Task.org_id == member_org)))
                    from evoharness.organization.models import BusinessSpace
                    org_space_ids = [s.space_id for s in session.scalars(
                        select(BusinessSpace).where(
                            BusinessSpace.org_id == member_org))]
                run_query = select(ExecutionRun)
                if member_org:
                    run_query = run_query.where(ExecutionRun.task_id.in_(org_task_ids))
                run_counts = {}
                for run in session.scalars(run_query):
                    run_counts[run.state] = run_counts.get(run.state, 0) + 1
                recovering_query = run_query.where(ExecutionRun.state == "waiting_recovery")
                recovering = [{"run_id": r.id, "error": (r.error or "")[:160]}
                              for r in session.scalars(recovering_query)]
                receipt_counts = {}
                receipt_query = select(Outbox)
                if member_org:
                    receipt_query = receipt_query.where(Outbox.target_id.in_(
                        {str(i) for i in org_task_ids}))
                for row in session.scalars(receipt_query):
                    receipt_counts[row.status] = receipt_counts.get(row.status, 0) + 1
                waiting_query = select(Task).where(
                    Task.status.in_(sorted({"waiting_authorization", "waiting_budget",
                                            "waiting_dependency", "waiting_resource",
                                            "waiting_external"})))
                if member_org:
                    waiting_query = waiting_query.where(Task.org_id == member_org)
                waiting = {}
                for task in session.scalars(waiting_query):
                    waiting[task.status] = waiting.get(task.status, 0) + 1
                limit_query = select(BudgetLimit)
                if member_org:
                    limit_query = limit_query.where(
                        ((BudgetLimit.scope_kind == "org")
                         & (BudgetLimit.scope_id == member_org))
                        | ((BudgetLimit.scope_kind == "space")
                           & BudgetLimit.scope_id.in_(org_space_ids)))
                limits = list(session.scalars(limit_query))
                stale_query = select(func.count(KnowledgeClaim.claim_id)).where(
                    KnowledgeClaim.state == "stale")
                if member_org:
                    stale_query = stale_query.where(KnowledgeClaim.org_id == member_org)
                stale_claims = session.scalar(stale_query)
                data = {
                    "at": utcnow().isoformat(),
                    "database": {"reachable": True, "backend": session.get_bind().name},
                    "runs": {"by_state": run_counts, "awaiting_recovery": recovering},
                    "receipts": {"by_status": receipt_counts,
                                 "delivery_unknown": receipt_counts.get(
                                     OutboxStatus.DELIVERY_UNKNOWN, 0)},
                    "waiting": waiting,
                    "budget": {"limits_configured": bool(limits),
                               "limit_count": len(limits)},
                    "knowledge": {"stale_claims": int(stale_claims or 0)},
                    # 未知项如实列出：没有配置限额时不假装有预算口径
                    "unknown": ["budget_limits"] if not limits else [],
                }
                return self._ok(data, [r["run_id"] for r in recovering])
        except (ValueError, BudgetError) as exc:
            return self._convert(exc)

    def schema(self, principal=None) -> dict:  # noqa: ARG002 — schema 无主体数据，保持同门
        return self._ok(API_SCHEMA)


def open_api(database_url: str, *, artifacts_dir=None, authorize=None,
             acceptance_policy=None) -> WorkApi:
    """从数据库 URL 构造 WorkApi（server / CLI 共用）；不加载任何凭据。"""
    from evoharness.application import Application
    application = Application.open(database_url)
    return WorkApi(application, artifacts_dir=artifacts_dir, authorize=authorize,
                   acceptance_policy=acceptance_policy)
