"""岗位能力证据与组织知识学习闭环（DE-42）。

指南 §17.2 CapabilityEvidence / §18.4 组织学习与岗位能力升级的产品核心，
全部落在原服务上（不建第二控制面）：

1. 能力证据：Position 的 capability_refs 只是声明；只有绑定 Skill 制品 hash、
   评测协议指纹、工具环境指纹与适用范围（业务空间集合）并完成显式核验
   （核条件 + 回归证据）的证据才可被调度器视为合格接单资格。没有证据的能力
   显式标 pending_verification（不猜测、不因声明自动授权）。
2. 跨范围迁移：给已验证证据扩新范围必须重新核条件并回归——扩展请求生成
   pending 状态的派生证据行（derived_from 引用原证据），原范围的 verified
   不自动授权新范围（指南 §18.4：不因上一岗位 healthy 就取得新范围认证）。
3. 调度接缝：qualified 判定引用证据状态（EVOHARNESS_CAPABILITY_EVIDENCE_GATE，
   默认 off 保持 DE-40 现行为；开启后证据缺失/待验证/范围不符 → 不合格）。
4. 组织知识岗位视图：经原 knowledge.service（DE-16 scope 检索与来源契约）
   给岗位可读视图；跨单元复用取来源授权与目标岗位单元绑定空间的交集
   （不扩大受众）；个人来源（owner_scope=个人）与组织来源分别继承——
   岗位更换只带走合法组织经验，个人私聊/凭据不进全组织资产（接 DE-38）。
5. 学习闭环归因：在线反馈/增量复盘/搜索增强都回原版本评测晋级链
   （DE-18 制品绑定 champion 门 / DE-30 评测协议），本模块只新增岗位与
   目标归因维度（evidence 关联 position/objective 引用），不提供绕过
   set_tier/apply_eval_promotions 的第二晋级链。组织政策与评分器对优化
   路径只读：改写尝试被显式拒绝并写 permission-denied 审计事件。
6. 投影单写者：已采用的独立 SOP（executable_sop 证据绑定制品）对重建式
   投影更新只读（接 DE-18 producer_kind 与 DE-29 manifest 冻结语义），
   投影只能由源服务（experience.skill_sync）重建。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from evoharness.models import Base
from evoharness.clock import utcnow
from evoharness.knowledge import service as knowledge
from evoharness.knowledge.models import KnowledgeClaim
from .models import BusinessSpace, OrganizationEvent, Position, UnitSpaceBinding
from . import service as org_service


class CapabilityError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# 证据状态：pending（待验证，显式状态不是猜测）/ verified（已核条件+回归）/
# revoked（撤回，保留行与历史）
EVIDENCE_PENDING = "pending"
EVIDENCE_VERIFIED = "verified"
EVIDENCE_REVOKED = "revoked"

# 证据种类：skill = 原生 Skill 能力；executable_sop = 已采用的独立执行 SOP；
# tool = 工具环境能力
EVIDENCE_KIND_SKILL = "skill"
EVIDENCE_KIND_SOP = "executable_sop"
EVIDENCE_KIND_TOOL = "tool"

# 调度证据门（DE-42）：默认 off = DE-40 现行为（已建组织不回溯）；开启后
# qualified 判定引用 CapabilityEvidence。每次调用读环境变量（与
# skill_tiers.experimental_injection_enabled 同款约定，可按环境灰度）。
GATE_ENV = "EVOHARNESS_CAPABILITY_EVIDENCE_GATE"


def evidence_gate_enabled() -> bool:
    raw = str(os.environ.get(GATE_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(raw: str, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except ValueError:
        return fallback


def _scope(raw: str) -> list[str]:
    return [str(item) for item in _loads(raw, [])]


class CapabilityEvidence(Base):
    """岗位能力 → Skill/工具/评测协议/适用范围/真实制品的引用（指南 §17.2）。

    state=pending 是"没有证据的能力显式待验证"的落地：证据行登记后必须由
    组织管理者核条件并附回归证据才翻 verified；优化路径对证据行无写权。
    """
    __tablename__ = "position_capability_evidence"
    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    position_id: Mapped[str] = mapped_column(String(64), index=True)
    capability: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(24), default=EVIDENCE_KIND_SKILL)
    skill_name: Mapped[str] = mapped_column(String(255), default="")
    # 绑定：Skill 制品内容 hash / 评测协议指纹 / 工具环境指纹（缺一不可核验）
    artifact_hash: Mapped[str] = mapped_column(String(128), default="")
    eval_protocol: Mapped[str] = mapped_column(String(255), default="")
    tool_env: Mapped[str] = mapped_column(String(255), default="")
    # 适用范围（业务空间集合）：跨范围必须重新核验，不因原范围 verified 自动授权
    scope_space_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    state: Mapped[str] = mapped_column(String(16), default=EVIDENCE_PENDING, index=True)
    verified_by: Mapped[str] = mapped_column(String(128), default="")
    verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # 核条件+回归的真实证据引用（评测报告/回归运行/tier 审计行等）
    regression_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    # 学习闭环归因：能力证据关联岗位与目标引用（不新建第二晋级链，只加归因维度）
    source_objective_id: Mapped[str] = mapped_column(String(64), default="")
    source_task_id: Mapped[int] = mapped_column(default=0)
    # 跨范围派生：引用被扩展的原证据（原证据保持原范围 verified，不受影响）
    derived_from: Mapped[str] = mapped_column(String(64), default="")
    revision: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    def scope(self) -> list[str]:
        return _scope(self.scope_space_ids_json)

    def regression_refs(self) -> list[str]:
        return _scope(self.regression_refs_json)

    def state_view(self) -> dict:
        return {"evidence_id": self.evidence_id, "position_id": self.position_id,
                "capability": self.capability, "kind": self.kind,
                "skill_name": self.skill_name, "artifact_hash": self.artifact_hash,
                "eval_protocol": self.eval_protocol, "tool_env": self.tool_env,
                "scope_space_ids": self.scope(), "state": self.state,
                "verified_by": self.verified_by,
                "verified_at": self.verified_at.isoformat() if self.verified_at else "",
                "regression_refs": self.regression_refs(),
                "source_objective_id": self.source_objective_id,
                "source_task_id": self.source_task_id,
                "derived_from": self.derived_from, "revision": self.revision}


def _evidence(session, org_id: str, evidence_id: str) -> CapabilityEvidence:
    row = session.get(CapabilityEvidence, evidence_id)
    if row is None or row.org_id != org_id:
        raise CapabilityError("evidence_not_found")
    return row


def _declared(session, org_id: str, position_id: str, capability: str) -> Position:
    row = org_service._owned(session, Position, position_id, org_id)
    if capability not in _loads(row.capability_refs_json, []):
        # 岗位能力引用是声明清单：证据只能登记在已声明的能力上，不猜测扩展
        raise CapabilityError("capability_not_declared")
    return row


def _check_space(session, org_id: str, space_id: str) -> None:
    org_service._owned(session, BusinessSpace, space_id, org_id)


def _event(session, org_id: str, actor: str, action: str, target: str, detail: dict) -> OrganizationEvent:
    event = OrganizationEvent(org_id=org_id, actor_membership_id=actor, action=action,
                              target_id=target, detail_json=_json(detail))
    session.add(event)
    session.flush()
    return event


# ── 证据登记 / 核验 / 撤回 / 跨范围迁移（组织管理者确定性管理，优化器无写权）──


def record_evidence(session, actor, *, org_id: str, position_id: str, capability: str,
                    scope_space_ids, kind: str = EVIDENCE_KIND_SKILL, skill_name: str = "",
                    artifact_hash: str = "", eval_protocol: str = "", tool_env: str = "",
                    regression_refs=(), source_objective_id: str = "",
                    source_task_id: int = 0, derived_from: str = "") -> CapabilityEvidence:
    """登记能力证据：初始一律 pending（待验证），核验走 verify_evidence。

    - 组织管理者入口：能力证据是确定性管理对象，优化路径不得自写（见
      request_optimizer_write 的只读边界）；
    - capability 必须已在岗位 capability_refs 声明；范围空间必须属于本组织；
    - 同岗位同能力同范围集合的未撤回证据不重复登记（单写者不重复建账）。
    """
    manager = org_service._manager(session, actor, org_id)
    position = _declared(session, org_id, position_id, capability)
    if kind not in {EVIDENCE_KIND_SKILL, EVIDENCE_KIND_SOP, EVIDENCE_KIND_TOOL}:
        raise CapabilityError("invalid_evidence_kind")
    scope = sorted({str(item) for item in scope_space_ids or [] if str(item).strip()})
    if not scope:
        raise CapabilityError("evidence_scope_required")
    for space_id in scope:
        _check_space(session, org_id, space_id)
    if derived_from:
        base = _evidence(session, org_id, derived_from)
        if base.position_id != position_id or base.capability != capability:
            raise CapabilityError("derived_evidence_scope_mismatch")
    existing = [row for row in session.scalars(select(CapabilityEvidence).where(
        CapabilityEvidence.org_id == org_id,
        CapabilityEvidence.position_id == position_id,
        CapabilityEvidence.capability == capability,
        CapabilityEvidence.state.in_([EVIDENCE_PENDING, EVIDENCE_VERIFIED])))
        if set(row.scope()) == set(scope)]
    if existing:
        raise CapabilityError("evidence_duplicate_scope")
    row = CapabilityEvidence(
        evidence_id=f"ev-{position_id}-{capability[:32]}-{uuid4().hex[:10]}"[:64],
        org_id=org_id, position_id=position_id, capability=capability, kind=kind,
        skill_name=str(skill_name or "").strip(),
        artifact_hash=str(artifact_hash or "").strip(),
        eval_protocol=str(eval_protocol or "").strip(),
        tool_env=str(tool_env or "").strip(),
        scope_space_ids_json=_json(scope), state=EVIDENCE_PENDING,
        regression_refs_json=_json(sorted(str(item) for item in regression_refs or ())),
        source_objective_id=str(source_objective_id or "").strip(),
        source_task_id=int(source_task_id or 0), derived_from=str(derived_from or "").strip())
    session.add(row)
    session.flush()
    _event(session, org_id, manager.membership_id, "capability.evidence_recorded",
           row.evidence_id, {"position_id": position_id, "capability": capability,
                             "scope_space_ids": scope, "state": row.state,
                             "position_revision": position.revision,
                             "source_objective_id": row.source_objective_id,
                             "source_task_id": row.source_task_id,
                             "derived_from": row.derived_from})
    return row


def verify_evidence(session, actor, *, org_id: str, evidence_id: str,
                    regression_refs=(), expected_revision: int | None = None) -> CapabilityEvidence:
    """显式核验：绑定齐备（制品 hash/评测协议/工具环境/范围）+ 回归证据才翻 verified。

    只认重新核条件与回归的真实证据；没有回归引用一律拒绝（不因声明或原范围
    verified 自动授权）。核验不触碰 skill_tiers：晋级仍只走原评测链。
    """
    manager = org_service._manager(session, actor, org_id)
    row = _evidence(session, org_id, evidence_id)
    if row.state != EVIDENCE_PENDING:
        raise CapabilityError("evidence_not_pending")
    if not (row.artifact_hash and row.eval_protocol and row.tool_env and row.scope()):
        raise CapabilityError("evidence_binding_incomplete")
    refs = sorted({str(item) for item in regression_refs or () if str(item).strip()})
    if not refs:
        raise CapabilityError("evidence_regression_required")
    org_service._revise(session, CapabilityEvidence, row, expected_revision,
                        state=EVIDENCE_VERIFIED, verified_by=manager.membership_id,
                        verified_at=utcnow(), regression_refs_json=_json(refs))
    _event(session, org_id, manager.membership_id, "capability.evidence_verified",
           row.evidence_id, {"position_id": row.position_id, "capability": row.capability,
                             "scope_space_ids": row.scope(), "regression_refs": refs,
                             "revision": row.revision})
    return row


def revoke_evidence(session, actor, *, org_id: str, evidence_id: str, reason: str,
                    expected_revision: int | None = None) -> CapabilityEvidence:
    """撤回能力证据：保留行与历史（不物理删除），岗位立即失去该资格。"""
    manager = org_service._manager(session, actor, org_id)
    row = _evidence(session, org_id, evidence_id)
    if not str(reason or "").strip():
        raise CapabilityError("revoke_reason_required")
    if row.state == EVIDENCE_REVOKED:
        raise CapabilityError("evidence_not_pending")
    org_service._revise(session, CapabilityEvidence, row, expected_revision,
                        state=EVIDENCE_REVOKED)
    _event(session, org_id, manager.membership_id, "capability.evidence_revoked",
           row.evidence_id, {"position_id": row.position_id, "capability": row.capability,
                             "reason": str(reason).strip()[:400]})
    return row


def extend_scope(session, actor, *, org_id: str, evidence_id: str, extra_space_ids) -> CapabilityEvidence:
    """能力迁移到新范围：生成 pending 派生证据，必须重新核条件并回归。

    原证据保持原范围 verified 不受影响；新范围在核验前不是合格资格——
    不因原范围 healthy/verified 自动跨范围授权（指南 §18.4）。
    """
    manager = org_service._manager(session, actor, org_id)
    base = _evidence(session, org_id, evidence_id)
    if base.state != EVIDENCE_VERIFIED:
        raise CapabilityError("derived_evidence_requires_verified")
    extras = sorted({str(item) for item in extra_space_ids or [] if str(item).strip()})
    if not extras:
        raise CapabilityError("evidence_scope_required")
    covered = set(base.scope())
    fresh = [item for item in extras if item not in covered]
    if not fresh:
        raise CapabilityError("scope_already_covered")
    return record_evidence(session, actor, org_id=org_id, position_id=base.position_id,
                           capability=base.capability, scope_space_ids=fresh,
                           kind=base.kind, skill_name=base.skill_name,
                           artifact_hash=base.artifact_hash, eval_protocol=base.eval_protocol,
                           tool_env=base.tool_env, source_objective_id=base.source_objective_id,
                           source_task_id=base.source_task_id, derived_from=base.evidence_id)


# ── 合格判定：调度器接缝读取的唯一口径 ───────────────────────────────────────


def capability_status(session, *, org_id: str, position_id: str, capability: str,
                      space_id: str, now: datetime | None = None) -> dict:
    """岗位在指定空间对指定能力的合格资格判定（证据缺失 → 不合格 + 原因）。

    - 能力未在岗位声明 → capability_not_declared；
    - 无任何证据行 → 显式待验证（pending_verification / evidence_missing）；
    - 有证据但无一覆盖任务空间 → scope_not_covered（跨范围未重新核验）；
    - 覆盖中存在 verified → 合格；只有 pending → 待验证；只有 revoked → 已撤回。
    """
    del now  # 预留：证据有效期接入点（当前证据行无 TTL，保留参数与组织服务同口径）
    position = session.get(Position, position_id)
    if position is None or position.org_id != org_id:
        return {"qualified": False, "state": "", "reason": "position_not_found", "evidence_id": ""}
    if capability not in _loads(position.capability_refs_json, []):
        return {"qualified": False, "state": "", "reason": "capability_not_declared", "evidence_id": ""}
    rows = sorted(session.scalars(select(CapabilityEvidence).where(
        CapabilityEvidence.org_id == org_id,
        CapabilityEvidence.position_id == position_id,
        CapabilityEvidence.capability == capability)),
        key=lambda row: (row.created_at, row.evidence_id))
    if not rows:
        return {"qualified": False, "state": "pending_verification",
                "reason": "evidence_missing", "evidence_id": ""}
    covering = [row for row in rows if not space_id or space_id in row.scope()]
    if not covering:
        return {"qualified": False, "state": rows[0].state,
                "reason": "scope_not_covered", "evidence_id": rows[0].evidence_id}
    verified = [row for row in covering if row.state == EVIDENCE_VERIFIED]
    if verified:
        return {"qualified": True, "state": EVIDENCE_VERIFIED,
                "reason": "", "evidence_id": verified[0].evidence_id}
    if any(row.state == EVIDENCE_PENDING for row in covering):
        return {"qualified": False, "state": EVIDENCE_PENDING,
                "reason": "evidence_pending_verification", "evidence_id": covering[0].evidence_id}
    return {"qualified": False, "state": EVIDENCE_REVOKED,
            "reason": "evidence_revoked", "evidence_id": covering[0].evidence_id}


def qualified_for_task(session, *, org_id: str, position_id: str, capabilities,
                       space_id: str) -> tuple[bool, list[dict]]:
    """任务所需能力清单的岗位合格判定（全部能力合格才合格），返回逐能力原因。"""
    checks = [capability_status(session, org_id=org_id, position_id=position_id,
                                capability=cap, space_id=space_id) for cap in capabilities]
    return all(check["qualified"] for check in checks), checks


# ── 投影单写者：已采用的独立 SOP 对重建式投影更新只读 ────────────────────────


def projection_update_guard(session, *, org_id: str, skill_name: str,
                            producer_kind: str = "", managed_by: str = "",
                            artifact_hash: str = "") -> dict:
    """重建式投影更新的单写者裁决（DE-18 producer_kind / DE-29 manifest 冻结语义）。

    - producer-kind=projection / managed-by=experience.skill_sync：允许——投影由
      源服务重建（原 write_managed_projection 契约不变）；
    - 目标是已采用的独立 SOP（存在 verified 的 executable_sop 证据绑定该制品）：
      拒绝——投影更新不得覆盖已采用的独立 SOP，其正文走责任人原写入链；
    - 其余存量文件保持原行为（原投影写入者的 unmanaged_projection_conflict 门）。
    纯判定不改数据：调用方（投影重建/搜索采用链）必须尊重 allowed=False。
    """
    if str(producer_kind or "").strip() == "projection" or \
            str(managed_by or "").strip() == "experience.skill_sync":
        return {"allowed": True, "reason": "projection_source_owned_rebuild", "evidence_id": ""}
    name = str(skill_name or "").strip()
    digest = str(artifact_hash or "").strip()
    if name or digest:
        for row in session.scalars(select(CapabilityEvidence).where(
                CapabilityEvidence.org_id == org_id,
                CapabilityEvidence.kind == EVIDENCE_KIND_SOP,
                CapabilityEvidence.state == EVIDENCE_VERIFIED)):
            matched = (name and row.skill_name == name) or (digest and row.artifact_hash == digest)
            if matched:
                return {"allowed": False, "reason": "adopted_sop_source_owned",
                        "evidence_id": row.evidence_id}
    return {"allowed": True, "reason": "unmanaged_legacy", "evidence_id": ""}


# ── 组织知识岗位视图：原 scope 与 source 服务（DE-16），不另建知识库 ──────────


def _position_read_spaces(session, *, org_id: str, position_id: str) -> list[str]:
    """岗位可读空间 = 岗位所在单元显式绑定的业务空间（授权交集的读边界）。"""
    position = org_service._owned(session, Position, position_id, org_id)
    rows = session.scalars(select(UnitSpaceBinding).where(
        UnitSpaceBinding.unit_id == position.unit_id)).all()
    return sorted({row.space_id for row in rows})


def _with_owner_scope(session, claims: list[dict]) -> list[dict]:
    """给检索结果补 owner_scope 来源标注。

    knowledge.search（DE-16）按调用方身份裁剪可见性，结果契约不含 owner_scope；
    岗位视图必须显式区分个人来源与组织来源（分别继承），按 claim_id 回查来源行补齐，
    不改变原检索契约。
    """
    enriched: list[dict] = []
    for claim in claims:
        row = session.get(KnowledgeClaim, str(claim.get("claim_id") or ""))
        enriched.append({**claim,
                         "owner_scope": str(row.owner_scope or "") if row is not None else ""})
    return enriched


def position_knowledge_view(session, *, org_id: str, position_id: str,
                            principal_id: str, subject: str) -> dict:
    """岗位可读的组织知识视图（个人来源与组织来源分别继承，接 DE-38 语义）。

    - 只读岗位单元绑定空间内的主张：来源授权（主张登记所在空间）∩ 岗位单元
      绑定空间，不扩大受众；
    - 组织来源（owner_scope public/空）岗位更换后仍可读——合法组织经验随岗位；
    - 个人来源只有本人可读：他人（含前任）的私有主张一律不进视图——个人私聊/
      凭据不被沉淀成全组织资产。
    """
    spaces = _position_read_spaces(session, org_id=org_id, position_id=position_id)
    claims: list[dict] = []
    conflicts: list[str] = []
    for space_id in spaces:
        result = knowledge.search(session, space_id=space_id, subject=subject,
                                  owner_scope=str(principal_id or ""), include_private=True)
        for claim in _with_owner_scope(session, result.get("claims", [])):
            owner = str(claim.get("owner_scope") or "")
            if owner not in {"", "public"} and owner != str(principal_id or ""):
                continue  # 他人个人来源：不进岗位视图（不沉淀成组织资产）
            claims.append({**claim, "space_id": space_id})
        conflicts.extend(f"{space_id}:{cid}" for cid in result.get("conflicts", []))
    return {"position_id": position_id, "spaces": spaces, "claims": claims,
            "conflicts": conflicts}


def cross_unit_reuse_view(session, *, org_id: str, target_position_id: str,
                          subject: str, principal_id: str = "") -> dict:
    """跨单元复用：来源授权空间 ∩ 目标岗位单元绑定空间（不扩大受众）。

    来源授权 = 组织来源主张（owner_scope public/空）登记所在的空间集合；
    交集之外的空间即使存在同主题主张也不进入目标岗位视图。
    """
    source_rows = [row for row in session.scalars(select(KnowledgeClaim).where(
        KnowledgeClaim.org_id == org_id, KnowledgeClaim.subject == str(subject or "").strip()))
        if row.state in {"active", "conflicting"}
        and str(row.owner_scope or "") in {"", "public"}]
    source_spaces = sorted({row.space_id for row in source_rows if row.space_id})
    target_spaces = _position_read_spaces(session, org_id=org_id,
                                          position_id=target_position_id)
    authorized = sorted(set(source_spaces) & set(target_spaces))
    claims: list[dict] = []
    for space_id in authorized:
        result = knowledge.search(session, space_id=space_id, subject=subject,
                                  owner_scope=str(principal_id or ""), include_private=False)
        claims.extend({**claim, "space_id": space_id}
                      for claim in _with_owner_scope(session, result.get("claims", [])))
    return {"target_position_id": target_position_id, "source_spaces": source_spaces,
            "target_spaces": target_spaces, "authorized_spaces": authorized,
            "excluded_spaces": sorted(set(source_spaces) - set(target_spaces)),
            "claims": claims}


# ── 学习闭环：岗位/目标归因维度 + 确定性管理对象只读边界 ─────────────────────


def capability_attribution(*, position_id: str = "", objective_id: str = "",
                           task_id: int = 0, evidence_id: str = "") -> dict:
    """构造组织归因维度（纯函数）：在线反馈/增量复盘/搜索增强事件携带岗位与目标引用。

    事件仍走原版本评测晋级链（DE-18 制品绑定 / DE-30 协议指纹），本函数只提供
    归因载荷；evidence_id 关联 CapabilityEvidence 行实现证据关联。
    """
    body = {"position_id": str(position_id or "").strip(),
            "objective_id": str(objective_id or "").strip(),
            "task_id": int(task_id or 0),
            "evidence_id": str(evidence_id or "").strip()}
    return {key: value for key, value in body.items() if value}


def normalize_attribution(attribution) -> dict:
    """规范化外部传入的归因载荷（agents 接线复用；空归因返回空 dict）。"""
    if not isinstance(attribution, dict):
        return {}
    return capability_attribution(
        position_id=str(attribution.get("position_id") or ""),
        objective_id=str(attribution.get("objective_id") or ""),
        task_id=attribution.get("task_id") or 0,
        evidence_id=str(attribution.get("evidence_id") or ""))


def attach_attribution(payload: dict, attribution) -> dict:
    """把非空归因并入载荷（键 capability_attribution 已存在时不覆盖）。"""
    normalized = normalize_attribution(attribution)
    if normalized and isinstance(payload, dict) and "capability_attribution" not in payload:
        payload["capability_attribution"] = normalized
    return payload


# 优化路径只读的组织确定性管理对象：评分器、组织政策/revision、岗位授权、
# 业务规则、组织结构与能力证据行（证据由管理者经 record_evidence/verify_evidence
# 写入，优化路径只能引用）。改写尝试一律拒绝并写 permission-denied 审计事件
# （指南 §18.4：优化器不能自己改这些对象）。
READ_ONLY_TARGET_KINDS = frozenset({
    "org_policy", "policy_revision", "eval_protocol", "scorer", "scoring_rule",
    "position_authorization", "action_ceiling", "business_rule", "organization_structure",
    "capability_evidence",
})


def request_optimizer_write(session, *, org_id: str, actor: str, target_kind: str,
                            target_id: str = "", intent: str = "") -> dict:
    """优化路径（在线反馈/增量复盘/搜索增强候选改写）请求写管理对象的唯一入口。

    - READ_ONLY_TARGET_KINDS：拒绝并落 OrganizationEvent
      （action=capability.optimizer_write_denied，outcome=permission_denied），
      失败尝试可审计，不静默；
    - 其余目标（skill_content/knowledge_candidate 等经验与知识候选）返回
      allowed，调用方继续走原服务（skill_evolution/knowledge.service），
      不在本模块开第二写入面。
    """
    kind = str(target_kind or "").strip()
    if kind in READ_ONLY_TARGET_KINDS:
        _event(session, org_id, str(actor or "optimizer"), "capability.optimizer_write_denied",
               str(target_id or kind), {"target_kind": kind, "target_id": str(target_id or ""),
                                        "intent": str(intent or "")[:400],
                                        "outcome": "permission_denied"})
        raise CapabilityError("optimization_target_read_only")
    return {"allowed": True, "target_kind": kind, "target_id": str(target_id or "")}


# ── 任职/调度辅助：岗位能力证据概览（岗位更换/接手检查用） ────────────────────


def position_capability_overview(session, *, org_id: str, position_id: str,
                                 principal_id: str = "") -> dict:
    """岗位全部声明能力的证据状态概览：没有证据的能力显式列待验证。"""
    position = org_service._owned(session, Position, position_id, org_id)
    spaces = _position_read_spaces(session, org_id=org_id, position_id=position_id)
    capabilities = [str(item) for item in _loads(position.capability_refs_json, [])]
    entries = []
    for capability in capabilities:
        per_space = {space_id: capability_status(session, org_id=org_id,
                                                 position_id=position_id,
                                                 capability=capability, space_id=space_id)
                     for space_id in spaces}
        entries.append({"capability": capability,
                        "pending_verification": all(
                            not check["qualified"] for check in per_space.values()),
                        "per_space": per_space})
    return {"position_id": position_id, "position_revision": position.revision,
            "capabilities": entries, "read_spaces": spaces,
            "principal_id": str(principal_id or "")}
