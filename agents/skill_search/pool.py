"""DE-33 候选池：只追加池、Attempt 记录与父子逐 Case 入池判定（搜索增强 S1 第五步）。

对应 SKILL-EVOLUTION-SEARCH §10（候选池、入池与父代选择）与
NATIVE-ARCHITECTURE-INTEGRATION §6（数据契约）；评分向量来自 DE-30
``evaluator.finalize_score_vector`` 的完整性门输出，候选制品来自 DE-32。

核心契约（全部 fail-closed，拒绝时零入池、零副作用）：

- **只追加**：全局候选池 P 只追加种子与通过搜索入池的版本；失败/无收益尝试
  进 Attempt 记录（未入池尝试也保留，绝不丢弃）；已入池候选不因当前不领先
  而删除——发现制品/协议/权限无效时用 ``mark_unusable`` 保留原因并标记，
  下游（前沿/冠军/抽样）不再使用，记录本体仍在池中。
- **硬门先于比较**：安全/完整性硬门先过（``safety_passed=False`` →
  ``rejected_safety``，即使分数向量支配父代也不入池；完整性失败 →
  ``rejected_invalid_evaluation``）。传入 DE-30 ``finalize_result`` 时，
  ``ok=False`` 按其 blocking/quarantine 归类：隔离（security_violation）→
  安全拒绝，其余 → 评测无效。
- **与直接父代逐 Case 比较**（文章口径，``epsilon=1e-6``）：
  ``dominates(p,c) = all(p_i >= c_i - epsilon) and any(p_i > c_i + epsilon)``。
  父支配子 → ``rejected_dominated``；全部差值在容差内 → ``no_effect``
  （同分不改进，不入池）；子支配父或互不支配 → 入池。比较对象永远是
  **直接父代**（必须在池中且可用），不能偷偷改成只与当前均分冠军比。
- **评分向量 fail-closed**：Case 集合与冻结协议完全一致（缺题/多题/重复 →
  拒绝）、每个分数必须是有限数值且落在 [0,1]（NaN/Inf/越界/非数值 →
  拒绝）；绝不填零、绝不部分入池。
- **去重**：相同制品（content/bundle hash）在相同协议（protocol hash）中
  复用已验证评测 → ``rejected_duplicate``，不重复入池、不重复评测调用。
- **确定性**：本模块不含任何墙钟时间与随机数——时间与 RNG 记账由搜索控制层
  （DE-15/DE-34）随事务保存；池记录的创建序号（created_sequence）在入池
  提交时单调分配，与输入 dict/list 的呈现顺序无关；所有聚合（均分/最差分/
  逐 Case 比较）按冻结的**排序 Case 序**计算（``math.fsum`` 求和按构造与
  顺序无关），Case 完成顺序与池输入排列不改变任何判定。
- **作用域边界（S4）**：本模块只描述**一次优化任务**的结构化记录
  （candidate_id/hash/分数向量/评测引用/拒绝原因），不保存候选正文、不产生
  生产技能制品、不写 usage/tier、不与生产 ``discover_skills`` 交互；落盘只进
  DE-29 搜索命名空间（``<evolution>/search/<tenant>/<space>/<lineage>/<search_id>/``）。

真实模型能力实验（预算/审核真值/隔离执行环境）另行 blockedOn，见任务 DE-33；
本模块为纯离线合成可验证逻辑。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .contracts import _canonical_sha256

POOL_SCHEMA_VERSION = 1
POOL_ALGORITHM_VERSION = "skill-search-pool-v1"
POOL_FILE_NAME = "pool.json"

# 文章数值容差（SKILL-EVOLUTION-SEARCH §10.2）。
SCORE_EPSILON = 1e-6

KIND_SEED = "seed"
KIND_CANDIDATE = "candidate"

# 入池判定码（Attempt.decision / admit() 返回）。
DECISION_ADMITTED = "admitted"
DECISION_NO_EFFECT = "no_effect"
DECISION_REJECTED_DOMINATED = "rejected_dominated"
DECISION_REJECTED_DUPLICATE = "rejected_duplicate"
DECISION_REJECTED_INVALID = "rejected_invalid_evaluation"
DECISION_REJECTED_SAFETY = "rejected_safety"
DECISION_REJECTED_PARENT_UNKNOWN = "rejected_parent_unknown"
DECISION_REJECTED_PARENT_UNUSABLE = "rejected_parent_unusable"
DECISION_REJECTED_ID_EXISTS = "rejected_candidate_id_exists"

# 有向量比较的判定才算"入池竞争"；被支配/同分/重复/无效都不产生新池条目。
ADMIT_DECISIONS = (DECISION_ADMITTED,)
NO_POOL_ENTRY_DECISIONS = (
    DECISION_NO_EFFECT,
    DECISION_REJECTED_DOMINATED,
    DECISION_REJECTED_DUPLICATE,
    DECISION_REJECTED_INVALID,
    DECISION_REJECTED_SAFETY,
    DECISION_REJECTED_PARENT_UNKNOWN,
    DECISION_REJECTED_PARENT_UNUSABLE,
    DECISION_REJECTED_ID_EXISTS,
)

# 父子比较结论码（compare_vectors.verdict）。
VERDICT_PARENT_DOMINATES = "parent_dominates"
VERDICT_CHILD_DOMINATES = "child_dominates"
VERDICT_EQUAL_WITHIN_EPSILON = "equal_within_epsilon"
VERDICT_INCOMPARABLE = "incomparable"

# 无向量尝试（Attempt.status）。
ATTEMPT_SCORED = "scored"
ATTEMPT_FAILED = "failed"
ATTEMPT_BLOCKED = "blocked"


def validate_score_vector(
    score_vector: Any,
    *,
    case_ids: list[str],
) -> dict[str, Any]:
    """评分向量完整性校验（fail-closed）：

    - 必须是 dict；Case 键集合与冻结协议**完全一致**（缺题/多题都拒绝）；
    - 每个分数必须是有限数值且落在 [0,1]（NaN/Inf/越界/非数值拒绝）；
    - 返回归一化向量（按排序 Case 序构建，键序与输入呈现顺序无关）。
    """
    expected = sorted({str(cid) for cid in case_ids})
    if not isinstance(score_vector, dict):
        return {"ok": False, "errors": [{"code": "vector_not_dict", "got": type(score_vector).__name__}]}
    provided = {str(k) for k in score_vector}
    errors: list[dict[str, str]] = []
    for case_id in expected:
        if case_id not in provided:
            errors.append({"code": "missing_case", "case_id": case_id})
    for key in sorted(provided - set(expected)):
        errors.append({"code": "unknown_case", "case_id": key})
    normalized: dict[str, float] = {}
    for case_id in expected:
        if case_id not in provided:
            continue
        raw = score_vector[case_id]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            errors.append({"code": "score_not_numeric", "case_id": case_id, "got": str(raw)[:50]})
            continue
        if not math.isfinite(value):
            errors.append({"code": "score_not_finite", "case_id": case_id, "got": str(raw)[:50]})
            continue
        if value < 0.0 or value > 1.0:
            errors.append({"code": "score_out_of_range", "case_id": case_id, "got": repr(value)})
            continue
        normalized[case_id] = value
    if errors:
        return {"ok": False, "errors": errors}
    return {"ok": True, "errors": [], "normalized": normalized}


def compare_vectors(
    parent_scores: dict[str, float],
    child_scores: dict[str, float],
    *,
    case_ids: list[str] | None = None,
    epsilon: float = SCORE_EPSILON,
) -> dict[str, Any]:
    """父子逐 Case 比较文章口径（§10.2，epsilon 容差）：

    ``dominates(p,c) = all(p_i >= c_i - epsilon) and any(p_i > c_i + epsilon)``

    - ``parent_dominates``：父支配子 → 拒绝入池；
    - ``child_dominates``：子支配父 → 允许入池；
    - ``equal_within_epsilon``：全部差值在容差内 → no_effect（同分不改进）；
    - ``incomparable``：互不支配（允许互补低均分候选入池）。

    逐 Case 差值按排序 Case 序计算；容差语义下支配关系保持反对称
    （父支配子要求某维 p>c+epsilon，与全维 |p-c|<=epsilon 矛盾），不存在
    支配环。输入 Case 集合不一致时 fail-closed（caller 先做向量校验）。
    """
    ids = sorted({str(cid) for cid in (case_ids or list(parent_scores))})
    missing_parent = [cid for cid in ids if cid not in parent_scores]
    missing_child = [cid for cid in ids if cid not in child_scores]
    if missing_parent or missing_child:
        return {
            "verdict": "invalid_input",
            "ok": False,
            "missing_parent": missing_parent,
            "missing_child": missing_child,
            "epsilon": float(epsilon),
        }
    diffs: dict[str, float] = {}
    parent_ge_child = True     # all(p_i >= c_i - eps)
    parent_gt_child = False    # any(p_i > c_i + eps)
    child_ge_parent = True
    child_gt_parent = False
    for case_id in ids:
        p = float(parent_scores[case_id])
        c = float(child_scores[case_id])
        diffs[case_id] = c - p
        if p < c - epsilon:
            parent_ge_child = False
        if p > c + epsilon:
            parent_gt_child = True
        if c < p - epsilon:
            child_ge_parent = False
        if c > p + epsilon:
            child_gt_parent = True
    if parent_ge_child and parent_gt_child:
        verdict = VERDICT_PARENT_DOMINATES
    elif child_ge_parent and child_gt_parent:
        verdict = VERDICT_CHILD_DOMINATES
    elif all(abs(d) <= epsilon for d in diffs.values()):
        verdict = VERDICT_EQUAL_WITHIN_EPSILON
    else:
        verdict = VERDICT_INCOMPARABLE
    return {
        "verdict": verdict,
        "ok": True,
        "epsilon": float(epsilon),
        "case_ids": ids,
        "diffs": diffs,
        "max_child_gain": max(diffs.values()) if diffs else 0.0,
        "max_child_loss": min(diffs.values()) if diffs else 0.0,
    }


def _summary_of(vector: dict[str, float], *, case_ids: list[str]) -> dict[str, Any]:
    """池记录聚合：按排序 Case 序 + ``math.fsum``（求和按构造与顺序无关）。"""
    ordered = sorted(case_ids)
    values = [vector[cid] for cid in ordered]
    total = math.fsum(values)
    return {
        "average_score": total / len(values) if values else 0.0,
        "worst_score": min(values) if values else 0.0,
        "passed_case_count": sum(1 for v in values if v >= 1.0),
    }


def _seed_diff_tuple(cumulative_seed_diff: Any) -> tuple[float, float, float]:
    """相对种子累计修改量 → 可比较数值三元组（对齐 DE-32 谱系计数形状）。"""
    if isinstance(cumulative_seed_diff, dict):
        def _num(key: str) -> float:
            try:
                return float(cumulative_seed_diff.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return (_num("added_bytes"), _num("ops"), _num("generations"))
    if isinstance(cumulative_seed_diff, (int, float)) and math.isfinite(float(cumulative_seed_diff)):
        return (float(cumulative_seed_diff), 0.0, 0.0)
    return (0.0, 0.0, 0.0)


def _duplicate_content_evidence(existing: dict[str, Any]) -> dict[str, str]:
    return {
        "existing_candidate_id": str(existing.get("candidate_id") or ""),
        "content_hash": str(existing.get("content_hash") or ""),
        "protocol_hash": str(existing.get("protocol_hash") or ""),
    }


class CandidatePool:
    """一次搜索任务的只追加候选池：种子 + 入池候选 + 全部 Attempt 记录。

    - ``case_ids`` 为冻结协议的 S 集 Case（传入后内部固定为排序序，缺题/多题拒绝）；
    - ``candidates``/``attempts`` 只追加，不提供任何删除/改写已入池条目的接口
      （唯一状态变化是 ``mark_unusable`` 的可用性标记，记录本体保留）；
    - ``created_sequence`` 由入池提交顺序单调分配（种子为 0），与输入排列无关。
    """

    def __init__(self, *, search_id: str, case_ids: list[str], epsilon: float = SCORE_EPSILON):
        self.search_id = str(search_id or "").strip()
        if not self.search_id:
            raise ValueError("search_id_missing")
        ids = sorted({str(cid) for cid in (case_ids or [])})
        if not ids:
            raise ValueError("case_ids_missing")
        self.case_ids = ids
        self.epsilon = float(epsilon)
        self.candidates: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self._next_sequence = 0
        self._next_attempt = 1

    # ── 查询 ────────────────────────────────────────────────────────────────

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        cid = str(candidate_id or "")
        for record in self.candidates:
            if record["candidate_id"] == cid:
                return record
        return None

    def usable_candidates(self) -> list[dict[str, Any]]:
        """可用池条目（已入池且未标记不可用），按 created_sequence 稳定排序。"""
        return [
            record
            for record in sorted(self.candidates, key=lambda r: r["created_sequence"])
            if record.get("admitted") and not record.get("unusable")
        ]

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    # ── 入池主流程 ──────────────────────────────────────────────────────────

    def register_seed(
        self,
        *,
        candidate_id: str,
        score_vector: dict[str, float],
        content_hash: str,
        protocol_hash: str,
        safety_passed: bool = True,
        integrity_passed: bool = True,
        finalize_result: dict[str, Any] | None = None,
        evaluation_ref: str = "",
    ) -> dict[str, Any]:
        """登记种子版本（基线，无父代）：同样过安全/完整性硬门与向量校验。"""
        return self._attempt_admission(
            candidate_id=candidate_id,
            parent_id="",
            kind=KIND_SEED,
            score_vector=score_vector,
            content_hash=content_hash,
            protocol_hash=protocol_hash,
            safety_passed=safety_passed,
            integrity_passed=integrity_passed,
            finalize_result=finalize_result,
            evaluation_ref=evaluation_ref,
            cumulative_seed_diff=None,
        )

    def admit(
        self,
        *,
        candidate_id: str,
        parent_id: str,
        score_vector: dict[str, float] | None = None,
        content_hash: str = "",
        protocol_hash: str = "",
        safety_passed: bool = True,
        integrity_passed: bool = True,
        finalize_result: dict[str, Any] | None = None,
        evaluation_ref: str = "",
        cumulative_seed_diff: Any = None,
    ) -> dict[str, Any]:
        """候选入池判定：硬门 → 向量校验 → 去重 → 直接父代逐 Case 比较。

        返回判定结果（``decision`` + ``ok``），每次调用都会追加一条 Attempt
        记录（未入池尝试也保留）。``finalize_result`` 是 DE-30
        ``finalize_score_vector`` 的输出；同时给 ``score_vector`` 时以
        finalize 的判定优先（其 ``score_vector`` 覆盖手工向量）。
        """
        return self._attempt_admission(
            candidate_id=candidate_id,
            parent_id=parent_id,
            kind=KIND_CANDIDATE,
            score_vector=score_vector,
            content_hash=content_hash,
            protocol_hash=protocol_hash,
            safety_passed=safety_passed,
            integrity_passed=integrity_passed,
            finalize_result=finalize_result,
            evaluation_ref=evaluation_ref,
            cumulative_seed_diff=cumulative_seed_diff,
        )

    def _attempt_admission(
        self,
        *,
        candidate_id: str,
        parent_id: str,
        kind: str,
        score_vector: dict[str, float] | None,
        content_hash: str,
        protocol_hash: str,
        safety_passed: bool,
        integrity_passed: bool,
        finalize_result: dict[str, Any] | None,
        evaluation_ref: str,
        cumulative_seed_diff: Any,
    ) -> dict[str, Any]:
        cid = str(candidate_id or "").strip()
        pid = str(parent_id or "").strip()

        def _finish(
            decision: str,
            reason: str,
            *,
            evidence: list[Any] | None = None,
            verdict: str = "",
            vector: dict[str, float] | None = None,
        ) -> dict[str, Any]:
            self._append_attempt(
                candidate_id=cid,
                parent_id=pid,
                status=ATTEMPT_SCORED if vector is not None else ATTEMPT_FAILED,
                decision=decision,
                reason=reason,
                evidence=evidence or [],
                candidate_hash=str(content_hash or ""),
                protocol_hash=str(protocol_hash or ""),
                completed_cases=len(vector) if vector else 0,
                verdict=verdict,
            )
            return {
                "ok": decision in ADMIT_DECISIONS,
                "decision": decision,
                "reason": reason,
                "verdict": verdict,
                "evidence": list(evidence or []),
                "candidate_id": cid,
                "parent_id": pid,
                "attempt_id": self.attempts[-1]["attempt_id"],
                "pool_size": len(self.candidates),
            }

        if not cid:
            return _finish(DECISION_REJECTED_INVALID, "candidate_id_missing")
        if self.get_candidate(cid) is not None:
            return _finish(
                DECISION_REJECTED_ID_EXISTS,
                f"candidate_id already in pool: {cid}",
            )
        if not str(content_hash or "").strip() or not str(protocol_hash or "").strip():
            return _finish(
                DECISION_REJECTED_INVALID,
                "content_hash/protocol_hash required（无制品与协议身份不能入池或去重）",
            )

        # DE-30 完整性门输出优先：ok=False 按隔离/其余归类。
        vector = dict(score_vector) if isinstance(score_vector, dict) else None
        if finalize_result is not None:
            if not isinstance(finalize_result, dict) or not finalize_result.get("ok"):
                blocking = list((finalize_result or {}).get("blocking") or [])
                quarantined = bool((finalize_result or {}).get("candidate_quarantined"))
                if quarantined:
                    return _finish(
                        DECISION_REJECTED_SAFETY,
                        "finalize gate quarantined candidate (security_violation)",
                        evidence=blocking,
                    )
                return _finish(
                    DECISION_REJECTED_INVALID,
                    str((finalize_result or {}).get("reason") or "finalize gate failed"),
                    evidence=blocking,
                )
            finalize_vector = finalize_result.get("score_vector")
            if not isinstance(finalize_vector, dict):
                return _finish(DECISION_REJECTED_INVALID, "finalize result missing score_vector")
            vector = dict(finalize_vector)

        if vector is None:
            return _finish(DECISION_REJECTED_INVALID, "score_vector_missing")

        checked = validate_score_vector(vector, case_ids=self.case_ids)
        if not checked.get("ok"):
            return _finish(
                DECISION_REJECTED_INVALID,
                "score vector fail-closed（缺题/多题/NaN/Inf/越界/非数值）",
                evidence=checked.get("errors", []),
                vector=vector,
            )
        normalized: dict[str, float] = checked["normalized"]

        # 硬门：安全先于均分/比较（safety_before_mean）。
        if not safety_passed:
            return _finish(
                DECISION_REJECTED_SAFETY,
                "safety gate failed; 安全/边界硬门先于分数比较，支配父代也不入池",
                vector=normalized,
            )
        if not integrity_passed:
            return _finish(
                DECISION_REJECTED_INVALID,
                "integrity gate failed; 完整性硬门未通过",
                vector=normalized,
            )

        # 去重：相同制品相同协议复用已验证评测。
        for existing in self.candidates:
            if (
                existing.get("content_hash") == str(content_hash)
                and existing.get("protocol_hash") == str(protocol_hash)
            ):
                return _finish(
                    DECISION_REJECTED_DUPLICATE,
                    "相同制品在相同协议中已有已验证评测：复用旧评测，不重复入池",
                    evidence=[_duplicate_content_evidence(existing)],
                    vector=normalized,
                )

        # 父代判定：种子无父代；候选必须与**直接父代**比较（父代须在池中且可用）。
        if kind == KIND_SEED:
            if pid:
                return _finish(DECISION_REJECTED_INVALID, "seed must not carry a parent_id")
        else:
            if not pid:
                return _finish(DECISION_REJECTED_INVALID, "parent_id_missing")
            parent = self.get_candidate(pid)
            if parent is None:
                return _finish(
                    DECISION_REJECTED_PARENT_UNKNOWN,
                    f"direct parent not in pool: {pid}（只能与池内直接父代比较）",
                    vector=normalized,
                )
            if parent.get("unusable"):
                return _finish(
                    DECISION_REJECTED_PARENT_UNUSABLE,
                    f"direct parent unusable: {pid}（{parent.get('unusable_reason') or 'unspecified'}）",
                    vector=normalized,
                )
            comparison = compare_vectors(
                parent["score_vector"],
                normalized,
                case_ids=self.case_ids,
                epsilon=self.epsilon,
            )
            verdict = str(comparison.get("verdict") or "")
            if verdict == VERDICT_PARENT_DOMINATES:
                return _finish(
                    DECISION_REJECTED_DOMINATED,
                    "父代逐 Case 支配子代（含容差）：同分不改进/全面退化，不入池",
                    evidence=[
                        {"max_child_gain": comparison.get("max_child_gain"),
                         "max_child_loss": comparison.get("max_child_loss")}
                    ],
                    verdict=verdict,
                    vector=normalized,
                )
            if verdict == VERDICT_EQUAL_WITHIN_EPSILON:
                return _finish(
                    DECISION_NO_EFFECT,
                    "全部差值在 epsilon 容差内：同分不改进，no_effect 不入池",
                    verdict=verdict,
                    vector=normalized,
                )
            if verdict not in {VERDICT_CHILD_DOMINATES, VERDICT_INCOMPARABLE}:
                return _finish(
                    DECISION_REJECTED_INVALID,
                    f"unexpected comparison verdict: {verdict}",
                    verdict=verdict,
                    vector=normalized,
                )

        # 入池提交：只追加 + 单调序号。
        parent_iteration = -1
        if kind == KIND_CANDIDATE:
            parent_iteration = int(self.get_candidate(pid).get("iteration") or 0)
        summary = _summary_of(normalized, case_ids=self.case_ids)
        record: dict[str, Any] = {
            "candidate_id": cid,
            "parent_id": pid,
            "kind": kind,
            "iteration": 0 if kind == KIND_SEED else parent_iteration + 1,
            "content_hash": str(content_hash),
            "protocol_hash": str(protocol_hash),
            "score_vector": {case_id: normalized[case_id] for case_id in self.case_ids},
            "average_score": summary["average_score"],
            "worst_score": summary["worst_score"],
            "passed_case_count": summary["passed_case_count"],
            "created_sequence": self._next_sequence,
            "cumulative_seed_diff": cumulative_seed_diff,
            "evaluation_ref": str(evaluation_ref or ""),
            "admitted": True,
            "unusable": False,
            "unusable_reason": "",
            "admission": {
                "decision": DECISION_ADMITTED,
                "verdict": "" if kind == KIND_SEED else verdict,
                "epsilon": self.epsilon,
                "algorithm_version": POOL_ALGORITHM_VERSION,
            },
        }
        self.candidates.append(record)
        self._next_sequence += 1
        return _finish(
            DECISION_ADMITTED,
            "" if kind == KIND_SEED else f"admitted vs direct parent {pid} ({verdict})",
            verdict="" if kind == KIND_SEED else verdict,
            vector=normalized,
        )

    # ── Attempt 与可用性 ────────────────────────────────────────────────────

    def _append_attempt(
        self,
        *,
        candidate_id: str,
        parent_id: str,
        status: str,
        decision: str,
        reason: str,
        evidence: list[Any],
        candidate_hash: str,
        protocol_hash: str,
        completed_cases: int,
        verdict: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        attempt = {
            "attempt_id": f"attempt-{self._next_attempt:04d}",
            "attempt_index": self._next_attempt,
            "candidate_id": candidate_id,
            "parent_id": parent_id,
            "status": status,
            "decision": decision,
            "reason": reason,
            "verdict": verdict,
            "evidence": [dict(e) if isinstance(e, dict) else e for e in evidence],
            "completed_cases": int(completed_cases),
            "candidate_hash": candidate_hash,
            "protocol_hash": protocol_hash,
            "note": note,
        }
        self.attempts.append(attempt)
        self._next_attempt += 1
        return attempt

    def record_failed_attempt(
        self,
        *,
        candidate_id: str,
        parent_id: str = "",
        reason: str,
        evidence: list[Any] | None = None,
        candidate_hash: str = "",
        protocol_hash: str = "",
        completed_cases: int = 0,
        status: str = ATTEMPT_FAILED,
        note: str = "",
    ) -> dict[str, Any]:
        """无向量尝试落账（infra 失败/预算中止/生成失败等）：Attempt 也保留。"""
        if status not in {ATTEMPT_FAILED, ATTEMPT_BLOCKED}:
            status = ATTEMPT_FAILED
        return self._append_attempt(
            candidate_id=str(candidate_id or ""),
            parent_id=str(parent_id or ""),
            status=status,
            decision="",
            reason=str(reason or ""),
            evidence=list(evidence or []),
            candidate_hash=str(candidate_hash or ""),
            protocol_hash=str(protocol_hash or ""),
            completed_cases=int(completed_cases),
            note=note,
        )

    def mark_unusable(
        self,
        candidate_id: str,
        *,
        reason: str,
        evidence: list[Any] | None = None,
    ) -> dict[str, Any]:
        """制品/协议/权限无效 → 标记不可用并保留原因（记录不删除，不再被采样）。"""
        record = self.get_candidate(candidate_id)
        if record is None:
            return {"ok": False, "error": "candidate_not_found", "candidate_id": str(candidate_id or "")}
        if record.get("unusable"):
            return {
                "ok": True,
                "candidate_id": record["candidate_id"],
                "already_unusable": True,
                "unusable_reason": record.get("unusable_reason") or "",
            }
        record["unusable"] = True
        record["unusable_reason"] = str(reason or "unspecified")
        record["unusable_evidence"] = [dict(e) if isinstance(e, dict) else e for e in (evidence or [])]
        return {
            "ok": True,
            "candidate_id": record["candidate_id"],
            "already_unusable": False,
            "unusable_reason": record["unusable_reason"],
        }

    # ── 序列化（确定性状态，无墙钟时间） ────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": POOL_SCHEMA_VERSION,
            "algorithm_version": POOL_ALGORITHM_VERSION,
            "search_id": self.search_id,
            "epsilon": self.epsilon,
            "case_ids": list(self.case_ids),
            "next_sequence": self._next_sequence,
            "next_attempt": self._next_attempt,
            "candidates": [dict(record) for record in self.candidates],
            "attempts": [dict(attempt) for attempt in self.attempts],
            "content_digest": self.content_digest(),
        }

    def content_digest(self) -> str:
        """池内容摘要（不含自身 digest 字段）：落盘完整性前后比对用。"""
        payload = {
            "schema_version": POOL_SCHEMA_VERSION,
            "search_id": self.search_id,
            "epsilon": self.epsilon,
            "case_ids": list(self.case_ids),
            "candidates": self.candidates,
            "attempts": self.attempts,
        }
        return _canonical_sha256(payload)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidatePool":
        if not isinstance(data, dict):
            raise ValueError("pool_payload_not_dict")
        pool = cls(
            search_id=str(data.get("search_id") or ""),
            case_ids=list(data.get("case_ids") or []),
            epsilon=float(data.get("epsilon") or SCORE_EPSILON),
        )
        pool.candidates = [dict(record) for record in (data.get("candidates") or [])]
        pool.attempts = [dict(attempt) for attempt in (data.get("attempts") or [])]
        pool._next_sequence = int(data.get("next_sequence") or len(pool.candidates))
        pool._next_attempt = int(data.get("next_attempt") or len(pool.attempts) + 1)
        return pool

    def save(self, directory: str | Path) -> dict[str, Any]:
        """池记录落盘：只写调用方给定的目录（应为 DE-29 搜索命名空间）。"""
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        path = target / POOL_FILE_NAME
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path.write_text(payload, encoding="utf-8")
        return {"ok": True, "path": str(path), "bytes": len(payload.encode("utf-8")), "digest": self.content_digest()}

    @classmethod
    def load(cls, directory: str | Path) -> "CandidatePool":
        path = Path(directory) / POOL_FILE_NAME
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
