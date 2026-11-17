"""DE-30 候选执行适配边界：真实 Run 评测、完整性门、触发路由与公平对照。

对应 NATIVE-ARCHITECTURE-INTEGRATION §6 与 SKILL-EVOLUTION-SEARCH §3/§7：

执行适配边界（定义在 online_skill_eval 评测边界上，不改造旧函数）：
- 输入：candidate bundle + 原 sample ref（Case）+ frozen verifier；
- 输出：``ExecutionOutcome``——实际加载 hash、response/tool/artifact refs、
  执行级别（text_only / tool_replay / artifact_verified）、终态有效性、
  分项分数与全部成本记录。
- 旧 ``side_query`` 文本重答作为 **text_only 实现**保留（复用
  ``online_skill_eval._generate_variant_response_async``，不替换）；工具/产物验证
  走 DE-11 隔离执行通道（``IsolatedRunExecutor``，执行器注入，离线用合成执行器，
  不调真实模型）。

结果完整性（fail-closed，缺题/重复/NaN/旧 rubric/错误候选加载 → 不产生有效分数向量）：
- 每题唯一终态、固定 Case 顺序；缺失/重复/NaN/Inf/越界分数/协议指纹不一致/
  实际加载 hash 与候选不符 → 整体拒绝，不填零入池；
- 区分三种终态：``skill_scored_fail``（答错/格式错/工具用错，有效失败保留低分）、
  ``infra_error``（429/超时/worker 丢失/环境缺失，无可比分数，不伪装技能 0 分）、
  ``security_violation``（越权/改只读保护，直接隔离候选且证据可追踪）。

触发评测：沿用原 trigger BM25 重放（``agents.skills.rank_corpus_for_query``），
补应触发/不应触发/其他 Skill 竞争与真实加载——routing 变化必须走实际检索→
竞争→加载链；强制注入成绩不能证明触发优化。正文与触发分别通过后才允许组合
重测（接 ``eval/skill_combo_replay``）。

公平对照与兼容：旧局部候选与新策略候选经 ``normalize_candidate`` 归一后使用
**同一执行适配器**；``build_eval_report`` 在关闭新适配时原样返回旧报告兼容字段
（candidate_variants/variant_summaries/best_*/outputs/judgments/trigger/...），
不破坏现有报告消费者。``used`` 标注是召回/使用信号，不自动等于任务成功；
出生题（origin_group=evidence）重答成功只作候选开发证据。
"""
from __future__ import annotations

import inspect
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..online_skill_eval import SideQuery, _generate_variant_response_async
from ..skill_evolution import skill_content_hash
from .contracts import SearchManifest, _canonical_sha256
from .protocol import ORIGIN_EVIDENCE, CaseFreeze

# ── 执行级别与终态口径 ──────────────────────────────────────────────────────

LEVEL_TEXT_ONLY = "text_only"
LEVEL_TOOL_REPLAY = "tool_replay"
LEVEL_ARTIFACT_VERIFIED = "artifact_verified"
EXECUTION_LEVELS = (LEVEL_TEXT_ONLY, LEVEL_TOOL_REPLAY, LEVEL_ARTIFACT_VERIFIED)
_LEVEL_RANK = {LEVEL_TEXT_ONLY: 0, LEVEL_TOOL_REPLAY: 1, LEVEL_ARTIFACT_VERIFIED: 2}

TERMINAL_PASS = "skill_pass"                    # 正常运行且通过冻结 verifier
TERMINAL_SCORED_FAIL = "skill_scored_fail"      # 答错/格式错/工具使用错误：有效失败，保留低分
TERMINAL_INFRA_ERROR = "infra_error"            # provider 429/超时/worker 丢失/环境缺失：无可比分数
TERMINAL_SECURITY_VIOLATION = "security_violation"  # 越权/改只读保护：直接隔离，证据可追踪
TERMINAL_INVALID_LOAD = "invalid_candidate_load"    # 实际加载的不是本候选：评测无效
TERMINAL_EVAL_INVALID = "eval_invalid"          # 冻结 verifier 缺失等评测配置问题：无可比分数

REPORT_SCHEMA_V2 = 2

# 基础设施错误特征（原文/小写匹配）：这些不是技能 0 分，不得计入技能退化。
INFRA_ERROR_MARKERS = (
    "429",
    "rate limit",
    "ratelimit",
    "too many requests",
    "insufficient quota",
    "quota exceeded",
    "timeout",
    "timed out",
    "worker_lost",
    "worker lost",
    "env_missing",
    "environment missing",
    "503",
    "connection error",
    "infra_error",
)


def classify_error_text(text: str) -> str | None:
    """错误文本 → infra_error 特征命中返回标记，否则 None（供适配器与测试共用）。"""
    lowered = str(text or "").lower()
    for marker in INFRA_ERROR_MARKERS:
        if marker in lowered:
            return marker
    return None


def normalize_execution_level(
    requested: str,
    *,
    has_tool_evidence: bool,
    has_artifact_evidence: bool,
    known_tool_defects: list[Any] | None = None,
    defect_fixed_evidence: bool = False,
) -> str:
    """执行级别归一（防虚报）：

    - artifact_verified 必须有产物验证证据（artifact refs + verifier 通过）；
    - 明知有工具缺陷的候选（known_tool_defects 非空）在缺陷无修复证据时，
      级别压到 tool_replay 以下——LLM 重答文本再完美也不能拿到 artifact_verified；
    - 请求级别低于证据级别时按证据归一（不降级已有事实，不升虚报）。
    """
    requested_level = str(requested or LEVEL_TEXT_ONLY)
    level = requested_level if requested_level in EXECUTION_LEVELS else LEVEL_TEXT_ONLY
    if level == LEVEL_ARTIFACT_VERIFIED and not has_artifact_evidence:
        level = LEVEL_TOOL_REPLAY if has_tool_evidence else LEVEL_TEXT_ONLY
    if level == LEVEL_TOOL_REPLAY and not has_tool_evidence:
        level = LEVEL_TEXT_ONLY
    if known_tool_defects and level == LEVEL_ARTIFACT_VERIFIED and not defect_fixed_evidence:
        # 已知工具缺陷无修复证据：产物验证结论不可信，级别封顶 tool_replay。
        level = LEVEL_TOOL_REPLAY if has_tool_evidence else LEVEL_TEXT_ONLY
    return level


