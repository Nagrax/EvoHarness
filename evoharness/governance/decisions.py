"""组织决策、审阅责任与统一接管（DE-41，指南 §18.3 / §5.2）。

不新增平行控制面：决定统一走 DE-12 governance.authorization（批准绑定
target_revision + payload_hash，撤回/替代/过期显式记录，复用 outbox 回执口径）；
独立验收走 DE-13 work.acceptance；组织/岗位生命周期走 DE-38 organization.service；
Run 租约与恢复走 DE-11 work.execution。

- 决定绑定：岗位委派 / 目标验收 / 审阅责任 / 费用归属统一绑定具体版本与
  真实操作者；授权/目标/正文任一变化旧批准即失效（revision 条件）。
- actor 只来自可信会话绑定（AuthenticatedPrincipal）：HTTP 正文 actor 字段
  或群聊指令文本不能增加权限。
- 审阅分工按影响配置：普通可验证工作按已获授权自动推进（确定性验证优先，
  无副作用步骤不重复确认）；高影响工作执行与审阅分离，同一执行 Run 的
  自评分不冒充独立验收，执行者不能用自造审阅身份批准自己越界。
- 暂停/接管：组织/单元/空间/岗位暂停与预算熔断共用治理状态（organizations/
  positions/budget_limits）与同一 Run 状态机；暂停围栏传播到在途 Run
  （取消传播 + 在途回执对账）与工具/发送层；恢复先核授权与目标版本。
- 权限传播：tool_gate 供 agents.tools.check_permission 在 bypass/档位检查
  之前调用；check_send_allowed 供发送层 _precheck 在发送前重验；
  resolve_send_identity 把发送身份冻结（authorization.resolve_send_identity）
  绑定到 outbox 意图：token 失效不悄悄降级 bot，fallback 只按显式批准放行。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.governance import authorization, budget
from evoharness.governance.authorization import AuthorizationError
from evoharness.governance.models import Decision
from evoharness.ids import sha256_json
from evoharness.models import OutboxStatus, Task
from evoharness.organization import service as organization
from evoharness.organization.models import (Assignment, Membership, Organization,
                                            Position, TaskCrewMember, TenureReceipt)
from evoharness.organization.service import AuthenticatedPrincipal, OrganizationError
from evoharness.work import supplements
from evoharness.work.models import Artifact, ExecutionRun

REVIEW_ACTION = "work.review"
IMPACT_LEVELS = frozenset({"routine", "high"})
DECISION_KINDS = frozenset({"position_delegation", "objective_acceptance", "cost_attribution"})
PAUSE_SCOPES = frozenset({"organization", "space", "unit", "position"})
# 有副作用的工具动作在组织模式下的授权映射；未映射工具（读类/无副作用步骤）
# 不重复确认，沿用既有权限语义。
TOOL_ACTION_MAP = {
    "write_file": "work.execute",
    "edit_file": "work.execute",
    "run_shell": "work.execute",
}


class GovernanceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time(value: datetime | None = None) -> datetime:
    value = value or utcnow()
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _actions(raw: str) -> set[str]:
    try:
        return set(json.loads(raw or "[]"))
    except ValueError:
        return set()


def _live(row, now: datetime) -> bool:
    return (row.status == "active" and _time(row.valid_from) <= now
            and (row.valid_until is None or now < _time(row.valid_until)))


def _assignment_of(run: ExecutionRun) -> str:
    try:
        return json.loads(run.context_json or "{}").get("assignment_id", "")
    except ValueError:
        return ""


# ── actor 只来自可信会话绑定（S1）────────────────────────────────────────────


def trusted_actor(principal) -> AuthenticatedPrincipal:
    """决定/接管的操作者只接受身份适配器产出的可信主体；不接受字符串声明。"""
    if not isinstance(principal, AuthenticatedPrincipal) or not principal.authenticated \
            or not principal.principal_id:
        raise GovernanceError("trusted_principal_required")
    return principal


def bind_actor(principal, claimed=None) -> str:
    """HTTP 正文 actor 字段 / 群聊指令文本不能增加权限。

    操作者一律取会话绑定主体；正文声明与可信主体不一致时显式拒绝；
    声明一致也不放大（决定的主体与权限只来自 AuthenticatedPrincipal 与
    其任职授权交集）。
    """
    trusted = trusted_actor(principal).principal_id
    claimed = str(claimed or "").strip()
    if claimed and claimed != trusted:
        raise GovernanceError("actor_claim_mismatch")
    return trusted


# ── 组织成员与授权交集的本地复核（镜像 organization.resolve_context 口径）────


def _member_of(session: Session, principal: AuthenticatedPrincipal, org_id: str) -> Membership | None:
    member = session.scalar(select(Membership).where(
        Membership.org_id == org_id, Membership.principal_id == principal.principal_id,
        Membership.tenant_id == principal.tenant_id, Membership.app_id == principal.app_id))
    if member is None or not _live(member, _time(None)):
        return None
    return member


def _is_manager(session: Session, principal: AuthenticatedPrincipal, org_id: str) -> bool:
    member = _member_of(session, principal, org_id)
    return member is not None and "organization.manage" in _actions(member.grants_json)


def _require_manager(session: Session, principal: AuthenticatedPrincipal, org_id: str) -> None:
    if not _is_manager(session, principal, org_id):
        raise GovernanceError("delegation_not_authorized")


def _require_manager_or_owner(session: Session, principal: AuthenticatedPrincipal, org_id: str,
                              target_type: str, target_id: str) -> None:
    if _is_manager(session, principal, org_id):
        return
    if target_type == "work_item":
        task = session.get(Task, int(target_id))
        if task is not None and task.org_id == org_id and task.owner == principal.principal_id:
            return
    raise GovernanceError("acceptance_decision_not_authorized")


def _assignment_intersection(session: Session, member: Membership, assignment: Assignment) -> set[str]:
    position = session.get(Position, assignment.position_id)
    if position is None or position.status != "active":
        return set()
    return (_actions(member.grants_json) & _actions(position.action_ceiling_json)
            & _actions(assignment.actions_json))


# ── 决定绑定：岗位委派 / 目标验收 / 审阅责任 / 费用归属 ─────────────────────


def decision_payload(kind: str, payload: dict) -> dict:
    """决定的规范载荷：同一 kind + 载荷 → 同一 payload_hash（批准方与服务方共用）。"""
    if not isinstance(payload, dict):
        raise GovernanceError("decision_payload_required")
    return {"kind": kind, **payload}


def bind_decision(session: Session, *, org_id: str, principal, kind: str, target_type: str,
                  target_id: str, target_revision: int, payload: dict, actions: set[str],
                  grantee_id: str = "", claimed_actor=None,
                  expires_at: datetime | None = None) -> Decision:
    """岗位委派 / 目标验收 / 费用归属统一绑定具体版本与真实操作者（DE-12 语义）。

    - actor 只来自会话绑定主体（claimed_actor 仅用于一致性校验，不能提权）；
    - 委派与费用归属要求组织管理者；目标验收要求管理者或任务 owner；
    - 批准绑定 target_revision + payload_hash：授权/目标/正文任一变化即失效。
    """
    actor_id = bind_actor(principal, claimed_actor)
    if kind not in DECISION_KINDS:
        raise GovernanceError("invalid_decision_kind")
    if kind in {"position_delegation", "cost_attribution"}:
        _require_manager(session, principal, org_id)
    else:
        _require_manager_or_owner(session, principal, org_id, target_type, target_id)
    return authorization.approve(
        session, actor_id, org_id=org_id, target_type=target_type,
        target_id=str(target_id), target_revision=int(target_revision),
        payload_hash=sha256_json(decision_payload(kind, payload)),
        actions=set(actions), grantee_id=grantee_id, expires_at=expires_at)


def verify_decision(session: Session, *, kind: str, target_type: str, target_id: str,
                    target_revision: int, payload: dict, action: str,
                    grantee_id: str = "", now: datetime | None = None) -> Decision:
    """发送/工具/推进前的统一重验：版本、载荷、动作、期限、撤回任一不符即拒绝。"""
    return authorization.validate(
        session, target_type=target_type, target_id=str(target_id),
        target_revision=int(target_revision),
        payload_hash=sha256_json(decision_payload(kind, payload)),
        action=action, grantee_id=grantee_id, now=now)


# ── 审阅分工：按影响配置，执行与审阅分离 ─────────────────────────────────────


def review_policy(impact: str) -> dict:
    """审阅分工配置：普通可验证工作按已获授权自动推进；高影响独立审阅。"""
    if impact not in IMPACT_LEVELS:
        raise GovernanceError("invalid_impact_level")
    return {"impact": impact,
            "mode": "auto" if impact == "routine" else "independent_review",
            "requires_independent_review": impact == "high"}


def authorize_step(session: Session, principal, snapshot: dict, *, action: str,
                   impact: str = "routine", now: datetime | None = None) -> dict:
    """单步推进：按既有授权上下文即时重验后放行，不要求逐个无副作用步骤重新确认。

    暂停/撤权经组织授权上下文失效即时拦截；高影响步骤只标记需要独立验收，
    不凭这一步的模型自述改变任何状态。
    """
    policy = review_policy(impact)
    organization.validate_snapshot(session, snapshot, principal, action, now=now)
    return {"impact": impact, "mode": policy["mode"],
            "decision_required": policy["requires_independent_review"]}


def record_review_decision(session: Session, *, org_id: str, reviewer, task: Task,
                           run: ExecutionRun, verdict: str, notes: str = "",
                           claimed_reviewer=None) -> Decision:
    """高影响工作的审阅责任绑定（配合 DE-13 独立验收，不自评冒充独立验收）。

    - 审阅者必须是会话绑定的可信主体且持覆盖任务空间的有效任职、
      审阅动作在其授权交集内（角色名/正文自造身份都不算审阅者）；
    - 审阅者与执行 Run 的 actor 必须是不同主体；
    - 绑定 task revision 与验收载荷：目标/正文变化后旧审阅批准失效。
    """
    reviewer_id = bind_actor(reviewer, claimed_reviewer)
    if verdict not in {"pass", "fail"}:
        raise GovernanceError("invalid_verdict")
    if reviewer_id == str(run.actor_id or ""):
        raise GovernanceError("reviewer_must_differ_from_executor")
    member = _member_of(session, reviewer, org_id)
    assignment = None
    if member is not None:
        for candidate in session.scalars(select(Assignment).where(
                Assignment.org_id == org_id,
                Assignment.membership_id == member.membership_id)):
            if not _live(candidate, _time(None)):
                continue
            if task.space_id and task.space_id not in _actions(candidate.space_ids_json):
                continue
            if REVIEW_ACTION in _assignment_intersection(session, member, candidate):
                assignment = candidate
                break
    if assignment is None:
        raise GovernanceError("reviewer_not_authorized")
    payload = {"verdict": verdict, "run_id": run.id,
               "notes": (notes or "").strip()[:200],
               "artifact_hashes": sorted(artifact.sha256 for artifact in session.scalars(
                   select(Artifact).where(Artifact.run_id == run.id)))}
    # 审阅责任不经 bind_decision 的委派/费用通道：审阅者资格已在上方按
    # 会话绑定主体 + 有效任职 + 授权交集复核；这里只落 DE-12 决定绑定。
    return authorization.approve(
        session, reviewer_id, org_id=org_id, target_type="work_item",
        target_id=str(task.id), target_revision=int(task.revision),
        payload_hash=sha256_json(decision_payload("review_responsibility", payload)),
        actions={REVIEW_ACTION}, grantee_id=assignment.assignment_id)


# ── 暂停 / 恢复：三级暂停共用治理与 Run 状态 ────────────────────────────────


def _fence_runs(session: Session, *, org_id: str, runs, event_ref_prefix: str) -> tuple[list[str], list[str]]:
    """在途 Run 围栏：取消传播（补充队列）+ 副作用状态 Run 开回执对账单。

    已发送与未知回执保留真实状态，不写成功。"""
    fenced: list[str] = []
    receipt_ids: list[str] = []
    for run in runs:
        supplements.enqueue(session, run.task_id,
                            event_id=f"{event_ref_prefix}:{run.id}",
                            event_kind="cancellation",
                            payload={"reason": event_ref_prefix, "run_id": run.id})
        if run.state in organization.RUN_EFFECT_STATES:
            receipt = TenureReceipt(
                receipt_id=f"rct-{event_ref_prefix}-{run.id}"[:64],
                org_id=org_id, kind="run_reconcile", target_id=run.id,
                assignment_id=_assignment_of(run), task_id=run.task_id,
                detail_json=_json({"state": run.state, "generation": run.generation,
                                   "lease_owner": run.lease_owner}))
            session.add(receipt)
            receipt_ids.append(receipt.receipt_id)
        fenced.append(run.id)
    return fenced, receipt_ids


def _pause_fencing_effects(session: Session, event) -> dict:
    """组织/空间/单元暂停的 Run 级围栏（lifecycle_effects 回调）。

    波及面：组织=全部任务；空间=该空间任务；单元=该单元岗位负责的任务。
    岗位暂停走 organization.default_offboard_effects（按任职指派波及）。"""
    kind = event.action.split(".", 1)[0]
    query = select(Task.id).where(Task.org_id == event.org_id)
    if kind == "space":
        query = query.where(Task.space_id == event.target_id)
    elif kind == "unit":
        unit_positions = select(Position.position_id).where(
            Position.org_id == event.org_id, Position.unit_id == event.target_id)
        query = query.where(Task.assigned_position.in_(unit_positions))
    task_ids = set(session.scalars(query))
    runs = session.scalars(select(ExecutionRun).where(
        ExecutionRun.task_id.in_(task_ids),
        ExecutionRun.state.in_(organization.RUN_OPEN_STATES))) if task_ids else []
    fenced, receipt_ids = _fence_runs(session, org_id=event.org_id, runs=runs,
                                      event_ref_prefix=f"org-pause:{event.event_id}")
    detail = json.loads(event.detail_json or "{}")
    event.detail_json = _json({**detail, "fenced_run_ids": fenced, "receipt_ids": receipt_ids})
    return {"fenced_run_ids": fenced, "receipt_ids": receipt_ids}


def pause_scope(session: Session, principal, *, org_id: str, kind: str, target_id: str,
                reason: str, expected_revision: int | None = None) -> dict:
    """组织/空间/单元/岗位暂停：共用治理状态并把围栏传播到 Run 与发送层。

    - organization/space/unit → set_enabled(False)；position → transition(paused)；
    - 暂停即撤销未生效委派（DE-38 既有语义）+ 在途 Run 取消传播与回执对账；
    - 不删除历史，不把在途未知回执写成功；发送层经 check_send_allowed 即时拦截。
    """
    bind_actor(principal)
    if kind not in PAUSE_SCOPES:
        raise GovernanceError("invalid_pause_scope")
    if not str(reason or "").strip():
        raise GovernanceError("pause_reason_required")
    if kind == "position":
        row, event = organization.transition(
            session, principal, org_id=org_id, kind="position", target_id=target_id,
            status="paused", expected_revision=expected_revision,
            lifecycle_effects=organization.default_offboard_effects)
    else:
        row, event = organization.set_enabled(
            session, principal, org_id=org_id, kind=kind, target_id=target_id,
            enabled=False, expected_revision=expected_revision,
            lifecycle_effects=_pause_fencing_effects)
    detail = json.loads(event.detail_json or "{}")
    return {"scope": kind, "target_id": target_id, "reason": str(reason).strip()[:200],
            "revision": row.revision, "event_id": event.event_id,
            "fenced_run_ids": detail.get("fenced_run_ids", []),
            "receipt_ids": detail.get("receipt_ids", [])}


def resume_scope(session: Session, principal, *, org_id: str, kind: str, target_id: str,
                 expected_revision: int | None = None) -> dict:
    """恢复：先核授权（管理者身份在 set_enabled/transition 内复核）与目标版本。

    已围栏 Run 不自动复活：恢复返回仍未对账的回执单；任务 revision 相对
    Run 记录已漂移的 Run 显式列为 stale（必须按新版本重新起 Run）。"""
    bind_actor(principal)
    if kind not in PAUSE_SCOPES:
        raise GovernanceError("invalid_pause_scope")
    if kind == "position":
        row, _ = organization.transition(
            session, principal, org_id=org_id, kind="position", target_id=target_id,
            status="active", expected_revision=expected_revision)
    else:
        row, _ = organization.set_enabled(
            session, principal, org_id=org_id, kind=kind, target_id=target_id,
            enabled=True, expected_revision=expected_revision)
    open_receipts = organization.open_receipts(session, org_id=org_id)
    stale: set[str] = set()
    for receipt in open_receipts:
        run = session.get(ExecutionRun, receipt.target_id)
        task = session.get(Task, run.task_id) if run is not None else None
        if run is not None and task is not None and task.revision != run.task_revision:
            stale.add(run.id)
    return {"scope": kind, "target_id": target_id, "revision": row.revision,
            "open_receipt_ids": [receipt.receipt_id for receipt in open_receipts],
            "stale_run_ids": sorted(stale)}


# ── 统一接管：有权用户从根入口接管在途 Run ───────────────────────────────────


def _has_takeover_authority(session: Session, principal: AuthenticatedPrincipal,
                            org_id: str, task: Task) -> bool:
    if _is_manager(session, principal, org_id):
        return True
    member = _member_of(session, principal, org_id)
    if member is None:
        return False
    if task.owner == principal.principal_id:
        return True
    own_assignments = {row.assignment_id for row in session.scalars(select(Assignment).where(
        Assignment.org_id == org_id, Assignment.membership_id == member.membership_id))}
    for crew in session.scalars(select(TaskCrewMember).where(
            TaskCrewMember.task_id == task.id, TaskCrewMember.role == "accountable",
            TaskCrewMember.status == "active")):
        if crew.assignment_id in own_assignments:
            return True
    return False


def takeover_run(session: Session, principal, *, org_id: str, run_id: str, reason: str) -> dict:
    """有权用户（管理者 / 任务 owner / accountable 任职）接管在途 Run。

    - 围栏：waiting_recovery + 清租约 + generation+1（DE-11 恢复语义）；
    - 取消传播到任务补充队列 + 开在途回执对账单（未知回执不写成功）；
    - 操作者必须来自可信会话绑定；正文声明身份不能接管。
    """
    actor_id = bind_actor(principal)
    run = session.get(ExecutionRun, str(run_id))
    if run is None:
        raise GovernanceError("run_not_found")
    task = session.get(Task, run.task_id)
    if task is None or task.org_id != org_id:
        raise GovernanceError("run_not_in_organization")
    if not _has_takeover_authority(session, principal, org_id, task):
        raise GovernanceError("takeover_not_authorized")
    reason = str(reason or "").strip()
    if not reason:
        raise GovernanceError("takeover_reason_required")
    if run.state in {"done", "failed", "cancelled"}:
        raise GovernanceError("run_already_closed")
    if run.state == "waiting_recovery":
        receipt = session.scalar(select(TenureReceipt).where(
            TenureReceipt.target_id == run.id, TenureReceipt.status == "open").limit(1))
        return {"run_id": run.id, "state": run.state, "generation": run.generation,
                "receipt_id": receipt.receipt_id if receipt is not None else "",
                "takeover_by": actor_id}
    run.state = "waiting_recovery"
    run.generation += 1
    run.lease_owner = ""
    run.lease_until = None
    run.error = f"takeover:{actor_id}:{reason[:80]}"
    supplements.enqueue(session, task.id,
                        event_id=f"org-takeover:{run.id}:{run.generation}",
                        event_kind="cancellation",
                        payload={"reason": "takeover", "actor": actor_id})
    receipt = TenureReceipt(
        receipt_id=f"rct-takeover-{run.id}-{run.generation}"[:64],
        org_id=org_id, kind="run_reconcile", target_id=run.id,
        assignment_id=_assignment_of(run), task_id=task.id,
        detail_json=_json({"state": run.state, "generation": run.generation,
                           "takeover_by": actor_id, "reason": reason[:200]}))
    session.add(receipt)
    session.flush()
    return {"run_id": run.id, "state": run.state, "generation": run.generation,
            "receipt_id": receipt.receipt_id, "takeover_by": actor_id}


# ── 预算熔断：与暂停共用治理状态与 Run 状态机 ───────────────────────────────


def trip_budget_breaker(session: Session, principal, *, org_id: str, scope_kind: str,
                        scope_id: str, reason: str, limit_tokens: int = 0) -> dict:
    """预算熔断：budget_limits 置零（revision+1）；后续预留 budget_exhausted，
    执行器经同一 Run 状态机进入 waiting_budget；恢复（提高限额）不自动复活。"""
    bind_actor(principal)
    if not _is_manager(session, principal, org_id):
        raise GovernanceError("budget_breaker_not_authorized")
    if scope_kind not in {"org", "space", "principal"}:
        raise GovernanceError("invalid_budget_scope")
    if scope_kind == "org" and scope_id != org_id:
        raise GovernanceError("budget_breaker_scope_mismatch")
    if not str(reason or "").strip():
        raise GovernanceError("breaker_reason_required")
    limit = budget.set_limit(session, scope_kind=scope_kind, scope_id=scope_id,
                             limit_tokens=int(limit_tokens))
    return {"scope_kind": scope_kind, "scope_id": scope_id,
            "limit_tokens": limit.limit_tokens, "revision": limit.revision,
            "reason": str(reason).strip()[:200]}


# ── 权限传播：工具层 / 发送层 ────────────────────────────────────────────────


def tool_gate(session_factory, principal, snapshot: dict, *, action_map=None):
    """组织模式工具层授权闸门：供 agents.tools.check_permission 在 bypass/
    acceptEdits 档位检查之前调用——组织授权交集不能被内层 harness 档位放宽。

    撤权/暂停经 validate_snapshot 即时失效（重验组织上下文）；未映射工具
    （读类/无副作用步骤）返回 None 沿用既有权限语义，不重复确认。
    返回 None=放行；返回字符串=拒绝原因。
    """
    mapping = TOOL_ACTION_MAP if action_map is None else action_map

    def gate(tool_name: str, inp: dict):
        action = mapping.get(tool_name)
        if action is None:
            return None
        session = session_factory()
        try:
            organization.validate_snapshot(session, snapshot, principal, action)
        except (OrganizationError, AuthorizationError) as exc:
            return f"organization governance denied {tool_name}: {exc}"
        finally:
            session.close()
        return None

    return gate


def check_send_allowed(session: Session, row, *, now: datetime | None = None) -> tuple[bool, str]:
    """发送前治理重验（outbox._precheck 接线点）：撤权与暂停及时传到发送层。

    - 目标有批准绑定时：最新决定被撤回/被替代 → 拦截；其所在组织已暂停 → 拦截；
    - 无治理绑定的目标沿用既有核心检查口径（零行为变化）；
    - 只对在途可发送状态（queued/retry_wait/sending/waiting_authorization）判定；
      delivery_unknown、sent、failed 等真实回执状态不受本检查改写。
    waiting_authorization 的意图在 token 恢复/重新批准后按 next_at 重试；
    等待期间决定被撤回 → 下次领取时经本检查取消，不会发送。
    """
    if row.status not in {OutboxStatus.QUEUED, OutboxStatus.RETRY_WAIT, OutboxStatus.SENDING,
                          OutboxStatus.WAITING_AUTHORIZATION}:
        return True, ""
    newest = session.scalar(select(Decision).where(
        Decision.target_id == str(row.target_id), Decision.decision == "approve")
        .order_by(Decision.created_at.desc(), Decision.decision_id.desc()).limit(1))
    if newest is None:
        return True, ""
    if newest.revoked_at is not None:
        return False, "decision_revoked"
    if newest.superseded_by:
        return False, "decision_superseded"
    org_row = session.get(Organization, newest.org_id)
    if org_row is not None and not org_row.enabled:
        return False, "organization_paused"
    return True, ""


# ── 发送身份冻结（R09）：user 失效不悄悄改用 bot ─────────────────────────────

SEND_IDENTITY_SEND = "send"
SEND_IDENTITY_WAITING = "waiting_authorization"
SEND_IDENTITY_CANCEL = "cancel"


def resolve_send_identity(session: Session, row, *, payload: dict,
                          user_token_valid: bool,
                          now: datetime | None = None) -> tuple[str, str, str]:
    """把统一身份冻结（authorization.resolve_send_identity）绑定到 outbox 发送意图。

    发送前一刻调用：批准动作（send_as_user vs send_as_bot）、被授权人、
    target revision、payload hash、批准期限、fallback 许可任一不匹配即不发送。

    返回 (disposition, identity, reason)：
    - send：按 identity ∈ {"user","bot"} 发送；
    - waiting_authorization：授权/身份缺口（token 失效、载荷/版本变化、过期、
      动作或被授权人不符），意图转等待态；token 恢复或重新批准后 worker
      按 next_at 重试，不丢意图；reason 记录具体缺失；
    - cancel：决定已撤回/被替代或组织已暂停，取消意图。
    无治理绑定的目标沿用既有口径（token 可用即本人身份，否则机器人兜底），零行为变化。
    """
    if row.status not in {OutboxStatus.QUEUED, OutboxStatus.RETRY_WAIT,
                          OutboxStatus.SENDING, OutboxStatus.WAITING_AUTHORIZATION}:
        return SEND_IDENTITY_SEND, "bot", ""
    data = payload or {}
    newest = session.scalar(select(Decision).where(
        Decision.target_id == str(row.target_id), Decision.decision == "approve")
        .order_by(Decision.created_at.desc(), Decision.decision_id.desc()).limit(1))
    if newest is None:
        # 无治理绑定：沿用既有口径，不改变既有发送行为
        return SEND_IDENTITY_SEND, ("user" if user_token_valid else "bot"), ""
    if newest.revoked_at is not None:
        return SEND_IDENTITY_CANCEL, "", "decision_revoked"
    if newest.superseded_by:
        return SEND_IDENTITY_CANCEL, "", "decision_superseded"
    org_row = session.get(Organization, newest.org_id)
    if org_row is not None and not org_row.enabled:
        return SEND_IDENTITY_CANCEL, "", "organization_paused"
    twin = str(data.get("twin_open_id") or "").strip()
    requested_identity = "user" if twin else "bot"
    try:
        # grantee_id=twin：发送主体声明=分身本人；批准绑定了 grantee 时
        # validate 要求同一主体（省略≠通配，不匹配即拒，R02 语义）。
        _, identity = authorization.resolve_send_identity(
            session, target_type=str(row.target_type), target_id=str(row.target_id),
            target_revision=int(data.get("target_revision")
                                or newest.target_revision or 1),
            payload=data, requested_identity=requested_identity,
            user_identity_valid=bool(user_token_valid),
            grantee_id=twin, now=now)
    except AuthorizationError as exc:
        code = str(exc)
        # token 失效/未授权是等待的主因：记录具体缺失而不是笼统的授权缺口
        reason = "token_unavailable" if code == "waiting_authorization" else code
        return SEND_IDENTITY_WAITING, "", reason
    return SEND_IDENTITY_SEND, identity, ""
