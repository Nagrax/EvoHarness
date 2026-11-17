"""DE-30 Case 协议：S/V/H 访问角色 + origin_group 来源纪律 + 冻结与封存。

对应 SKILL-EVOLUTION-SEARCH §4/§7 与 NATIVE-ARCHITECTURE-INTEGRATION §6：
Search 的 S/V/H 是**访问角色**，不替代原数据来源分类——每个 Case 同时保留
``origin_group=evidence|score|curated_run``、原 split、family、source 引用与
consent/scope，新协议角色不抹掉来源纪律。

来源纪律（§6：出生证据不能给自己发成绩）：
- ``evidence``（出生证据组）可在开发 S 中诊断/重答；即使同样题目重答成功，
  也不能记入旧线上健康成绩 → 不得授 V/H；
- ``score``（原使用窗口/线上成绩）已被观测 → 可作 S/V（V 为 selection 数据），
  不得当从未观测过的封存 H；
- ``curated_run``（按协议新整理的 Run 资料）授 H 时必须引用**独立封存集**
  （sealed_holdout：ref + sha256 + case_ids），原 split=final_holdout 或已登记
  污染的题一律拒绝授 H（已观测 holdout 不能重命名为新 H）。

复用原实验设施（不重造第二套登记）：
- 污染登记：``eval.evolution_experiment.load_contamination / register_contamination``；
- 近重复泄漏判定：``eval.evolution_experiment.is_near_duplicate``（H 不得与 S/V 近重复）；
- 只读快照语义：冻结 Case 集合摘要（``case_readonly_digest``）+ freeze 后
  ``verify_case_freeze`` 逐字段比对——对应原 holdout 只读快照的 digest 前后比对。

冻结：Case 集合 + 冻结协议 → ``CaseFreeze.fingerprint``（写进
``SearchManifest.protocol["splits"]`` / ``freeze_protocol(splits=...)``，协议指纹
随之绑定；角色/Case 变化 → 新协议指纹 → 旧评测不可复用，复用判定沿用 DE-29
``SearchManifest.check_evaluation_reuse``）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# S/V/H 是访问角色（SKILL-EVOLUTION-SEARCH §4）：S=搜索集（可进反思），
# V=发布验证（有限次选择），H=最终封存（搜索停止后只跑一次）。
ROLE_S = "S"
ROLE_V = "V"
ROLE_H = "H"
CASE_ROLES = (ROLE_S, ROLE_V, ROLE_H)
DEFAULT_ROLE = ROLE_S

ORIGIN_EVIDENCE = "evidence"
ORIGIN_SCORE = "score"
ORIGIN_CURATED_RUN = "curated_run"
ORIGIN_GROUPS = (ORIGIN_EVIDENCE, ORIGIN_SCORE, ORIGIN_CURATED_RUN)

# 原实验/回放池的 split 口径（沿用 eval/evolution_experiment 与 online_skill_eval）。
OBSERVED_SPLITS = {"final_holdout", "promotion_test", "mutate_dev", "evidence"}

SEAL_FIELD_REF = "ref"
SEAL_FIELD_SHA = "sha256"
SEAL_FIELD_CASES = "case_ids"

PROMOTION_GRADE_SEALED = "sealed_holdout"
PROMOTION_GRADE_SELECTION = "selection_only"
PROMOTION_GRADE_DEV_ONLY = "dev_only"


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _reject(error: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, "reason": reason, **detail}


def _violation(case_id: str, code: str, reason: str) -> dict[str, str]:
    return {"case_id": case_id, "code": code, "reason": reason}


# ── 污染登记复用（eval/evolution_experiment，不重造第二套） ────────────────


def load_contamination_registry(path: str | Path, items: list[dict] | None = None) -> dict:
    """读取/重建历史 holdout 污染登记（沿用原实验语义：只写一次，后续只读）。"""
    from eval.evolution_experiment import load_contamination

    return load_contamination(Path(path), items)


def register_observed_cases(
    task_ids: list[str],
    *,
    reason: str,
    evidence: list[str] | None = None,
    path: str | Path,
    items: list[dict] | None = None,
) -> dict:
    """显式登记已观测题（含其任务族）；登记只合并不删除旧证据。"""
    from eval.evolution_experiment import register_contamination

    return register_contamination(
        task_ids, reason=reason, evidence=evidence, path=Path(path), items=items
    )


def contamination_hits(case: dict, contamination: dict | None) -> list[str]:
    """该 Case 是否命中污染登记（task id 或任务族已在历史 holdout 中被观测）。"""
    if not isinstance(contamination, dict):
        return []
    hits: list[str] = []
    case_id = str(case.get("case_id") or "")
    family = str(case.get("family") or "")
    if case_id and case_id in {str(x) for x in (contamination.get("taskIds") or [])}:
        hits.append(f"taskId:{case_id}")
    if family and family in {str(x) for x in (contamination.get("families") or [])}:
        hits.append(f"family:{family}")
    return hits


# ── Case 规范化与集合构建（缺 id / 重复 id / 非法来源 fail-closed） ─────────


def normalize_case(raw: dict[str, Any]) -> dict[str, Any]:
    """规范化单个 Case：保留 origin_group/原 split/family/source/consent + 角色请求。

    不猜字段：case_id、origin_group、family 缺失或非法直接报错（不默认填充）。
    """
    case_id = str(raw.get("case_id") or raw.get("task_id") or raw.get("sample_id") or "").strip()
    if not case_id:
        raise ValueError("case_id_missing")
    origin_group = str(raw.get("origin_group") or "").strip()
    if origin_group not in ORIGIN_GROUPS:
        raise ValueError(f"invalid_origin_group:{case_id}:{origin_group or 'empty'}")
    family = str(raw.get("family") or "").strip()
    if not family:
        raise ValueError(f"family_missing:{case_id}")
    case = dict(raw)
    case["case_id"] = case_id
    case["origin_group"] = origin_group
    case["family"] = family
    case["origin_split"] = str(raw.get("origin_split") or raw.get("split") or "")
    case["revision"] = str(raw.get("revision") or "1")
    case["source_ref"] = str(raw.get("source_ref") or "")
    case["source_revision"] = str(raw.get("source_revision") or "")
    case["consent_scope"] = str(raw.get("consent_scope") or "")
    case["question"] = str(raw.get("question") or "")
    # 角色请求：未声明时留空（由 assign_protocol_roles 决定，默认 S）。
    case["protocol_role"] = str(raw.get("protocol_role") or "").strip()
    return case


def build_case_set(raw_cases: list[dict[str, Any]]) -> dict[str, Any]:
    """构建 Case 集合：重复 ID / 缺 ID / 非法来源整体拒绝（fail-closed，不静默去重）。"""
    cases: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    errors: list[dict[str, str]] = []
    for index, raw in enumerate(raw_cases or []):
        try:
            case = normalize_case(raw if isinstance(raw, dict) else {})
        except ValueError as exc:
            errors.append({"index": str(index), "code": str(exc).split(":")[0], "reason": str(exc)})
            continue
        if case["case_id"] in seen:
            errors.append(
                _violation(case["case_id"], "duplicate_case_id", f"重复 Case ID（首次出现于第 {seen[case['case_id']]} 项）")
            )
            continue
        seen[case["case_id"]] = index
        cases.append(case)
    if errors:
        return _reject("case_set_invalid", "Case 集合存在缺 ID/重复 ID/非法来源/缺族成员，拒绝建立协议", errors=errors)
    if not cases:
        return _reject("case_set_empty", "Case 集合为空：协议至少需要一个 Case")
    return {"ok": True, "cases": cases}


# ── S/V/H 角色授予（来源纪律 + 封存集引用 + 泄漏门） ────────────────────────


def _sealed_case_ids(sealed_holdout: dict | None) -> dict[str, Any]:
    """校验独立封存集引用：ref + sha256 + case_ids 全部非空才算有效引用。"""
    if not isinstance(sealed_holdout, dict):
        return {}
    ref = str(sealed_holdout.get(SEAL_FIELD_REF) or "").strip()
    sha = str(sealed_holdout.get(SEAL_FIELD_SHA) or "").strip()
    ids = [str(x).strip() for x in (sealed_holdout.get(SEAL_FIELD_CASES) or []) if str(x).strip()]
    if not ref or not sha or not ids:
        return {}
    return {"ref": ref, "sha256": sha, "case_ids": set(ids)}


def assign_protocol_roles(
    cases: list[dict[str, Any]],
    *,
    sealed_holdout: dict | None = None,
    contamination: dict | None = None,
    dup_threshold: float = 0.8,
) -> dict[str, Any]:
    """授予 S/V/H 访问角色；来源纪律与封存要求不满足时整体拒绝（fail-closed）。

    规则（§4/§6）：
    - evidence → 只能 S（诊断/重答是开发证据，不能升 V/H）；
    - score → S/V 可授；H 拒绝（原使用窗口/线上成绩已被观测）；
    - curated_run → H 仅当 case 在**独立封存集引用**的 case_ids 中；
    - 原 split=final_holdout 或命中污染登记（task/族）→ 不得授 H；
    - H 的 question 缺失 → 无法做泄漏检查，拒绝；H 与任何 S/V 题 near-duplicate → 拒绝。
    """
    violations: list[dict[str, str]] = []
    seal = _sealed_case_ids(sealed_holdout)
    assigned: list[dict[str, Any]] = []

    for case in cases:
        requested = str(case.get("protocol_role") or "").strip() or DEFAULT_ROLE
        if requested not in CASE_ROLES:
            violations.append(_violation(case["case_id"], "invalid_role_request", f"未知协议角色请求: {requested}"))
            continue
        origin = case["origin_group"]
        split = str(case.get("origin_split") or "")
        if requested in {ROLE_V, ROLE_H} and origin == ORIGIN_EVIDENCE:
            violations.append(
                _violation(
                    case["case_id"],
                    "evidence_cannot_be_final_holdout",
                    "出生证据组只能在开发 S 中诊断/重答；重答成功也不构成线上成绩或封存检验",
                )
            )
            continue
        if requested == ROLE_H:
            if origin == ORIGIN_SCORE:
                violations.append(
                    _violation(case["case_id"], "observed_holdout_not_sealed", "score 来源（原使用窗口）已被观测，不能当从未观测的封存 H")
                )
                continue
            if split in OBSERVED_SPLITS and split != "":
                violations.append(
                    _violation(case["case_id"], "observed_holdout_not_sealed", f"原 split={split} 的题已被观测，需另设独立封存集引用才可作 H")
                )
                continue
            if contamination_hits(case, contamination):
                hits = ",".join(contamination_hits(case, contamination))
                violations.append(
                    _violation(case["case_id"], "contaminated_case_not_sealed", f"命中历史 holdout 污染登记（{hits}），不得充当新封存 H")
                )
                continue
            if not seal:
                violations.append(
                    _violation(case["case_id"], "sealed_ref_required", "H 必须引用独立封存集（ref+sha256+case_ids），不接受无引用的裸题")
                )
                continue
            if case["case_id"] not in seal["case_ids"]:
                violations.append(
                    _violation(case["case_id"], "sealed_ref_required", f"H 题不在封存集 {seal['ref']} 的 case_ids 清单中")
                )
                continue
            if not str(case.get("question") or "").strip():
                violations.append(
                    _violation(case["case_id"], "holdout_question_missing", "H 题缺 question 文本，无法执行近重复泄漏检查（fail-closed）")
                )
                continue
        assigned.append({**case, "protocol_role": requested})

    if violations:
        return _reject(
            "role_assignment_rejected",
            "S/V/H 角色授予违反来源纪律或封存要求，整体拒绝（不部分授角色）",
            violations=violations,
        )

    # 泄漏门：H 的题与任何 S/V 的题近重复 → 拒绝（跨集合防泄漏沿用原实验判定）。
    from eval.evolution_experiment import is_near_duplicate

    exposed = [c for c in assigned if c["protocol_role"] in {ROLE_S, ROLE_V}]
    for hold in [c for c in assigned if c["protocol_role"] == ROLE_H]:
        for other in exposed:
            if is_near_duplicate(str(hold.get("question") or ""), str(other.get("question") or ""), dup_threshold):
                violations.append(
                    _violation(hold["case_id"], "holdout_leak", f"与已开放题 {other['case_id']} 近重复：不得进入封存 H")
                )
    if violations:
        return _reject("role_assignment_rejected", "H 集合与 S/V 存在近重复泄漏，整体拒绝", violations=violations)

    roles: dict[str, list[str]] = {ROLE_S: [], ROLE_V: [], ROLE_H: []}
    for case in assigned:
        roles[case["protocol_role"]].append(case["case_id"])
    if roles[ROLE_H]:
        grade = PROMOTION_GRADE_SEALED
    elif roles[ROLE_V]:
        grade = PROMOTION_GRADE_SELECTION  # 只有 V：反复用于选择须记 selection，不具备无偏最终检验
    else:
        grade = PROMOTION_GRADE_DEV_ONLY
    return {
        "ok": True,
        "cases": assigned,
        "roles": {role: sorted(ids) for role, ids in roles.items()},
        "promotion_grade": grade,
        "sealed_holdout_ref": str((seal or {}).get("ref") or ""),
        "violations": [],
    }


# ── 冻结：Case 集合 + 协议 → 指纹（进 SearchManifest，协议变化即新指纹） ────


def _case_entry(case: dict[str, Any]) -> dict[str, str]:
    return {
        "case_id": case["case_id"],
        "revision": str(case.get("revision") or "1"),
        "origin_group": case["origin_group"],
        "origin_split": str(case.get("origin_split") or ""),
        "family": case["family"],
        "protocol_role": case["protocol_role"],
        "source_ref": str(case.get("source_ref") or ""),
        "source_revision": str(case.get("source_revision") or ""),
        "consent_scope": str(case.get("consent_scope") or ""),
        "question": str(case.get("question") or ""),
    }


@dataclass
class CaseFreeze:
    """冻结的 Case 协议：固定顺序 + 全量身份条目 + 指纹（只增不改，变化即拒绝）。"""

    ordered_case_ids: list[str]
    entries: list[dict[str, str]] = field(default_factory=list)
    cases_sha256: str = ""
    fingerprint: str = ""
    protocol: dict[str, Any] = field(default_factory=dict)
    roles: dict[str, list[str]] = field(default_factory=dict)
    promotion_grade: str = PROMOTION_GRADE_DEV_ONLY

    @property
    def role_of(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for role, ids in self.roles.items():
            for case_id in ids:
                out[case_id] = role
        return out


def freeze_case_protocol(
    cases: list[dict[str, Any]],
    *,
    protocol: dict[str, Any] | None = None,
    roles_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """冻结 Case 协议：每个 Case 必须已带 protocol_role（未授角色拒绝冻结）。"""
    for case in cases:
        if str(case.get("protocol_role") or "") not in CASE_ROLES:
            return _reject(
                "roles_not_assigned",
                f"Case {case.get('case_id')} 未授予 S/V/H 角色：先调用 assign_protocol_roles 再冻结",
            )
    entries = sorted((_case_entry(case) for case in cases), key=lambda e: e["case_id"])
    frozen_protocol = dict(protocol or {})
    cases_sha256 = _canonical_sha256(entries)
    fingerprint = _canonical_sha256({"cases_sha256": cases_sha256, "protocol": frozen_protocol})
    roles: dict[str, list[str]] = {ROLE_S: [], ROLE_V: [], ROLE_H: []}
    for entry in entries:
        roles[entry["protocol_role"]].append(entry["case_id"])
    grade = (
        PROMOTION_GRADE_SEALED if roles[ROLE_H]
        else PROMOTION_GRADE_SELECTION if roles[ROLE_V]
        else PROMOTION_GRADE_DEV_ONLY
    )
    if roles_result and roles_result.get("ok"):
        grade = str(roles_result.get("promotion_grade") or grade)
    freeze = CaseFreeze(
        ordered_case_ids=sorted(case["case_id"] for case in cases),
        entries=entries,
        cases_sha256=cases_sha256,
        fingerprint=fingerprint,
        protocol=frozen_protocol,
        roles={role: sorted(ids) for role, ids in roles.items()},
        promotion_grade=grade,
    )
    return {"ok": True, "freeze": freeze}


def verify_case_freeze(
    freeze: CaseFreeze,
    cases: list[dict[str, Any]],
    *,
    protocol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """冻结后复核：Case 增/删/改（含角色/来源/题目文本）或协议变化 → 拒绝。

    对应原实验 holdout 只读快照语义：快照 digest 前后必须一致；搜索期间任何
    Case 内容漂移都使本协议作废，不能拿漂移后的题继续冒用旧指纹。
    """
    drift: list[dict[str, str]] = []
    frozen_ids = {entry["case_id"] for entry in freeze.entries}
    current: dict[str, dict[str, str]] = {}
    for case in cases:
        if str(case.get("protocol_role") or "") not in CASE_ROLES:
            drift.append({"case_id": str(case.get("case_id") or ""), "kind": "role_missing"})
            continue
        current[case["case_id"]] = _case_entry(case)
    for case_id in sorted(frozen_ids - set(current)):
        drift.append({"case_id": case_id, "kind": "missing"})
    for case_id in sorted(set(current) - frozen_ids):
        drift.append({"case_id": case_id, "kind": "added"})
    for case_id in sorted(frozen_ids & set(current)):
        if current[case_id] != next(e for e in freeze.entries if e["case_id"] == case_id):
            drift.append({"case_id": case_id, "kind": "modified"})
    frozen_protocol = dict(protocol if protocol is not None else freeze.protocol)
    fingerprint = _canonical_sha256({"cases_sha256": _canonical_sha256(sorted(current.values(), key=lambda e: e["case_id"])), "protocol": frozen_protocol})
    if drift:
        return _reject("case_protocol_drift", "冻结 Case 协议发生漂移（增/删/改），本协议作废", drift=drift)
    if fingerprint != freeze.fingerprint:
        return _reject("protocol_changed", "冻结协议（rubric/verifier/模型环境/角色范围）已变化：跨协议分数不可混用")
    return {"ok": True, "reason": "", "fingerprint": freeze.fingerprint}


def case_readonly_digest(cases: list[dict[str, Any]]) -> str:
    """Case 集合只读快照摘要（对应原 holdout _state_digest 的前后比对口径）。"""
    entries = sorted((_case_entry(case) for case in cases), key=lambda e: e["case_id"])
    return _canonical_sha256(entries)


def protocol_freeze_entry(freeze: CaseFreeze) -> dict[str, Any]:
    """写进 SearchManifest.protocol["splits"]（经 freeze_protocol(splits=...)），
    使 manifest 协议指纹绑定 Case 集合与角色范围；角色/Case 变化 → 新指纹。"""
    return {
        "cases": {"count": len(freeze.ordered_case_ids), "ordered_case_ids": list(freeze.ordered_case_ids)},
        "roles": {role: list(ids) for role, ids in freeze.roles.items()},
        "promotion_grade": freeze.promotion_grade,
        "cases_sha256": freeze.cases_sha256,
        "fingerprint": freeze.fingerprint,
    }