# ── 执行结果结构 ────────────────────────────────────────────────────────────


@dataclass
class ExecutionOutcome:
    """一题的唯一终态记录（适配边界的输出契约）。"""

    case_id: str
    candidate_id: str
    terminal_status: str = TERMINAL_EVAL_INVALID
    execution_level: str = LEVEL_TEXT_ONLY
    valid: bool = False
    score: float | None = None
    dimension_scores: dict[str, float] = field(default_factory=dict)
    loaded_hash: str = ""            # 实际加载进运行的技能内容 hash（receipt）
    expected_loaded_hash: str = ""   # 候选声明的内容 hash（不符即 invalid_candidate_load）
    response_ref: str = ""
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    artifact_verified: bool = False
    cost: dict[str, Any] = field(default_factory=dict)
    injection: dict[str, Any] = field(default_factory=dict)
    protocol_fingerprint: str = ""
    evidence: list[str] = field(default_factory=list)
    dev_evidence_only: bool = False   # origin_group=evidence：只作开发证据
    usage_used: bool = False          # 线上 used 标注（信号，不是任务成功）
    task_success: bool = False        # 仅当 artifact_verified 且通过（防 used→成功）
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "candidate_id": self.candidate_id,
            "terminal_status": self.terminal_status,
            "execution_level": self.execution_level,
            "valid": self.valid,
            "score": self.score,
            "dimension_scores": dict(self.dimension_scores),
            "loaded_hash": self.loaded_hash,
            "expected_loaded_hash": self.expected_loaded_hash,
            "response_ref": self.response_ref,
            "tool_events": [dict(e) for e in self.tool_events],
            "artifact_refs": [dict(a) for a in self.artifact_refs],
            "artifact_verified": self.artifact_verified,
            "cost": dict(self.cost),
            "injection": dict(self.injection),
            "protocol_fingerprint": self.protocol_fingerprint,
            "evidence": list(self.evidence),
            "dev_evidence_only": self.dev_evidence_only,
            "usage_used": self.usage_used,
            "task_success": self.task_success,
            "notes": self.notes,
        }


def _verifier_for(case: dict[str, Any], frozen_protocol: dict[str, Any] | None) -> Callable[..., Any] | None:
    """冻结 verifier 查找：Case 内引用优先，其次协议表（两者都没有 → fail-closed）。"""
    verifier = case.get("verifier")
    if verifier is None and isinstance(frozen_protocol, dict):
        verifiers = frozen_protocol.get("verifiers")
        if isinstance(verifiers, dict):
            verifier = verifiers.get(str(case.get("case_id") or ""))
    return verifier if callable(verifier) else None


def _apply_verifier(verifier: Callable[..., Any], response_text: str, case: dict[str, Any]) -> dict[str, Any]:
    result = verifier(response_text, case) if _takes_two_args(verifier) else verifier(response_text)
    return result if isinstance(result, dict) else {"passed": bool(result)}


def _takes_two_args(verifier: Callable[..., Any]) -> bool:
    try:
        return len(inspect.signature(verifier).parameters) >= 2
    except (TypeError, ValueError):
        return False


def _verdict_scores(verdict: dict[str, Any]) -> tuple[float | None, dict[str, float], bool]:
    """verifier 结果 → (总分, 分项, passed)；NaN/Inf/越界分数视为非法（fail-closed）。"""
    dims_raw = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
    dims: dict[str, float] = {}
    for key, value in dims_raw.items():
        try:
            num = float(value)
        except (TypeError, ValueError):
            num = float("nan")
        dims[str(key)] = num
    if verdict.get("score") is not None:
        try:
            overall = float(verdict["score"])
        except (TypeError, ValueError):
            overall = float("nan")
    elif dims:
        valid = [v for v in dims.values() if math.isfinite(v)]
        overall = sum(valid) / len(valid) if valid else float("nan")
    else:
        overall = 1.0 if verdict.get("passed") else 0.0
    passed = bool(verdict.get("passed", overall == 1.0))
    return overall, dims, passed


def _score_is_usable(score: float | None) -> bool:
    return score is not None and math.isfinite(score) and 0.0 <= score <= 1.0


def _finish_verdict(
    outcome: ExecutionOutcome,
    verdict: dict[str, Any],
) -> ExecutionOutcome:
    """把 verifier 结果落到 outcome：非法分数 → eval_invalid（不静默归零）。"""
    score, dims, passed = _verdict_scores(verdict)
    if not _score_is_usable(score) or any(not _score_is_usable(v) for v in dims.values()):
        outcome.terminal_status = TERMINAL_EVAL_INVALID
        outcome.valid = False
        outcome.score = None
        outcome.notes = "verifier returned NaN/Inf/out-of-range score"
        return outcome
    outcome.score = score
    outcome.dimension_scores = dims
    outcome.terminal_status = TERMINAL_PASS if passed else TERMINAL_SCORED_FAIL
    outcome.valid = True
    outcome.task_success = bool(passed and outcome.execution_level == LEVEL_ARTIFACT_VERIFIED)
    return outcome


