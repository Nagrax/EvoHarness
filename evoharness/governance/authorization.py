"""主体×目标×版本×载荷×动作×期限 的统一决定验证与撤权联动。

每次工具调用、知识读取、启动执行和发送前都调用 validate 重验；
批准一次不能推导出其他动作、更广范围或永久有效。
"""
from __future__ import annotations
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.ids import sha256_json
from evoharness.models import Outbox, OutboxStatus
from evoharness.governance.models import Decision


class AuthorizationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _actions(row: Decision) -> set[str]:
    return set(json.loads(row.actions_json or "[]"))


def approve(session: Session, actor_id: str, *, org_id: str, target_type: str,
            target_id: str, target_revision: int, payload_hash: str,
            actions: set[str], grantee_id: str = "",
            allow_bot_fallback: bool = False,
            expires_at: datetime | None = None) -> Decision:
    """新批准替代同目标的旧批准；旧记录保留 superseded_by，不物理删除。"""
    if not actor_id or not actions:
        raise AuthorizationError("decision_actor_and_actions_required")
    for previous in session.scalars(select(Decision).where(
            Decision.org_id == org_id, Decision.target_type == target_type,
            Decision.target_id == target_id, Decision.grantee_id == grantee_id,
            Decision.decision == "approve", Decision.revoked_at.is_(None),
            Decision.superseded_by == "")):
        previous.superseded_by = "pending"
    decision = Decision(decision_id=uuid4().hex, org_id=org_id, actor_id=actor_id,
                        grantee_id=grantee_id, target_type=target_type, target_id=target_id,
                        target_revision=target_revision, payload_hash=payload_hash,
                        decision="approve",
                        actions_json=json.dumps(sorted(actions), ensure_ascii=False),
                        allow_bot_fallback=allow_bot_fallback, expires_at=expires_at)
    session.add(decision)
    session.flush()
    for previous in session.scalars(select(Decision).where(Decision.superseded_by == "pending")):
        previous.superseded_by = decision.decision_id
    session.flush()
    return decision


def revoke(session: Session, actor_id: str, *, target_type: str, target_id: str,
           org_id: str) -> list[str]:
    """撤回所有未撤决定并取消其未执行发送意图；已发送与未知回执保留真实状态。"""
    if not actor_id:
        raise AuthorizationError("decision_actor_required")
    now = utcnow()
    cancelled_intents: list[str] = []
    for row in session.scalars(select(Decision).where(
            Decision.org_id == org_id, Decision.target_type == target_type,
            Decision.target_id == target_id, Decision.decision == "approve",
            Decision.revoked_at.is_(None))):
        row.revoked_at = now
        for outbox_row in session.scalars(select(Outbox).where(
                Outbox.target_id == target_id,
                Outbox.status.in_([OutboxStatus.QUEUED, OutboxStatus.RETRY_WAIT]))):
            outbox_row.status = OutboxStatus.CANCELLED
            outbox_row.last_error = f"decision_revoked:{row.decision_id}"
            cancelled_intents.append(outbox_row.intent_key)
    session.flush()
    return cancelled_intents


def validate(session: Session, *, target_type: str, target_id: str,
             target_revision: int, payload_hash: str, action: str,
             grantee_id: str = "", org_id: str = "",
             now: datetime | None = None) -> Decision:
    """发送/工具/共享前的统一重验；版本、载荷、动作、期限、撤回任一不符即拒绝。

    grantee 绑定语义（R02）：批准绑定了 grantee 时，validate 必须以同一 grantee
    调用——省略不算通配符，不匹配即拒绝；完全未绑定 grantee 的批准保留原
    兼容行为（任何调用方/省略均可，由入口层另行做主体绑定）。
    org_id 非空时额外把候选决定限制在同一组织（主体×组织×目标×版本×载荷×动作）。
    """
    now = now or utcnow()
    candidates = [row for row in session.scalars(select(Decision).where(
        Decision.target_type == target_type, Decision.target_id == target_id,
        Decision.decision == "approve", Decision.revoked_at.is_(None),
        Decision.superseded_by == ""))]
    if not candidates:
        raise AuthorizationError("no_active_decision")
    if str(org_id or ""):
        candidates = [row for row in candidates if row.org_id == org_id]
        if not candidates:
            raise AuthorizationError("no_active_decision")
    # 同一目标可以分别授权多个受托人；无关记录不能遮蔽本人的有效授权。
    eligible = [row for row in candidates if row.grantee_id == grantee_id]
    if grantee_id:
        eligible += [row for row in candidates if not row.grantee_id]
    if not eligible:
        raise AuthorizationError("decision_grantee_mismatch" if grantee_id
                                 else "decision_grantee_required")
    first_error = None
    for row in eligible:
        problem = None
        if row.target_revision != target_revision:
            problem = "decision_target_revision_changed"
        elif row.payload_hash != payload_hash:
            problem = "decision_payload_changed"
        elif row.expires_at is not None and now >= row.expires_at:
            problem = "decision_expired"
        elif action not in _actions(row):
            problem = "action_not_in_decision"
        if problem:
            first_error = first_error or problem
            continue
        return row
    raise AuthorizationError(first_error or "no_active_decision")


def resolve_send_identity(session: Session, *, target_type: str, target_id: str,
                          target_revision: int, payload: dict,
                          requested_identity: str, user_identity_valid: bool,
                          grantee_id: str = "",
                          now: datetime | None = None) -> tuple[Decision, str]:
    """发送身份冻结：请求身份必须在批准动作内；user 失效不悄悄降级 bot。

    grantee_id：调用主体的绑定声明；批准绑定了 grantee 时必须一致（R02）。
    """
    decision = validate(session, target_type=target_type, target_id=target_id,
                        target_revision=target_revision,
                        payload_hash=sha256_json(payload),
                        action=f"send_as_{requested_identity}",
                        grantee_id=grantee_id, now=now)
    if requested_identity == "user" and not user_identity_valid:
        if not decision.allow_bot_fallback:
            raise AuthorizationError("waiting_authorization")
        return decision, "bot"
    return decision, requested_identity