def _base_outcome(case: dict[str, Any], candidate: dict[str, Any]) -> ExecutionOutcome:
    return ExecutionOutcome(
        case_id=str(case.get("case_id") or ""),
        candidate_id=str(candidate.get("candidate_id") or ""),
        expected_loaded_hash=str(candidate.get("content_hash") or ""),
        dev_evidence_only=str(case.get("origin_group") or "") == ORIGIN_EVIDENCE,
        usage_used=bool(case.get("used")),
    )


# ── 适配器一：旧 side_query 文本重答（text_only 实现，保留不替换） ──────────


class TextOnlySideQueryExecutor:
    """text_only 执行适配器：候选作为 system，side_query 重答原 sample。

    - 复用 ``online_skill_eval._generate_variant_response_async``（旧局部候选同款
      答题路径，公平对照的口径不变）；
    - 永远产不出 artifact_verified：无工具/产物证据，级别固定 text_only；
    - side_query 异步异常（含 429/配额）按 ``infra_error`` 处理，不伪装技能 0 分；
    - 候选须携带 frozen verifier（Case 或协议内），缺失 → eval_invalid。
    """

    adapter_id = "text_only_side_query"
    execution_level = LEVEL_TEXT_ONLY

    def __init__(self, *, side_query: SideQuery | None):
        self.side_query = side_query

    async def execute(
        self,
        *,
        case: dict[str, Any],
        candidate: dict[str, Any],
        frozen_protocol: dict[str, Any] | None = None,
    ) -> ExecutionOutcome:
        outcome = _base_outcome(case, candidate)
        outcome.protocol_fingerprint = str((frozen_protocol or {}).get("fingerprint") or "")
        if self.side_query is None:
            outcome.terminal_status = TERMINAL_INFRA_ERROR
            outcome.notes = "side_query unavailable (no model budget configured)"
            outcome.evidence = ["side_query_none"]
            return outcome
        verifier = _verifier_for(case, frozen_protocol)
        if verifier is None:
            outcome.terminal_status = TERMINAL_EVAL_INVALID
            outcome.notes = "frozen verifier missing for case"
            return outcome
        snapshot = dict(candidate.get("snapshot") or {})
        sample = {
            "sample_id": case.get("case_id", ""),
            "messages": case.get("messages")
            or [{"role": "user", "content": str(case.get("question") or case.get("latest_user") or "")}],
            "latest_user": str(case.get("question") or case.get("latest_user") or ""),
        }
        started = time.time()
        response = await _generate_variant_response_async(
            variant={"variant_id": outcome.candidate_id, "snapshot": snapshot},
            sample=sample,
            side_query=self.side_query,
        )
        wall_ms = int((time.time() - started) * 1000)
        outcome.response_ref = f"side_query://{outcome.candidate_id}/{outcome.case_id}"
        outcome.loaded_hash = skill_content_hash(snapshot)
        outcome.injection = {
            "point": "side_query_system_prompt",
            "hash": outcome.loaded_hash,
            "authorized": True,
        }
        outcome.cost = {"input_tokens": 0, "output_tokens": 0, "wall_ms": wall_ms, "calls": 1}
        if response.startswith("[candidate_response_failed"):
            # 旧实现把异常包进响应文本——这里还原为 infra_error，不许伪装技能 0 分。
            marker = classify_error_text(response) or "side_query_error"
            outcome.terminal_status = TERMINAL_INFRA_ERROR
            outcome.valid = False
            outcome.score = None
            outcome.notes = f"infra failure surfaced from side_query replay (marker={marker})"
            outcome.evidence = [response]
            return outcome
        verdict = _apply_verifier(verifier, response, case)
        # text_only 没有任何工具/产物证据：即使候选明知有工具缺陷、文本再完美，
        # artifact_verified 也不可达（normalize_execution_level 再兜一次底）。
        outcome.execution_level = normalize_execution_level(
            LEVEL_TEXT_ONLY,
            has_tool_evidence=False,
            has_artifact_evidence=False,
            known_tool_defects=list(candidate.get("known_tool_defects") or []),
        )
        outcome.artifact_verified = False
        return _finish_verdict(outcome, verdict)


# ── 适配器二：DE-11 隔离执行通道（工具/产物验证；离线用合成执行器注入） ────


IsolatedRunCallable = Callable[..., Any]


class IsolatedRunExecutor:
    """隔离 Harness 执行适配器（DE-11 Run / 隔离工作区）。

    ``run_executor(candidate, case, frozen_protocol) -> record`` 由调用方注入：
    生产接 DE-11 真实隔离执行服务；离线测试注入合成执行器，不调真实模型。
    record 契约（fail-closed，缺回执判无效）：
    - ``loaded_snapshot``：实际注入运行的技能内容 dict（实际加载 hash 的凭据）；
    - ``response_text`` / ``tool_events``（tool/ok/authorized/event_id）/
      ``artifacts``（path/sha256）/ ``artifact_verdict``（verified/reason）；
    - ``error``：infra 特征（429/timeout/worker_lost/...）→ infra_error；
    - 越权工具事件（authorized=False 或未声明工具）→ security_violation + 隔离；
    - ``execution_level`` / ``defect_fixed``：供级别归一（已知工具缺陷须有修复证据）。
    """

    adapter_id = "isolated_run"

    def __init__(self, *, run_executor: IsolatedRunCallable | None):
        self.run_executor = run_executor

    async def execute(
        self,
        *,
        case: dict[str, Any],
        candidate: dict[str, Any],
        frozen_protocol: dict[str, Any] | None = None,
    ) -> ExecutionOutcome:
        outcome = _base_outcome(case, candidate)
        outcome.protocol_fingerprint = str((frozen_protocol or {}).get("fingerprint") or "")
        verifier = _verifier_for(case, frozen_protocol)
        if verifier is None:
            outcome.terminal_status = TERMINAL_EVAL_INVALID
            outcome.notes = "frozen verifier missing for case"
            return outcome
        if self.run_executor is None:
            outcome.terminal_status = TERMINAL_EVAL_INVALID
            outcome.notes = "isolated run executor not configured"
            return outcome
        try:
            record = self.run_executor(candidate=candidate, case=case, frozen_protocol=frozen_protocol)
            if inspect.isawaitable(record):
                record = await record
        except Exception as exc:  # noqa: BLE001 - 执行器自身崩溃按 infra 处理，不计 0 分
            outcome.terminal_status = TERMINAL_INFRA_ERROR
            outcome.notes = f"isolated executor crashed: {type(exc).__name__}"
            outcome.evidence = [f"{type(exc).__name__}: {exc}"]
            return outcome
        if not isinstance(record, dict):
            outcome.terminal_status = TERMINAL_EVAL_INVALID
            outcome.notes = "isolated run returned no receipt record"
            return outcome

        error_text = str(record.get("error") or "")
        marker = classify_error_text(error_text)
        if marker:
            outcome.terminal_status = TERMINAL_INFRA_ERROR
            outcome.valid = False
            outcome.score = None
            outcome.notes = f"infra failure in isolated run (marker={marker})"
            outcome.evidence = [error_text]
            outcome.cost = dict(record.get("tokens") or {})
            return outcome

        loaded_snapshot = record.get("loaded_snapshot")
        if not isinstance(loaded_snapshot, dict):
            outcome.terminal_status = TERMINAL_EVAL_INVALID
            outcome.notes = "isolated run missing loaded_snapshot receipt (实际加载 hash 无凭据)"
            return outcome
        outcome.loaded_hash = skill_content_hash(loaded_snapshot)
        if outcome.expected_loaded_hash and outcome.loaded_hash != outcome.expected_loaded_hash:
            outcome.terminal_status = TERMINAL_INVALID_LOAD
            outcome.valid = False
            outcome.score = None
            outcome.notes = "isolated run loaded a different artifact than the candidate under test"
            outcome.evidence = [
                f"expected={outcome.expected_loaded_hash}",
                f"loaded={outcome.loaded_hash}",
            ]
            return outcome

        tool_events = [dict(e) for e in (record.get("tool_events") or []) if isinstance(e, dict)]
        artifacts = [dict(a) for a in (record.get("artifacts") or []) if isinstance(a, dict)]
        allowed_tools = {
            part.strip()
            for part in str((candidate.get("snapshot") or {}).get("allowed_tools") or "").split(",")
            if part.strip()
        }
        unauthorized = [
            event
            for event in tool_events
            if event.get("authorized") is False
            or (allowed_tools and str(event.get("tool") or "") not in allowed_tools)
        ]
        if unauthorized:
            # 越权/改只读保护：直接隔离候选，证据（事件 ID/工具名）可追踪。
            outcome.terminal_status = TERMINAL_SECURITY_VIOLATION
            outcome.valid = False
            outcome.score = None
            outcome.execution_level = LEVEL_TOOL_REPLAY if tool_events else LEVEL_TEXT_ONLY
            outcome.tool_events = tool_events
            outcome.notes = "unauthorized tool use detected; candidate quarantined"
            outcome.evidence = [
                f"unauthorized_tool={event.get('tool')} event_id={event.get('event_id')}"
                for event in unauthorized
            ]
            outcome.cost = dict(record.get("tokens") or {})
            return outcome

        artifact_verdict = record.get("artifact_verdict") if isinstance(record.get("artifact_verdict"), dict) else {}
        has_tool_evidence = bool(tool_events) and all(event.get("ok", True) for event in tool_events)
        has_artifact_evidence = bool(artifacts) and bool(artifact_verdict.get("verified"))
        outcome.execution_level = normalize_execution_level(
            str(record.get("execution_level") or (LEVEL_ARTIFACT_VERIFIED if has_artifact_evidence else LEVEL_TOOL_REPLAY if has_tool_evidence else LEVEL_TEXT_ONLY)),
            has_tool_evidence=has_tool_evidence,
            has_artifact_evidence=has_artifact_evidence,
            known_tool_defects=list(candidate.get("known_tool_defects") or []),
            defect_fixed_evidence=bool(record.get("defect_fixed")),
        )
        outcome.artifact_verified = outcome.execution_level == LEVEL_ARTIFACT_VERIFIED
        outcome.response_ref = str(record.get("response_ref") or f"isolated://{outcome.candidate_id}/{outcome.case_id}")
        outcome.tool_events = tool_events
        outcome.artifact_refs = artifacts
        outcome.injection = {
            "point": str(record.get("injection_point") or "isolated_run_explicit_channel"),
            "hash": outcome.loaded_hash,
            "authorized": True,
        }
        outcome.cost = dict(record.get("tokens") or {})
        outcome.evidence = [str(x) for x in (record.get("evidence") or [])]
        if artifact_verdict.get("reason") and not outcome.artifact_verified:
            outcome.notes = f"artifact verification failed: {artifact_verdict.get('reason')}"
            outcome.evidence.append(str(artifact_verdict.get("reason")))
        verdict = _apply_verifier(verifier, str(record.get("response_text") or ""), case)
        return _finish_verdict(outcome, verdict)


# ── 候选归一：旧局部变体与新策略候选同一形态、同一适配器 ────────────────────


def normalize_candidate(candidate: dict[str, Any], *, index: int = 0) -> dict[str, Any]:
    """把旧局部候选（``variant_id``+``snapshot``，online_skill_eval 形态）与搜索候选
    （``candidate_id``+``bundle``/``snapshot``）归一成同一执行输入。"""
    snapshot = candidate.get("snapshot")
    if not isinstance(snapshot, dict):
        bundle = candidate.get("bundle")
        snapshot = bundle if isinstance(bundle, dict) else {}
    candidate_id = str(
        candidate.get("candidate_id")
        or candidate.get("variant_id")
        or f"cand-{index}"
    ).strip()
    known_defects = list(
        candidate.get("known_tool_defects")
        or snapshot.get("known_tool_defects")
        or []
    )
    strategy = str(
        candidate.get("strategy")
        or candidate.get("mutation_type")
        or ("legacy_local" if "variant_id" in candidate else "search")
    )
    return {
        "candidate_id": candidate_id,
        "snapshot": snapshot,
        "content_hash": skill_content_hash(snapshot),
        "known_tool_defects": known_defects,
        "strategy": strategy,
        "parent_ref": str(candidate.get("parent_variant_id") or candidate.get("parent_id") or ""),
    }


# ── 批量执行 + 完整性门（fail-closed，缺题/重复/NaN/旧 rubric/错载 → 无向量） ─


async def evaluate_cases(
    *,
    executor: Any,
    cases: list[dict[str, Any]],
    candidate: dict[str, Any],
    freeze: CaseFreeze | None = None,
    frozen_protocol: dict[str, Any] | None = None,
) -> list[ExecutionOutcome]:
    """按冻结顺序对全部 Case 执行候选；执行器抛错按 infra_error 落账，不吞异常。"""
    protocol_payload = dict(frozen_protocol or {})
    if freeze is not None:
        protocol_payload.setdefault("fingerprint", freeze.fingerprint)
    ordered = cases
    if freeze is not None:
        by_id = {str(c.get("case_id") or ""): c for c in cases}
        ordered = [by_id[cid] for cid in freeze.ordered_case_ids if cid in by_id]
        ordered.extend([c for c in cases if str(c.get("case_id") or "") not in set(freeze.ordered_case_ids)])
    outcomes: list[ExecutionOutcome] = []
    for case in ordered:
        outcomes.append(
            await executor.execute(case=case, candidate=candidate, frozen_protocol=protocol_payload)
        )
    return outcomes


def finalize_score_vector(
    *,
    freeze: CaseFreeze,
    outcomes: list[ExecutionOutcome],
    candidate: dict[str, Any],
    manifest: SearchManifest | None = None,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    """完整性门：产出有效完整分数向量，或整体拒绝（绝不填零、绝不部分入池）。

    覆盖协议：缺题 / 重复 ID / NaN-Inf-越界分数 / 旧 rubric（协议指纹不符）/
    错误候选加载（loaded_hash 不符 / 候选 ID 不符）→ ``ok=False`` 且 ``score_vector=None``；
    infra_error 不产生可比分数（阻塞向量，可按冻结策略重试）；security_violation
    直接隔离候选并保留可追踪证据；dev_evidence_only（出生证据）不入分数向量。
    """
    candidate = normalize_candidate(candidate)
    expected_loaded = str(candidate.get("content_hash") or "")
    if manifest is not None:
        expected_fp = str(manifest.eval_protocol_fingerprint or "")
    else:
        expected_fp = str(expected_fingerprint or freeze.fingerprint)

    by_case: dict[str, list[ExecutionOutcome]] = {}
    for outcome in outcomes:
        by_case.setdefault(outcome.case_id, []).append(outcome)

    blocking: list[dict[str, Any]] = []
    dev_evidence: list[dict[str, Any]] = []
    vector: dict[str, float] = {}
    quarantined = False

    for case_id in freeze.ordered_case_ids:
        found = by_case.get(case_id, [])
        if len(found) > 1:
            blocking.append({"case_id": case_id, "code": "duplicate_case_result", "count": len(found)})
            continue
        if not found:
            blocking.append({"case_id": case_id, "code": "missing_case_result"})
            continue
        outcome = found[0]
        if outcome.candidate_id != candidate["candidate_id"]:
            blocking.append(
                {"case_id": case_id, "code": "candidate_id_mismatch", "got": outcome.candidate_id}
            )
            continue
        if outcome.protocol_fingerprint and outcome.protocol_fingerprint != expected_fp:
            blocking.append(
                {"case_id": case_id, "code": "rubric_protocol_mismatch",
                 "got": outcome.protocol_fingerprint, "expected": expected_fp}
            )
            continue
        if outcome.terminal_status == TERMINAL_INVALID_LOAD or (
            outcome.expected_loaded_hash and outcome.loaded_hash != outcome.expected_loaded_hash
        ):
            blocking.append(
                {"case_id": case_id, "code": "candidate_load_mismatch",
                 "loaded": outcome.loaded_hash, "expected": outcome.expected_loaded_hash}
            )
            continue
        if outcome.terminal_status == TERMINAL_SECURITY_VIOLATION:
            quarantined = True
            blocking.append(
                {"case_id": case_id, "code": "security_violation", "evidence": list(outcome.evidence)}
            )
            continue
        if outcome.terminal_status == TERMINAL_INFRA_ERROR:
            blocking.append({"case_id": case_id, "code": "infra_error", "notes": outcome.notes})
            continue
        if outcome.terminal_status == TERMINAL_EVAL_INVALID:
            blocking.append({"case_id": case_id, "code": "eval_invalid", "notes": outcome.notes})
            continue
        if outcome.dev_evidence_only:
            dev_evidence.append(
                {
                    "case_id": case_id,
                    "passed": outcome.terminal_status == TERMINAL_PASS,
                    "score": outcome.score,
                    "note": "出生题重答：只作候选开发证据，不计入分数向量/线上健康成绩",
                }
            )
            continue
        if not outcome.valid or not _score_is_usable(outcome.score):
            blocking.append(
                {"case_id": case_id, "code": "invalid_score_value", "score": outcome.score,
                 "terminal_status": outcome.terminal_status}
            )
            continue
        vector[case_id] = float(outcome.score)

    if blocking:
        return {
            "ok": False,
            "reason": blocking[0]["code"],
            "blocking": blocking,
            "score_vector": None,
            "candidate_quarantined": quarantined,
            "dev_evidence": dev_evidence,
            "protocol_fingerprint": expected_fp,
            "candidate_id": candidate["candidate_id"],
        }
    if not vector:
        return {
            "ok": False,
            "reason": "score_vector_empty",
            "blocking": [],
            "score_vector": None,
            "candidate_quarantined": False,
            "dev_evidence": dev_evidence,
            "protocol_fingerprint": expected_fp,
            "candidate_id": candidate["candidate_id"],
            "note": "全部 Case 均为出生证据（dev_evidence_only）：只可作开发证据，不产生成绩向量",
        }
    return {
        "ok": True,
        "reason": "",
        "blocking": [],
        "score_vector": vector,
        "average_score": sum(vector.values()) / len(vector),
        "candidate_quarantined": False,
        "dev_evidence": dev_evidence,
        "protocol_fingerprint": expected_fp,
        "candidate_id": candidate["candidate_id"],
        "cache_key": evaluation_cache_key(candidate=candidate, freeze=freeze, manifest=manifest),
    }


def collect_dev_evidence(outcomes: list[ExecutionOutcome]) -> dict[str, Any]:
    """出生题（evidence 组）重答证据单独收集：只做诊断/开发，不进状态机与晋级。"""
    return {
        "dev_evidence_only": True,
        "birth_replays": [
            {
                "case_id": outcome.case_id,
                "passed": outcome.terminal_status == TERMINAL_PASS,
                "score": outcome.score,
                "execution_level": outcome.execution_level,
            }
            for outcome in outcomes
            if outcome.dev_evidence_only
        ],
        "note": "出生题重答成功只作候选开发证据，不能伪装已有在线成绩或晋级依据",
    }


# ── 缓存键：覆盖完整协议（bundle/Case/rubric/verifier/模型/工具环境/seed/scope）


def evaluation_cache_key(
    *,
    candidate: dict[str, Any],
    freeze: CaseFreeze,
    manifest: SearchManifest | None = None,
    models: dict[str, Any] | None = None,
    tool_env: dict[str, Any] | None = None,
    replicate: int = 0,
    seed: int = 0,
    scope: str = "",
) -> str:
    candidate = normalize_candidate(candidate)
    payload = {
        "candidate_content_hash": candidate["content_hash"],
        "bundle_hash": str(getattr(manifest, "bundle_hash", "") or ""),
        "protocol_fingerprint": str(
            getattr(manifest, "eval_protocol_fingerprint", "") or freeze.fingerprint
        ),
        "cases_sha256": freeze.cases_sha256,
        "ordered_case_ids": list(freeze.ordered_case_ids),
        "models": dict(models or {}),
        "tool_env": dict(tool_env or {}),
        "replicate": int(replicate),
        "seed": int(seed),
        "scope": str(scope),
    }
    return "evalcache-" + _canonical_sha256(payload)[:16]


def check_cache_reuse(
    entry: dict[str, Any],
    *,
    key: str,
    manifest: SearchManifest | None = None,
) -> dict[str, Any]:
    """评测复用判定：缓存键必须逐字段一致；有 manifest 时再过 DE-29 复用门
    （bundle/协议绑定，旧评测未绑定或已漂移一律拒绝）。"""
    if not isinstance(entry, dict) or str(entry.get("cache_key") or "") != str(key):
        return {
            "ok": False,
            "error": "cache_key_mismatch",
            "reason": "缓存键不一致（bundle/Case/rubric/模型/环境/seed/scope 任一变化须重测）",
            "entry_key": str((entry or {}).get("cache_key") or ""),
            "expected_key": str(key),
        }
    if manifest is not None:
        return manifest.check_evaluation_reuse(entry)
    return {"ok": True, "reason": ""}


# ── 触发评测：沿用原 BM25 重放 + 正负例 / 竞争 / 真实加载链 ─────────────────


def evaluate_trigger_routing(
    *,
    skill_name: str,
    candidate_snapshot: dict[str, Any],
    baseline_corpus: dict[str, list[str]],
    positive_queries: list[str] | None = None,
    negative_queries: list[str] | None = None,
    competitor_queries: list[dict[str, Any]] | None = None,
    load_limit: int = 3,
    trigger_gates: dict[str, Any] | None = None,
    forced_scores: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """routing 变化必须走实际检索→竞争→加载链（原 BM25 重放语义，零模型成本）。

    - positive：应触发 query——目标 skill 必须真经 ``rank_corpus_for_query`` 排进
      ``load_limit``（真实加载，不是强制注入）；
    - negative：不应触发 query——目标 skill 进 top-load 即误触发；
    - competitor：其他 Skill 的正确触发场景——观察目标是否错误抢加载、期望技能
      是否仍居首（竞争链包含语料中全部真实其他 skill）；
    - ``forced_scores``（注入的 hit@3 等成绩）一律不采信：只作 rejected 标记，
      无真实链路数据时 evaluated=False 且 trigger_improved=False。
    """
    from ..skills import rank_corpus_for_query, skill_terms_from_text

    gates = {
        "min_positive_load_rate": 1.0,
        "max_false_trigger_rate": 0.0,
        "max_competitor_misfire_rate": 0.0,
        **(dict(trigger_gates) if isinstance(trigger_gates, dict) else {}),
    }
    positive = [str(q).strip() for q in (positive_queries or []) if str(q).strip()]
    negative = [str(q).strip() for q in (negative_queries or []) if str(q).strip()]
    competitors = [dict(c) for c in (competitor_queries or []) if isinstance(c, dict)]

    forced_rejected = forced_scores is not None
    if not positive and not negative and not competitors:
        return {
            "evaluated": False,
            "trigger_improved": False,
            "routing_pass": False,
            "forced_injection_rejected": forced_rejected,
            "reason": "forced_injection_cannot_prove_trigger_optimization"
            if forced_rejected
            else "no_routing_queries",
        }

    def _chain_metrics(corpus: dict[str, list[str]]) -> dict[str, Any]:
        pos_rows: list[dict[str, Any]] = []
        for query in positive:
            ranking = [name for name, _ in rank_corpus_for_query(query, corpus)]
            loaded = skill_name in ranking[: max(1, int(load_limit))]
            pos_rows.append(
                {
                    "query": query,
                    "loaded": loaded,
                    "rank": (ranking.index(skill_name) + 1) if skill_name in ranking else len(corpus) + 1,
                    "top": ranking[: max(1, int(load_limit))],
                }
            )
        neg_rows: list[dict[str, Any]] = []
        for query in negative:
            ranking = [name for name, _ in rank_corpus_for_query(query, corpus)]
            neg_rows.append(
                {
                    "query": query,
                    "false_trigger": skill_name in ranking[: max(1, int(load_limit))],
                    "top": ranking[: max(1, int(load_limit))],
                }
            )
        comp_rows: list[dict[str, Any]] = []
        for spec in competitors:
            query = str(spec.get("query") or "").strip()
            expected = str(spec.get("expected") or "").strip()
            ranking = [name for name, _ in rank_corpus_for_query(query, corpus)]
            comp_rows.append(
                {
                    "query": query,
                    "expected_top": expected,
                    "actual_top": ranking[0] if ranking else "",
                    "target_misloaded": skill_name in ranking[: max(1, int(load_limit))],
                }
            )
        pos_rate = (sum(1 for r in pos_rows if r["loaded"]) / len(pos_rows)) if pos_rows else None
        neg_rate = (sum(1 for r in neg_rows if r["false_trigger"]) / len(neg_rows)) if neg_rows else None
        mis_rate = (sum(1 for r in comp_rows if r["target_misloaded"]) / len(comp_rows)) if comp_rows else None
        expected_won_rate = (
            sum(1 for r in comp_rows if r["actual_top"] == r["expected_top"]) / len(comp_rows)
        ) if comp_rows else None
        ranks = [float(r["rank"]) for r in pos_rows] or [float(len(corpus) + 1)]
        return {
            "positive": {"queries": pos_rows, "count": len(pos_rows), "load_rate": pos_rate},
            "negative": {"queries": neg_rows, "count": len(neg_rows), "false_trigger_rate": neg_rate},
            "competitor": {
                "queries": comp_rows,
                "count": len(comp_rows),
                "misfire_rate": mis_rate,
                "expected_won_rate": expected_won_rate,
            },
            "mean_positive_rank": round(sum(ranks) / max(1, len(ranks)), 4),
            "load_chain": "bm25(rank_corpus_for_query)+load_limit",
            "corpus_skill_names": sorted(corpus),
        }

    baseline = _chain_metrics(baseline_corpus)
    variant_corpus = dict(baseline_corpus)
    variant_corpus[skill_name] = skill_terms_from_text(
        candidate_snapshot.get("name", skill_name),
        candidate_snapshot.get("description", ""),
        candidate_snapshot.get("when_to_use", ""),
        candidate_snapshot.get("instructions", ""),
    )
    variant = _chain_metrics(variant_corpus)

    def _rate(value: Any, default: float) -> float:
        return default if value is None else float(value)

    routing_pass = (
        baseline["positive"]["count"] == 0
        or _rate(variant["positive"]["load_rate"], 0.0) >= float(gates["min_positive_load_rate"])
    ) and (
        variant["negative"]["count"] == 0
        or _rate(variant["negative"]["false_trigger_rate"], 0.0) <= float(gates["max_false_trigger_rate"])
    ) and (
        variant["competitor"]["count"] == 0
        or _rate(variant["competitor"]["misfire_rate"], 0.0) <= float(gates["max_competitor_misfire_rate"])
    )
    baseline_load = _rate(baseline["positive"]["load_rate"], 0.0)
    variant_load = _rate(variant["positive"]["load_rate"], 0.0)
    improved_vs_baseline = bool(
        variant_load > baseline_load
        or (
            variant_load == baseline_load
            and variant["mean_positive_rank"] < baseline["mean_positive_rank"]
        )
    )
    return {
        "evaluated": True,
        "skill_name": skill_name,
        "baseline": baseline,
        "variant": variant,
        "improved_vs_baseline": improved_vs_baseline,
        "routing_pass": bool(routing_pass),
        "trigger_improved": bool(routing_pass and improved_vs_baseline),
        "trigger_gates": gates,
        "load_limit": int(load_limit),
        "forced_injection_rejected": forced_rejected,
        "forced_scores_ignored": dict(forced_scores or {}),
        "note": "routing 结论只来自实际检索/竞争/加载链；注入成绩不构成触发优化证据",
    }


# ── 正文 + 触发分别通过后才允许组合重测（接 eval/skill_combo_replay） ───────


def combo_retest_gate(
    *,
    body_pass: bool,
    trigger_pass: bool,
    skills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """正文（case 矩阵）与触发（routing 正负例）分别通过才允许组合重测；
    组合重测复用现有 ``eval.skill_combo_replay.replay``，检测到互斥冲突即阻断——
    多技能分别通过不代表组合无冲突，组合必须重新验收。"""
    if not (body_pass and trigger_pass):
        return {
            "ok": True,
            "combo_allowed": False,
            "combo_required": True,
            "reason": "body_and_trigger_must_both_pass_before_combo_retest",
            "body_pass": bool(body_pass),
            "trigger_pass": bool(trigger_pass),
        }
    replay_result: dict[str, Any] | None = None
    if skills:
        from eval.skill_combo_replay import replay as combo_replay

        replay_result = combo_replay(skills)
        if replay_result.get("hardFailures"):
            return {
                "ok": True,
                "combo_allowed": False,
                "combo_required": True,
                "combo_conflict": True,
                "reason": "combo_conflict_blocked",
                "replay": replay_result,
            }
    return {
        "ok": True,
        "combo_allowed": True,
        "combo_required": True,
        "combo_conflict": False,
        "reason": "",
        "replay": replay_result,
    }


# ── used 标注是使用信号，不自动等于任务成功 ────────────────────────────────


def usage_signal_record(
    *,
    retrieved: bool = False,
    relevant: bool = False,
    used: bool = False,
    outcome: ExecutionOutcome | None = None,
) -> dict[str, Any]:
    """retrieved/relevant/used 是召回与使用信号；任务成功只认
    artifact_verified 且通过冻结 verifier 的执行终态（LLM usage judgment 不是
    工具调用事实，used=True 不构成验收成功）。"""
    task_success = bool(
        outcome is not None
        and outcome.terminal_status == TERMINAL_PASS
        and outcome.execution_level == LEVEL_ARTIFACT_VERIFIED
    )
    return {
        "retrieved": bool(retrieved),
        "relevant": bool(relevant),
        "used": bool(used),
        "task_success": task_success,
        "signal_kind": "recall_usage_signal",
        "note": "used=True 只是使用标注；任务成功需要真实执行/产物验证证据",
    }


# ── 公平对照 + 旧报告兼容 ──────────────────────────────────────────────────

LEGACY_REPORT_FIELDS = (
    "candidate_variants",
    "variant_summaries",
    "best_variant",
    "best_dev_summary",
    "best_test_summary",
    "outputs",
    "judgments",
    "trigger",
    "triggerError",
)

_LEGACY_DEFAULTS: dict[str, Any] = {
    "candidate_variants": [],
    "variant_summaries": [],
    "best_variant": {},
    "best_dev_summary": {},
    "best_test_summary": {},
    "outputs": [],
    "judgments": [],
    "trigger": {},
    "triggerError": "",
}


async def compare_candidates_fair(
    *,
    candidates: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    executor: Any,
    freeze: CaseFreeze,
    manifest: SearchManifest | None = None,
) -> dict[str, Any]:
    """公平对照：旧局部候选与新策略候选用**同一执行适配器**、同一冻结协议、
    同一 Case 顺序与重复口径；适配器身份与协议指纹随报告落盘。"""
    normalized = [normalize_candidate(c, index=i) for i, c in enumerate(candidates)]
    comparisons: list[dict[str, Any]] = []
    all_ok = True
    for cand in normalized:
        outcomes = await evaluate_cases(
            executor=executor, cases=cases, candidate=cand, freeze=freeze
        )
        final = finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=cand, manifest=manifest)
        all_ok = all_ok and bool(final.get("ok"))
        levels: dict[str, int] = {}
        for outcome in outcomes:
            levels[outcome.execution_level] = levels.get(outcome.execution_level, 0) + 1
        comparisons.append(
            {
                "candidate_id": cand["candidate_id"],
                "strategy": cand["strategy"],
                "content_hash": cand["content_hash"],
                "adapter_id": getattr(executor, "adapter_id", ""),
                "execution_levels": levels,
                "completeness": final,
                "average_score": final.get("average_score"),
            }
        )
    return {
        "ok": all_ok,
        "same_executor": True,
        "adapter_id": getattr(executor, "adapter_id", ""),
        "protocol_fingerprint": freeze.fingerprint,
        "ordered_case_ids": list(freeze.ordered_case_ids),
        "comparisons": comparisons,
    }


def build_eval_report(
    *,
    legacy_report: dict[str, Any] | None = None,
    unified: dict[str, Any] | None = None,
    new_adapter_enabled: bool = True,
) -> dict[str, Any]:
    """报告组装：旧字段永远保留（兼容现有报告消费者），新字段版本化追加。

    - ``new_adapter_enabled=False``：返回旧报告全部兼容字段 +
      ``search_extension={"enabled": False}``（行为与关闭前一致）；
    - ``new_adapter_enabled=True``：旧字段仍在，另附 ``search_extension`` v2
      （case_matrix / completeness / 执行级别分布 / 公平对照）。
    """
    legacy = legacy_report if isinstance(legacy_report, dict) else {}
    report: dict[str, Any] = {key: legacy.get(key, _LEGACY_DEFAULTS[key]) for key in LEGACY_REPORT_FIELDS}
    if not new_adapter_enabled:
        report["search_extension"] = {
            "enabled": False,
            "schema_version": 1,
            "reason": "new execution adapter disabled; legacy report fields only",
        }
        return report
    extension: dict[str, Any] = {
        "enabled": True,
        "schema_version": REPORT_SCHEMA_V2,
    }
    if isinstance(unified, dict):
        extension.update(
            {
                "case_matrix": unified.get("case_matrix", []),
                "completeness": unified.get("completeness", {}),
                "execution_levels": unified.get("execution_levels", {}),
                "fair_comparison": unified.get("fair_comparison", {}),
                "trigger_routing": unified.get("trigger_routing", {}),
                "combo_gate": unified.get("combo_gate", {}),
            }
        )
    else:
        extension["note"] = "no unified execution evaluation attached"
    report["search_extension"] = extension
    return report
