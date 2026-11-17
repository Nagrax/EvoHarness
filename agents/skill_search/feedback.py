"""DE-31 证据化逐 Case 归因与反思反馈编译（搜索增强 S1 第三步）。

对应 SKILL-EVOLUTION-SEARCH §6（Feedback 对象）/§8（从轨迹到可验证的归因）与
NATIVE-ARCHITECTURE-INTEGRATION §6（候选提案策略边界）。

反馈是**派生层**（§8：反馈是从 Trace 与判分生成的派生层，不能声称底层原来就有
"反思反馈"字段）：
- 输入是父版本全 S 的轨迹与评分（DE-30 ``ExecutionOutcome`` 记录 + 原 Trace 存储
  的引用），输出是逐 Case 反馈条目 + 结构化分流决策（可 JSON 落盘）；
- 记录生成版本（generation / bundle hash / 协议指纹 / prompt 版本）；
- **不新增第二份原始 Trace 库**：``TraceStore`` 只按引用读取（trace_ref → events），
  长轨迹按引用分块（``chunk_trace``）；反馈记录只保存 trace_ref / event_id /
  sha256 与已消费/未消费状态，不复制轨迹原文。

五类固定归因（§8 口径，不得扩展）：``SKILL_ISSUE / RUBRIC_ISSUE / CASE_ISSUE /
ENVIRONMENT_ISSUE / UNCERTAIN``。只有「证据可定位 + 评分可信 + 责任落在单个可写
Module」的 SKILL_ISSUE 能进入变异门；其余按环境/rubric/Case/澄清分流：
- verified RUBRIC_ISSUE / ENVIRONMENT_ISSUE → 本轮评分/执行不可信 → 整轮不变异
  （修 rubric/环境后更换协议版本重跑基线，不与旧分数混比较）；
- verified CASE_ISSUE → 该 Case 期望本身有问题 → 剔除该 Case 后其余可继续；
- SKILL_ISSUE 子类：``skill_body_defect``（可变异）/ ``knowledge_fact_error``
  （交知识源服务，DE-16/17 语义的引用，不直接写）/ ``stable_preference_conflict``
  （交原 Maintainer ``maintain_online_skill_candidate`` 的引用，不新建线上抽取入口）。

引用核验（§8：只说"证据非空"不足以驱动自动变更；模型 confidence 是排序提示，
不是概率保证，高 confidence 不替代证据支持）：
- 引用必须真实存在（虚构 event ref → ``citation_not_found``）；
- 必须属于同一 Case 的轨迹（跨 Case → ``citation_cross_case``）；
- 必须与结论相关（与 expected/observed/失败原子项零词元重叠 →
  ``citation_irrelevant``；复用 ``experience_standard.grounding_overlap`` 原语，
  不重复实现）；
- 任一引用被拒 → 整条归因失去自动变异资格（引用完整性破坏即降级，fail-closed）；
- 只有真实执行来源（工具/产物/verdict/score record）是"硬证据"；投影事实、
  未经核验 assistant 主张、用户反馈都是软证据，不能单独驱动 SOP 自动变异。

成功 Case 行为保护：全部 S 通过 → 不产生变异候选，只产出 preserve 条目
（保留行为 + 评分引用）；对通过 Case 的 SKILL_ISSUE 归因一律拒绝
（成功 Case 只能界定必须保持的行为，不能改变通过语义）。

变异门停止条件（返回明确 reason，不按 Module 字典序随便选一个）：
零信号 / 低置信 / 最高信号并列 / 单 Case 多责任归因（跨 Module）/ 只读 Module /
ModuleSpec 缺失 / 关键 Trace 分块未读完 / 高 confidence 但证据不足 / 无归因器。
信号公式（§8）：``sum((1-score_i) * confidence_i)``，先过真值、证据、单责任与置信门。

S3 安全：H（封存集）内容不可进入提示或日志——``scan_sealed_content`` /
``assert_no_sealed_content`` 对提示输入与反馈记录全文扫描，命中即抛
``SealedContentLeak``；反思只对 S 开放，V/H 的评分与轨迹不进反馈记录。

归因器离线可注入：``compile_feedback(attributions=[...])`` 接受预生成的归因提案
（真实模型路径：``attribution_inputs_for_search_set`` + ``render_attribution_prompt``
→ ``prompts/attribution.md``；合成测试注入确定性提案，不依赖真实模型调用）。
归因器输出映射原 IR（experience-standard/v1）的证据/决策规则并附 event 引用
（``_build_ir_mapping``，经 ``validate_record`` 校验）。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..experience_standard import (
    grounding_overlap,
    item as ir_item,
    record_from_mapping,
    repair_record,
    validate_record,
)
from .evaluator import (
    TERMINAL_EVAL_INVALID,
    TERMINAL_INFRA_ERROR,
    TERMINAL_INVALID_LOAD,
    TERMINAL_PASS,
    TERMINAL_SCORED_FAIL,
    TERMINAL_SECURITY_VIOLATION,
)
from .modules import ModuleSpec
from .protocol import ROLE_H, ROLE_S, ROLE_V, CaseFreeze

FEEDBACK_SCHEMA_VERSION = 1
ATTRIBUTION_PROMPT_NAME = "attribution.md"
ATTRIBUTION_PROMPT_VERSION = 1

# ── 五类固定归因（SKILL-EVOLUTION-SEARCH §8，不得扩展） ─────────────────────

ATTR_SKILL_ISSUE = "SKILL_ISSUE"
ATTR_RUBRIC_ISSUE = "RUBRIC_ISSUE"
ATTR_CASE_ISSUE = "CASE_ISSUE"
ATTR_ENVIRONMENT_ISSUE = "ENVIRONMENT_ISSUE"
ATTR_UNCERTAIN = "UNCERTAIN"
ATTRIBUTION_CATEGORIES = (
    ATTR_SKILL_ISSUE,
    ATTR_RUBRIC_ISSUE,
    ATTR_CASE_ISSUE,
    ATTR_ENVIRONMENT_ISSUE,
    ATTR_UNCERTAIN,
)

# SKILL_ISSUE 子类 → 交谁（引用语义：只引用目标服务/入口，不直接写）
SKILL_SUB_BODY_DEFECT = "skill_body_defect"                   # 唯一可自动变异的子类
SKILL_SUB_KNOWLEDGE_FACT = "knowledge_fact_error"             # 交知识源服务（DE-16/17）
SKILL_SUB_PREFERENCE_CONFLICT = "stable_preference_conflict"  # 交原 Maintainer
SKILL_SUB_KINDS = (SKILL_SUB_BODY_DEFECT, SKILL_SUB_KNOWLEDGE_FACT, SKILL_SUB_PREFERENCE_CONFLICT)

# 证据来源类别（event.source；来源纪律：NATIVE §6 不把任意消息当事实）
SOURCE_EXECUTION = "execution"              # 真实执行事件（工具/产物/verdict/score）
SOURCE_USER_FEEDBACK = "user_feedback"      # 用户反馈（相关性必须核验）
SOURCE_ASSISTANT_CLAIM = "assistant_claim"  # assistant 主张（默认未经核验）
SOURCE_PROJECTION_FACT = "projection_fact"  # 知识投影事实（可重建，不由搜索器写）
SOURCE_BY_KIND = {
    "tool_call": SOURCE_EXECUTION,
    "tool_event": SOURCE_EXECUTION,
    "artifact": SOURCE_EXECUTION,
    "verdict": SOURCE_EXECUTION,
    "score_record": SOURCE_EXECUTION,
    "user_message": SOURCE_USER_FEEDBACK,
    "assistant_message": SOURCE_ASSISTANT_CLAIM,
    "projection_note": SOURCE_PROJECTION_FACT,
}

# 路由目标（分流决策落盘字段；全部是"引用"，不含直接写动作）
ROUTE_ENVIRONMENT_REPAIR = "environment_repair"    # DE 技术台账 / 环境修复
ROUTE_RUBRIC_REVISION = "rubric_revision"          # 换协议版本重跑基线
ROUTE_CASE_REVISION = "case_revision"              # Case 修订
ROUTE_MANUAL_CLARIFICATION = "manual_clarification"  # 人工澄清
ROUTE_KNOWLEDGE_SOURCE_SERVICE = "knowledge_source_service"
ROUTE_ONLINE_MAINTAINER = "online_maintainer"
ROUTE_SECURITY_QUARANTINE = "security_quarantine"

KNOWLEDGE_SOURCE_SERVICE_REF = {
    "service": "evoharness.knowledge.service",
    "semantics": "DE-16/17 权威来源与版本化知识检索 / 增量对账",
    "write": False,
    "note": "知识事实错误交源服务修订：只附 event 证据引用，不由搜索器直接写知识",
}
ONLINE_MAINTAINER_REF = {
    "entry": "agents.online_skill_evolution.maintain_online_skill_candidate",
    "semantics": "原在线 Maintainer（add/merge/revise/discard 裁决）",
    "write": False,
    "note": "稳定偏好冲突交原 Maintainer 裁决；不新建线上抽取入口",
}

DEFAULT_MIN_CONFIDENCE = 0.7

# ── 引用核验拒绝码 / 变异门停止原因码（登记词表，测试只引用登记内码） ────────

REJECT_CATEGORY_UNKNOWN = "category_unknown"
REJECT_CONFIDENCE_INVALID = "confidence_invalid"
REJECT_CITATION_NOT_FOUND = "citation_not_found"        # 虚构 event ref
REJECT_CITATION_CROSS_CASE = "citation_cross_case"      # 跨 Case 证据
REJECT_CITATION_IRRELEVANT = "citation_irrelevant"      # 与结论无关引用
REJECT_SUCCESS_CASE = "success_case_cannot_drive_mutation"
REJECT_CASE_NOT_SCORED = "case_not_scored"

STOP_ALL_PASSED = "all_cases_passed_preserve_behavior"
STOP_RUBRIC_ISSUE = "rubric_issue_no_mutation"
STOP_ENVIRONMENT_ISSUE = "environment_issue_no_mutation"
STOP_ZERO_SIGNAL = "zero_signal"
STOP_LOW_CONFIDENCE = "low_confidence"
STOP_TIED_TOP_SIGNAL = "tied_top_signal"
STOP_MULTI_MODULE = "multi_module_responsibility"
STOP_READONLY_MODULE = "readonly_module_protected"
STOP_HIGH_CONF_NO_EVIDENCE = "high_confidence_insufficient_evidence"
STOP_UNCONSUMED_EVIDENCE = "key_evidence_unconsumed"
STOP_NO_HARD_EVIDENCE = "soft_evidence_only"
STOP_PROJECTION_SOURCE = "projection_fact_not_sop_evidence"
STOP_UNVERIFIED_CLAIM = "assistant_claim_unverified"
STOP_NO_ATTRIBUTOR = "no_attributor_configured"
STOP_NO_SCORED_FAILURE = "no_scored_failure_no_mutation"
STOP_UNCERTAIN_ONLY = "uncertain_attribution_manual_clarification"
STOP_MODULE_SPEC_MISSING = "module_spec_missing"
STOP_MODULE_UNASSIGNED = "module_unassigned"
STOP_MODULE_NOT_FOUND = "module_not_found"
STOP_DUPLICATE_PROPOSAL = "duplicate_attribution_suppressed"
STOP_MUTATOR_FAILED = "mutator_failed"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ── Trace 引用读取边界：长轨迹按引用分块，不复制原文 ────────────────────────


@dataclass
class TraceChunk:
    """一个轨迹分块：按引用定位（``trace_ref#chunk:<index>``），记录事件清单。"""

    trace_ref: str
    index: int
    count: int
    event_ids: tuple[str, ...]
    text: str
    sha256: str

    @property
    def ref(self) -> str:
        return f"{self.trace_ref}#chunk:{self.index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "trace_ref": self.trace_ref,
            "index": self.index,
            "count": self.count,
            "event_ids": list(self.event_ids),
            "sha256": self.sha256,
            "chars": len(self.text),
        }


def chunk_trace(
    trace_ref: str, events: list[dict[str, Any]], *, max_chars: int = 1200
) -> list[TraceChunk]:
    """确定性分块：事件按存储顺序整条装箱（不截断单条事件），超长即开新块。"""
    limit = max(1, int(max_chars))
    chunks: list[TraceChunk] = []
    ids: list[str] = []
    texts: list[str] = []
    size = 0

    def _flush() -> None:
        text = "\n".join(texts)
        chunks.append(
            TraceChunk(
                trace_ref=trace_ref,
                index=len(chunks),
                count=0,
                event_ids=tuple(ids),
                text=text,
                sha256=_sha256_text(text),
            )
        )

    for event in events:
        line = f"{event.get('event_id')} {event.get('text') or ''}"
        if ids and size + len(line) + 1 > limit:
            _flush()
            ids, texts, size = [], [], 0
        ids.append(str(event.get("event_id") or ""))
        texts.append(line)
        size += len(line) + 1
    if ids:
        _flush()
    if not chunks:
        chunks.append(TraceChunk(trace_ref=trace_ref, index=0, count=1, event_ids=(), text="", sha256=_sha256_text("")))
    for chunk in chunks:
        chunk.count = len(chunks)
    return chunks


def _normalize_event(event: dict[str, Any], index: int) -> dict[str, Any]:
    event = dict(event if isinstance(event, dict) else {})
    event.setdefault("event_id", f"e{index}")
    event["event_id"] = str(event["event_id"])
    event["text"] = str(event.get("text") or "")
    kind = str(event.get("kind") or "")
    if not str(event.get("source") or "").strip():
        event["source"] = SOURCE_BY_KIND.get(kind, SOURCE_EXECUTION if kind else "")
    return event


class TraceStore:
    """原轨迹存储的**引用读取边界**：反馈只保存 trace_ref/event_id/sha256，
    不复制轨迹原文（不新增第二份原始 Trace 库）。

    生产接现有 Artifact/Trace 存储（按 ref 定位）；离线测试注入内存映射。
    """

    def __init__(self, traces: dict[str, list[dict[str, Any]]] | None = None):
        self._traces: dict[str, list[dict[str, Any]]] = {}
        for ref, events in (traces or {}).items():
            self._traces[str(ref)] = [
                _normalize_event(e, i) for i, e in enumerate(events or [])
            ]
        self._chunk_cache: dict[tuple[str, int], list[TraceChunk]] = {}

    def add(self, trace_ref: str, events: list[dict[str, Any]]) -> None:
        self._traces[str(trace_ref)] = [_normalize_event(e, i) for i, e in enumerate(events or [])]
        self._chunk_cache.pop((str(trace_ref), -1), None)

    def resolve(self, trace_ref: str) -> list[dict[str, Any]] | None:
        return self._traces.get(str(trace_ref))

    def event(self, trace_ref: str, event_id: str) -> dict[str, Any] | None:
        for event in self._traces.get(str(trace_ref), []) or []:
            if event.get("event_id") == str(event_id):
                return event
        return None

    def chunks(self, trace_ref: str, *, max_chars: int = 1200) -> list[TraceChunk]:
        key = (str(trace_ref), int(max_chars))
        if key not in self._chunk_cache:
            events = self._traces.get(str(trace_ref)) or []
            self._chunk_cache[key] = chunk_trace(str(trace_ref), events, max_chars=max_chars)
        return self._chunk_cache[key]


# ── S3 安全：封存集（H）内容不得进入提示或日志 ──────────────────────────────


class SealedContentLeak(RuntimeError):
    """H（封存集）内容出现在提示输入或日志/记录中：fail-closed，立即中断。"""

    def __init__(self, matches: list[str], context: str = ""):
        self.matches = list(matches)
        self.context = context
        super().__init__(
            f"sealed_holdout_content_leaked:{','.join(self.matches)}" + (f" ({context})" if context else "")
        )


def scan_sealed_content(text: str, sealed_samples: list[str]) -> list[str]:
    """返回出现在 text 中的封存样本（按样本清单顺序去重）；短样本（<8 字符）跳过防误报。"""
    body = str(text or "")
    hits: list[str] = []
    for sample in sealed_samples or []:
        token = str(sample or "").strip()
        if len(token) < 8:
            continue
        if token in body and token not in hits:
            hits.append(token)
    return hits


def assert_no_sealed_content(text: str, sealed_samples: list[str], *, context: str = "") -> None:
    matches = scan_sealed_content(text, sealed_samples)
    if matches:
        raise SealedContentLeak(matches, context=context)


# ── 归因提案规范化与引用核验 ────────────────────────────────────────────────


def _confidence(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or not 0.0 <= num <= 1.0:
        return None
    return num


def _normalize_citation(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        return {"event_ref": raw}
    return dict(raw) if isinstance(raw, dict) else {}


def _normalize_hypotheses(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    raw = proposal.get("module_hypotheses")
    hyps: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            module_id = str(item.get("module_id") or "").strip()
            if not module_id:
                continue
            conf = _confidence(item.get("confidence"))
            hyps.append({"module_id": module_id, "confidence": conf})
    if not hyps:
        module_id = str(proposal.get("module_id") or "").strip()
        if module_id:
            hyps.append({"module_id": module_id, "confidence": _confidence(proposal.get("confidence"))})
    return hyps


def normalize_proposal(raw: dict[str, Any]) -> dict[str, Any]:
    """归一化一条归因提案（深拷贝，不改输入；缺字段填默认，不猜语义）。"""
    proposal = dict(raw if isinstance(raw, dict) else {})
    proposal["case_id"] = str(proposal.get("case_id") or "").strip()
    proposal["category"] = str(proposal.get("category") or "").strip().upper()
    sub = str(proposal.get("sub_kind") or "").strip().lower()
    if not sub and proposal["category"] == ATTR_SKILL_ISSUE:
        sub = SKILL_SUB_BODY_DEFECT
    proposal["sub_kind"] = sub
    proposal["confidence"] = _confidence(proposal.get("confidence"))
    proposal["citations"] = [_normalize_citation(c) for c in (proposal.get("citations") or [])]
    proposal["module_hypotheses"] = _normalize_hypotheses(proposal)
    proposal["observed"] = str(proposal.get("observed") or "")
    proposal["expected"] = str(proposal.get("expected") or "")
    proposal["failure_atoms"] = [str(a) for a in (proposal.get("failure_atoms") or []) if str(a).strip()]
    proposal["unknowns"] = [str(u) for u in (proposal.get("unknowns") or []) if str(u).strip()]
    proposal["preserve"] = str(proposal.get("preserve") or "")
    return proposal


def _claim_text(proposal: dict[str, Any]) -> str:
    parts = [proposal.get("observed") or "", proposal.get("expected") or ""]
    parts.extend(proposal.get("failure_atoms") or [])
    return " ".join(str(p) for p in parts if p)


def _split_event_ref(event_ref: str) -> tuple[str, str] | None:
    text = str(event_ref or "").strip()
    if "#" not in text:
        return None
    trace_ref, _, event_id = text.rpartition("#")
    trace_ref = trace_ref.strip()
    event_id = event_id.strip()
    if not trace_ref or not event_id or event_id.startswith("chunk:"):
        return None
    return trace_ref, event_id


def _verify_citations(
    proposal: dict[str, Any],
    *,
    case_id: str,
    trace_ref: str,
    evaluation: dict[str, Any],
    parent_candidate_id: str,
    trace_store: TraceStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """逐条核验引用。返回 (有效引用, 被拒引用, 是否存在硬执行证据)。

    - event 引用：必须能在本 Case 的轨迹里解析到真实事件（虚构 → not_found）、
      trace_ref 必须就是本 Case 的轨迹（跨 Case → cross_case）、事件文本必须与
      结论共享词元（零重叠 → irrelevant，复用 experience_standard 原语）；
    - score_record 引用：必须匹配本 Case 本候选的真实评分记录；
    - 任一引用被拒 → 整条归因失去自动变异资格（由调用方执行降级）。
    """
    claim = _claim_text(proposal)
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    def _reject(citation: dict[str, Any], reason: str, detail: str = "") -> None:
        entry = {k: v for k, v in citation.items()}
        entry["reason"] = reason
        if detail:
            entry["detail"] = detail
        rejected.append(entry)

    for citation in proposal.get("citations") or []:
        if str(citation.get("type") or "") == "score_record":
            cited_case = str(citation.get("case_id") or "").strip()
            if cited_case != case_id:
                _reject(citation, REJECT_CITATION_CROSS_CASE, f"score record 属于 Case {cited_case or '?'}，不属于本 Case")
                continue
            eval_candidate = str(evaluation.get("candidate_id") or "")
            cited_candidate = str(citation.get("candidate_id") or parent_candidate_id)
            if eval_candidate and cited_candidate and eval_candidate != cited_candidate:
                _reject(citation, REJECT_CITATION_CROSS_CASE, "score record 属于其他候选的评测")
                continue
            valid.append(
                {
                    **citation,
                    "source": SOURCE_EXECUTION,
                    "hard_evidence": True,
                    "sha256": _sha256_text(json.dumps(_score_record_payload(evaluation), ensure_ascii=False, sort_keys=True)),
                }
            )
            continue
        parts = _split_event_ref(citation.get("event_ref") or "")
        if parts is None:
            _reject(citation, REJECT_CITATION_NOT_FOUND, "event_ref 缺少 trace_ref#event_id 结构")
            continue
        cited_trace, cited_event = parts
        if cited_trace != trace_ref:
            _reject(citation, REJECT_CITATION_CROSS_CASE, f"引用轨迹 {cited_trace} 不属于本 Case（{trace_ref}）")
            continue
        event = trace_store.event(cited_trace, cited_event)
        if event is None:
            _reject(citation, REJECT_CITATION_NOT_FOUND, f"事件 {cited_event} 不存在于轨迹 {cited_trace}")
            continue
        if grounding_overlap(event.get("text") or "", claim) <= 0:
            _reject(citation, REJECT_CITATION_IRRELEVANT, "引用事件与归因结论无共享词元（不相关）")
            continue
        source = str(event.get("source") or "")
        valid.append(
            {
                **citation,
                "event_ref": f"{cited_trace}#{cited_event}",
                "source": source,
                "verified": bool(event.get("verified")),
                "hard_evidence": source == SOURCE_EXECUTION,
                "sha256": _sha256_text(event.get("text") or ""),
            }
        )
    hard = any(c.get("hard_evidence") for c in valid)
    return valid, rejected, hard


def _score_record_payload(evaluation: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": str(evaluation.get("case_id") or ""),
        "candidate_id": str(evaluation.get("candidate_id") or ""),
        "terminal_status": str(evaluation.get("terminal_status") or ""),
        "score": evaluation.get("score"),
        "dimension_scores": dict(evaluation.get("dimension_scores") or {}),
    }


def _soft_evidence_code(valid_citations: list[dict[str, Any]]) -> str:
    """无硬执行证据时的具体原因（来源纪律分流）。"""
    sources = {str(c.get("source") or "") for c in valid_citations}
    if sources and sources <= {SOURCE_PROJECTION_FACT}:
        return STOP_PROJECTION_SOURCE
    if sources and sources <= {SOURCE_ASSISTANT_CLAIM}:
        unverified = all(not c.get("verified") for c in valid_citations)
        if unverified:
            return STOP_UNVERIFIED_CLAIM
    return STOP_NO_HARD_EVIDENCE


def verify_attribution(
    proposal: dict[str, Any],
    *,
    case: dict[str, Any],
    evaluation: dict[str, Any],
    parent_candidate_id: str,
    trace_store: TraceStore,
    trace_ref: str,
) -> dict[str, Any]:
    """核验一条归因提案：类别合法、引用真实/同 Case/相关、来源纪律。

    返回 ``{"ok": True, "item": {...}}``（item 含 auto_mutate 与 blockers）或
    ``{"ok": False, "reason": ...}``（提案本身不合法，如 Case 不匹配）。
    """
    proposal = normalize_proposal(proposal)
    case_id = str(case.get("case_id") or "")
    if proposal["case_id"] != case_id:
        return {"ok": False, "reason": REJECT_CASE_NOT_SCORED, "detail": f"提案 case_id={proposal['case_id']} 与目标 {case_id} 不符"}
    category = proposal["category"]
    if category not in ATTRIBUTION_CATEGORIES:
        return {"ok": False, "reason": REJECT_CATEGORY_UNKNOWN, "detail": f"未知归因类别: {proposal['category'] or 'empty'}"}

    terminal = str(evaluation.get("terminal_status") or "")
    blockers: list[dict[str, Any]] = []
    auto_mutate = False
    if category == ATTR_SKILL_ISSUE:
        if terminal == TERMINAL_PASS:
            return {
                "ok": False,
                "reason": REJECT_SUCCESS_CASE,
                "detail": "成功 Case 只产出行为保持（preserve）条目，不能驱动改写（不能改变通过语义）",
            }
        if terminal not in (TERMINAL_SCORED_FAIL,):
            return {"ok": False, "reason": REJECT_CASE_NOT_SCORED, "detail": f"终态 {terminal} 无可比分数，归因交确定性分流"}
        if proposal["confidence"] is None:
            blockers.append({"code": REJECT_CONFIDENCE_INVALID, "detail": "confidence 缺失/越界（[0,1]），拒绝自动变异"})
        elif proposal["confidence"] == 0.0 and not proposal["citations"]:
            blockers.append({"code": REJECT_CONFIDENCE_INVALID, "detail": "confidence=0 且无引用，拒绝自动变异"})

    valid, rejected, hard = _verify_citations(
        proposal,
        case_id=case_id,
        trace_ref=trace_ref,
        evaluation=evaluation,
        parent_candidate_id=parent_candidate_id,
        trace_store=trace_store,
    )
    if category == ATTR_SKILL_ISSUE and rejected:
        # 引用完整性破坏（虚构/跨 Case/无关）→ 整条降级，fail-closed。
        blockers.append({"code": rejected[0]["reason"], "detail": rejected[0].get("detail", ""), "citations": [c.get("event_ref") or c.get("case_id") for c in rejected]})
    if category == ATTR_SKILL_ISSUE and not valid:
        blockers.append({"code": STOP_HIGH_CONF_NO_EVIDENCE, "detail": "无任何有效引用：高 confidence 不替代证据支持"})
    elif category == ATTR_SKILL_ISSUE and valid and not hard:
        blockers.append({"code": _soft_evidence_code(valid), "detail": "引用全部为软证据（投影事实/未核验主张/用户反馈），不能单独驱动 SOP 自动变异"})

    auto_mutate = category == ATTR_SKILL_ISSUE and not blockers
    item = {
        "case_id": case_id,
        "category": category,
        "sub_kind": proposal["sub_kind"],
        "module_hypotheses": [dict(h) for h in proposal["module_hypotheses"]],
        "citations": valid,
        "rejected_citations": rejected,
        "hard_evidence": hard,
        "expected": proposal["expected"],
        "observed": proposal["observed"],
        "failure_atoms": list(proposal["failure_atoms"]),
        "preserve": proposal["preserve"],
        "confidence": proposal["confidence"] if proposal["confidence"] is not None else 0.0,
        "unknowns": list(proposal["unknowns"]),
        "auto_mutate": auto_mutate,
        "blockers": blockers,
    }
    return {"ok": True, "item": item}


# ── 归因提示词（设计目标；真实模型路径，合成测试不依赖） ────────────────────

_FALLBACK_PROMPT = """你是 EvoHarness 搜索增强的逐 Case 归因器。只输出严格 JSON。
纪律：引用必须指向真实 event/score 记录且支持结论；confidence 是排序提示不是概率保证；
关键 Trace 未读全不得给高置信；V/H 资料不可见；不得把题目/答案写入建议正文。
"""


def _attribution_template() -> str:
    path = Path(__file__).resolve().parent / "prompts" / ATTRIBUTION_PROMPT_NAME
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            pass
    return _FALLBACK_PROMPT


def build_attribution_input(
    *,
    case: dict[str, Any],
    evaluation: dict[str, Any],
    trace_store: TraceStore,
    consumed_chunk_refs: list[str] | None = None,
    max_chars: int = 1200,
    module_spec: ModuleSpec | None = None,
) -> dict[str, Any]:
    """构建单 Case 归因输入（S-only 反思边界）。

    - Case 角色必须是 S（反思只对搜索集开放；H → ``SealedContentLeak``，V → ValueError）；
    - 长轨迹按引用分块；``consumed_chunk_refs=None`` 视为全读（短轨迹常态），
      显式给清单时未列出的分块即未消费（关键证据状态可见）。
    """
    case_id = str(case.get("case_id") or "")
    role = str(case.get("protocol_role") or ROLE_S)
    if role == ROLE_H:
        raise SealedContentLeak([f"case:{case_id}"], context="build_attribution_input(H case)")
    if role != ROLE_S:
        raise ValueError(f"case_role_not_in_search_set:{case_id}:{role}")
    trace_ref = str(evaluation.get("trace_ref") or "")
    chunks = trace_store.chunks(trace_ref, max_chars=max_chars) if trace_ref else []
    if consumed_chunk_refs is None:
        consumed = [c.ref for c in chunks]
    else:
        listed = {str(r) for r in consumed_chunk_refs}
        consumed = [c.ref for c in chunks if c.ref in listed]
    unconsumed = [c.ref for c in chunks if c.ref not in set(consumed)]
    modules = []
    if module_spec is not None:
        modules = [
            {"module_id": m.module_id, "type": m.type, "writable": bool(m.writable)}
            for m in sorted(module_spec.modules, key=lambda m: m.module_id)
        ]
    return {
        "case_id": case_id,
        "protocol_role": role,
        "trace_ref": trace_ref,
        "question": str(case.get("question") or ""),
        "expected": str(case.get("expected") or ""),
        "score_record": _score_record_payload(evaluation),
        "chunks": [c.to_dict() for c in chunks],
        "consumed_chunk_refs": consumed,
        "unconsumed_chunk_refs": unconsumed,
        "modules": modules,
        "sealed_content_guard": True,
    }


def render_attribution_prompt(attribution_input: dict[str, Any], *, template: str | None = None) -> str:
    """渲染归因提示词：模板（prompts/attribution.md，设计目标）+ 输入 JSON 附录。"""
    body = template if template is not None else _attribution_template()
    appendix = json.dumps(attribution_input, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{body}\n\n<!-- attribution_input (S-only) -->\n{appendix}"


def attribution_inputs_for_search_set(
    *,
    cases: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    s_case_ids: list[str],
    trace_store: TraceStore,
    consumed_chunks: dict[str, list[str]] | None = None,
    module_spec: ModuleSpec | None = None,
    max_chars: int = 1200,
) -> list[dict[str, Any]]:
    """为全 S 的有效失败 Case 构建归因输入（V/H 不进反思；§4 反思资料口径）。"""
    by_case = {str(c.get("case_id") or ""): c for c in cases}
    evals = {str(e.get("case_id") or ""): e for e in evaluations}
    inputs: list[dict[str, Any]] = []
    for case_id in s_case_ids:
        evaluation = evals.get(case_id)
        case = by_case.get(case_id)
        if evaluation is None or case is None:
            continue
        if str(evaluation.get("terminal_status") or "") != TERMINAL_SCORED_FAIL:
            continue  # 只对有效失败编译归因输入；通过/infra/无效走确定性分流
        inputs.append(
            build_attribution_input(
                case=case,
                evaluation=evaluation,
                trace_store=trace_store,
                consumed_chunk_refs=(consumed_chunks or {}).get(case_id),
                max_chars=max_chars,
                module_spec=module_spec,
            )
        )
    return inputs


# ── 变异门配置 ──────────────────────────────────────────────────────────────


@dataclass
class AttributionGates:
    """变异门阈值（置信阈值须在开发集标定并冻结；此处为待试点标定的配置）。"""

    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    min_signal: float = 1e-9          # 信号必须 > 0（零信号 → 停止）
    signal_epsilon: float = 1e-9      # 最高信号并列判定容差（§10 同款 epsilon 口径）
    require_full_trace: bool = True   # 关键 Trace 没读全 → 不得给高置信自动归因
    max_chunk_chars: int = 1200


# ── 反馈编译主流程 ──────────────────────────────────────────────────────────


_TRIAGE_BY_TERMINAL = {
    TERMINAL_INFRA_ERROR: ("environment", ROUTE_ENVIRONMENT_REPAIR),
    TERMINAL_EVAL_INVALID: ("eval_protocol", ROUTE_MANUAL_CLARIFICATION),
    TERMINAL_INVALID_LOAD: ("eval_protocol", ROUTE_MANUAL_CLARIFICATION),
    TERMINAL_SECURITY_VIOLATION: ("security", ROUTE_SECURITY_QUARANTINE),
}


def _evaluation_ref(parent_candidate_id: str, case_id: str, fingerprint: str) -> str:
    return f"evaluation://{parent_candidate_id}/{case_id}@{str(fingerprint)[:12]}"


def _verdict_event_ref(trace_store: TraceStore, trace_ref: str) -> str:
    """通过 Case 的 verdict 事件引用（保留行为证据；无轨迹则空）。"""
    if not trace_ref:
        return ""
    for event in trace_store.resolve(trace_ref) or []:
        if str(event.get("kind") or "") == "verdict":
            return f"{trace_ref}#{event.get('event_id')}"
    return ""


def _build_ir_mapping(
    item: dict[str, Any], case_question: str, parent_candidate_id: str
) -> dict[str, Any] | None:
    """归因输出映射原 IR（experience-standard/v1）的证据/决策规则并附 event 引用。

    经 ``validate_record`` 校验；校验不过则返回带 ``validation_errors`` 的占位
    （不静默吞错，也不把坏记录冒充合法 IR）。
    """
    observed = str(item.get("observed") or "")
    expected = str(item.get("expected") or "")
    atoms = [str(a) for a in (item.get("failure_atoms") or [])]
    refs = [str(c.get("event_ref") or "") for c in (item.get("citations") or []) if c.get("event_ref")]
    refs = [r for r in refs if r]
    conf = float(item.get("confidence") or 0.0)
    summary = observed or "；".join(atoms)
    if not summary or not refs:
        return None
    raw_text = str(case_question or f"case:{item.get('case_id')}")
    observations = [
        {**ir_item(f"执行证据引用 {ref}（sha256={_sha256_text(ref)[:12]}）", "explicit", conf), "event_refs": [ref]}
        for ref in refs
    ]
    mapping = {
        "identity": {
            "title": f"skill-search 归因：{item.get('case_id')}（{parent_candidate_id}）",
            "summary": summary[:200],
            "primary_type": "failure_pattern",
        },
        "situation": {"goal": [ir_item(raw_text, "explicit", 1.0)]},
        "outcome": {
            "failure_signals": [ir_item(summary, "explicit", conf)],
            "expected": ([ir_item(expected, "explicit", conf)] if expected else []),
        },
        "evidence": {"observations": observations, "confidence": conf},
        "guidance": {
            "decision_rules": [
                {
                    "when": [ir_item(summary, "explicit", conf)],
                    "then": [ir_item(expected or "修订对应 Module 后全 S 回归验证", "inferred", 0.5)],
                    "because": [ir_item(f"event 证据 {refs[0]}", "explicit", conf)],
                }
            ]
        },
        "indexing": {"tags": ["skill-search", "feedback", "DE-31"]},
    }
    record = record_from_mapping(mapping)
    record["source"]["raw_text"] = raw_text
    repaired, _applied = repair_record(record)
    errors = validate_record(repaired, raw_text)
    return {"schema_version": "experience-standard/v1", "record": repaired, "validation_errors": errors}


def compile_feedback(
    *,
    parent: dict[str, Any],
    freeze: CaseFreeze,
    cases: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    trace_store: TraceStore,
    attributions: list[dict[str, Any]] | None = None,
    module_spec: ModuleSpec | None = None,
    manifest: Any = None,
    trace_refs: dict[str, str] | None = None,
    consumed_chunks: dict[str, list[str]] | None = None,
    gates: AttributionGates | None = None,
    mutator: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """从父版本全 S 轨迹与评分编译反思反馈 + 结构化分流决策。

    - ``parent``：``{"candidate_id", "generation", ...}``（记录生成版本，必填）；
    - ``manifest``（可选，DE-29 SearchManifest）：提供 bundle_hash / skill_content_hash；
    - ``attributions``：注入的归因提案（真实模型或离线合成）；为空且存在有效失败
      → ``no_attributor_configured``，不自动变异（fail-closed）；
    - ``mutator``：变异器回调（DE-32 契约：输入候选编辑简报）。**只在变异门放行时
      被调用一次**；全成功/环境/rubric/证据不足等一律不调用；
    - 返回可 JSON 落盘的反馈记录（引用原轨迹，不复制原文；H 内容经扫描防泄漏）。
    """
    resolved_gates = gates or AttributionGates()
    candidate_id = str(parent.get("candidate_id") or "").strip()
    if not candidate_id:
        raise ValueError("parent_candidate_id_missing")
    raw_generation = parent.get("generation")
    try:
        generation = int(raw_generation)
    except (TypeError, ValueError):
        raise ValueError("parent_generation_missing") from None

    role_of = freeze.role_of
    cases_by_id = {str(c.get("case_id") or ""): c for c in cases}
    s_case_ids = [cid for cid in freeze.ordered_case_ids if role_of.get(cid) == ROLE_S]
    h_case_ids = [cid for cid in freeze.ordered_case_ids if role_of.get(cid) == ROLE_H]
    v_case_ids = [cid for cid in freeze.ordered_case_ids if role_of.get(cid) == ROLE_V]
    sealed_samples = _sealed_samples(cases, role_of)

    bundle_hash = str(getattr(manifest, "bundle_hash", "") or "") or str(parent.get("bundle_hash") or "")
    content_hash = str(getattr(manifest, "skill_content_hash", "") or "") or str(parent.get("skill_content_hash") or "")
    fingerprint = str(freeze.fingerprint or "")
    prompt_sha = ""
    prompt_path = Path(__file__).resolve().parent / "prompts" / ATTRIBUTION_PROMPT_NAME
    if prompt_path.is_file():
        prompt_sha = _sha256_text(prompt_path.read_text(encoding="utf-8"))

    record: dict[str, Any] = {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "feedback_id": "fb-" + _canonical_sha256(
            {"candidate_id": candidate_id, "generation": generation, "fingerprint": fingerprint, "kind": "feedback"}
        )[:16],
        "parent_candidate_id": candidate_id,
        "generation": generation,
        "bundle_hash": bundle_hash,
        "skill_content_hash": content_hash,
        "protocol_fingerprint": fingerprint,
        "cases_sha256": freeze.cases_sha256,
        "scope": {
            "reflection_scope": "S",
            "s_case_ids": list(s_case_ids),
            "v_cases_excluded": list(v_case_ids),
            "h_cases_excluded": list(h_case_ids),
            "note": "反思只对搜索集 S 开放；V 不向变异器开放逐题答案，H 封存内容不进提示或日志",
        },
        "derivation": {
            "attributor": str(parent.get("attributor") or "offline_synthetic"),
            "prompt": {"name": f"prompts/{ATTRIBUTION_PROMPT_NAME}", "version": ATTRIBUTION_PROMPT_VERSION, "sha256": prompt_sha},
            "algorithm_version": str(parent.get("algorithm_version") or "skill-search-feedback/v1"),
            "gates": {
                "min_confidence": resolved_gates.min_confidence,
                "min_signal": resolved_gates.min_signal,
                "signal_epsilon": resolved_gates.signal_epsilon,
                "require_full_trace": resolved_gates.require_full_trace,
            },
        },
        "trace_refs": [],
        "items": [],
        "preserve": [],
        "triage": [],
        "blockers": [],
        "mutation_gate": {"allowed": False, "reason": "", "module_id": "", "signal": 0.0},
        "mutation_candidates": [],
        "mutation_result": {"invoked": False},
    }

    evals_by_case: dict[str, dict[str, Any]] = {}
    for evaluation in evaluations:
        case_id = str(evaluation.get("case_id") or "")
        if case_id not in {c.get("case_id") for c in cases}:
            record["blockers"].append({"code": "unknown_case_evaluation", "case_id": case_id})
            continue
        role = role_of.get(case_id)
        if role == ROLE_H:
            # H 评分/轨迹不进反馈记录（封存集内容不可进入反思）。
            record["blockers"].append({"code": "sealed_case_evaluation_excluded", "case_id": case_id})
            continue
        if role == ROLE_V:
            record["blockers"].append({"code": "v_case_excluded_from_feedback", "case_id": case_id})
            continue
        evals_by_case[case_id] = evaluation
    if evals_by_case:
        record["evaluation_refs"] = {
            case_id: _evaluation_ref(candidate_id, case_id, fingerprint) for case_id in sorted(evals_by_case)
        }

    def _trace_ref_of(case_id: str) -> str:
        evaluation = evals_by_case.get(case_id) or {}
        return str(evaluation.get("trace_ref") or (trace_refs or {}).get(case_id, ""))

    def _chunks_of(case_id: str) -> list[Any]:
        ref = _trace_ref_of(case_id)
        return trace_store.chunks(ref, max_chars=resolved_gates.max_chunk_chars) if ref else []

    # ── 1. 成功 Case → 行为保持条目（不能改变通过语义） ─────────────────────
    passing_ids: list[str] = []
    failed_scored: list[str] = []
    for case_id in s_case_ids:
        evaluation = evals_by_case.get(case_id)
        if evaluation is None:
            continue
        terminal = str(evaluation.get("terminal_status") or "")
        if terminal == TERMINAL_PASS:
            passing_ids.append(case_id)
        elif terminal == TERMINAL_SCORED_FAIL:
            failed_scored.append(case_id)

    for case_id in passing_ids:
        evaluation = evals_by_case[case_id]
        trace_ref = _trace_ref_of(case_id)
        citations = [
            {
                "type": "score_record",
                "case_id": case_id,
                "candidate_id": str(evaluation.get("candidate_id") or candidate_id),
                "source": SOURCE_EXECUTION,
                "hard_evidence": True,
                "sha256": _sha256_text(json.dumps(_score_record_payload(evaluation), ensure_ascii=False, sort_keys=True)),
            }
        ]
        verdict_ref = _verdict_event_ref(trace_store, trace_ref)
        if verdict_ref:
            event = trace_store.event(trace_ref, verdict_ref.rpartition("#")[2])
            citations.append(
                {"event_ref": verdict_ref, "source": SOURCE_EXECUTION, "hard_evidence": True, "sha256": _sha256_text(event.get("text") or "")}
            )
        record["preserve"].append(
            {
                "case_id": case_id,
                "score": evaluation.get("score"),
                "execution_level": str(evaluation.get("execution_level") or ""),
                "citations": citations,
                "behavior": "保持本 Case 通过语义：该 Case 的预期行为与评分维度不得被候选改写",
            }
        )

    # ── 2. 终态确定性分流（infra/无效/安全；无需模型） ────────────────────────
    for case_id in s_case_ids:
        evaluation = evals_by_case.get(case_id)
        if evaluation is None:
            record["blockers"].append({"code": "case_evaluation_missing", "case_id": case_id})
            continue
        terminal = str(evaluation.get("terminal_status") or "")
        if terminal in (TERMINAL_PASS, TERMINAL_SCORED_FAIL):
            continue
        kind, route = _TRIAGE_BY_TERMINAL.get(terminal, ("unknown", ROUTE_MANUAL_CLARIFICATION))
        record["triage"].append(
            {
                "case_id": case_id,
                "kind": kind,
                "route": route,
                "write": False,
                "reason_codes": [terminal],
                "note": "终态由 DE-30 完整性门判定（确定性，无需模型归因）",
            }
        )

    # ── 3. 归因提案核验（逐 Case，至多一条终态提案） ─────────────────────────
    proposals = [normalize_proposal(p) for p in (attributions or [])]
    seen_case: dict[str, int] = {}
    verified_items: list[dict[str, Any]] = []
    for index, proposal in enumerate(proposals):
        case_id = proposal["case_id"]
        if case_id not in s_case_ids:
            record["blockers"].append(
                {"code": "attribution_outside_search_set", "case_id": case_id or f"proposal#{index}"}
            )
            continue
        if case_id in seen_case:
            record["blockers"].append({"code": STOP_DUPLICATE_PROPOSAL, "case_id": case_id})
            continue
        seen_case[case_id] = index
        evaluation = evals_by_case.get(case_id)
        case = cases_by_id.get(case_id) or {}
        if evaluation is None:
            record["blockers"].append({"code": REJECT_CASE_NOT_SCORED, "case_id": case_id, "detail": "无评分记录"})
            continue
        verdict = verify_attribution(
            proposal,
            case=case,
            evaluation=evaluation,
            parent_candidate_id=candidate_id,
            trace_store=trace_store,
            trace_ref=_trace_ref_of(case_id),
        )
        if not verdict.get("ok"):
            record["blockers"].append(
                {"code": verdict.get("reason"), "case_id": case_id, "detail": verdict.get("detail", "")}
            )
            continue
        item = verdict["item"]
        item["feedback_id"] = "fb-" + _canonical_sha256(
            {"candidate_id": candidate_id, "case_id": case_id, "fingerprint": fingerprint, "category": item["category"], "index": index}
        )[:16]
        item["evaluation_ref"] = _evaluation_ref(candidate_id, case_id, fingerprint)
        verified_items.append(item)

    # ── 4. 非变异类目分流（环境/rubric/Case/澄清/知识源/Maintainer） ─────────
    rubric_hit = False
    environment_hit = False
    case_issue_ids: list[str] = []
    for item in verified_items:
        category = item["category"]
        case_id = item["case_id"]
        evidence_refs = [c.get("event_ref") or c.get("case_id") for c in item["citations"]]
        evidence = [{"ref": str(r), "sha256": _sha256_text(str(r))} for r in evidence_refs]
        if category == ATTR_RUBRIC_ISSUE:
            rubric_hit = True
            record["triage"].append(
                {
                    "case_id": case_id,
                    "kind": "rubric",
                    "route": ROUTE_RUBRIC_REVISION,
                    "write": False,
                    "reason_codes": [category],
                    "evidence": evidence,
                    "note": "评分器/协议问题：修 rubric 后更换协议版本重跑基线，不与旧分数混比较",
                }
            )
        elif category == ATTR_ENVIRONMENT_ISSUE:
            environment_hit = True
            record["triage"].append(
                {
                    "case_id": case_id,
                    "kind": "environment",
                    "route": ROUTE_ENVIRONMENT_REPAIR,
                    "write": False,
                    "reason_codes": [category],
                    "evidence": evidence,
                    "note": "环境/基础设施问题：进 DE 技术台账，不当作技能退化",
                }
            )
        elif category == ATTR_CASE_ISSUE:
            case_issue_ids.append(case_id)
            record["triage"].append(
                {
                    "case_id": case_id,
                    "kind": "case",
                    "route": ROUTE_CASE_REVISION,
                    "write": False,
                    "reason_codes": [category],
                    "evidence": evidence,
                    "note": "Case 期望本身有问题：该 Case 剔除出信号，交 Case 修订",
                }
            )
        elif category == ATTR_UNCERTAIN:
            record["triage"].append(
                {
                    "case_id": case_id,
                    "kind": "manual",
                    "route": ROUTE_MANUAL_CLARIFICATION,
                    "write": False,
                    "reason_codes": [category],
                    "evidence": evidence,
                    "note": "证据不足或结论不确定：人工澄清，不自动变异",
                }
            )
        elif category == ATTR_SKILL_ISSUE:
            sub_kind = item["sub_kind"] or SKILL_SUB_BODY_DEFECT
            if sub_kind == SKILL_SUB_KNOWLEDGE_FACT:
                record["triage"].append(
                    {
                        "case_id": case_id,
                        "kind": "knowledge_fact",
                        "route": ROUTE_KNOWLEDGE_SOURCE_SERVICE,
                        "route_ref": dict(KNOWLEDGE_SOURCE_SERVICE_REF),
                        "write": False,
                        "reason_codes": [sub_kind],
                        "evidence": evidence,
                        "note": KNOWLEDGE_SOURCE_SERVICE_REF["note"],
                    }
                )
            elif sub_kind == SKILL_SUB_PREFERENCE_CONFLICT:
                record["triage"].append(
                    {
                        "case_id": case_id,
                        "kind": "preference_conflict",
                        "route": ROUTE_ONLINE_MAINTAINER,
                        "route_ref": dict(ONLINE_MAINTAINER_REF),
                        "write": False,
                        "reason_codes": [sub_kind],
                        "evidence": evidence,
                        "note": ONLINE_MAINTAINER_REF["note"],
                    }
                )
            # skill_body_defect 的分流在变异门之后补齐（放行/拦截各有去向）。

    # ── 5. 变异门（零信号/低置信/并列/跨 Module/只读/证据不足 → 不调变异器） ──
    gate_blockers: list[dict[str, Any]] = []
    module_signal: dict[str, float] = {}
    grade_items: list[dict[str, Any]] = []

    if failed_scored and not proposals:
        record["blockers"].append({"code": STOP_NO_ATTRIBUTOR, "detail": "存在有效失败但未提供归因提案（离线注入或真实模型）"})

    excluded_case_ids = set(case_issue_ids)
    skill_body_items = [
        it
        for it in verified_items
        if it["category"] == ATTR_SKILL_ISSUE
        and (it["sub_kind"] or SKILL_SUB_BODY_DEFECT) == SKILL_SUB_BODY_DEFECT
        and it["case_id"] not in excluded_case_ids
    ]
    # 知识事实/偏好冲突等不经变异门的 SKILL_ISSUE 条目：其自带 blocker 就是停止原因
    skill_item_blockers = [
        b["code"]
        for it in verified_items
        if it["category"] == ATTR_SKILL_ISSUE
        and (it["sub_kind"] or SKILL_SUB_BODY_DEFECT) != SKILL_SUB_BODY_DEFECT
        for b in it["blockers"]
    ]
    for item in skill_body_items:
        case_id = item["case_id"]
        confidence = float(item.get("confidence") or 0.0)
        if confidence < resolved_gates.min_confidence:
            gate_blockers.append({"code": STOP_LOW_CONFIDENCE, "case_id": case_id, "confidence": confidence})
            continue
        if item["rejected_citations"]:
            gate_blockers.append(
                {"code": item["blockers"][0]["code"] if item["blockers"] else "citation_rejected", "case_id": case_id}
            )
            continue
        if not item["citations"]:
            gate_blockers.append(
                {"code": STOP_HIGH_CONF_NO_EVIDENCE, "case_id": case_id,
                 "detail": "高 confidence 不替代证据支持：无任何有效引用"}
            )
            continue
        if not item["hard_evidence"]:
            gate_blockers.append({"code": _soft_evidence_code(item["citations"]), "case_id": case_id})
            continue
        # 关键 Trace 未读全 → 不得高置信自动归因（分块消费状态可见）。
        if resolved_gates.require_full_trace:
            all_refs = [c.ref for c in _chunks_of(case_id)]
            listed = {str(r) for r in (consumed_chunks or {}).get(case_id, [])} if consumed_chunks is not None else set(all_refs)
            missing = [r for r in all_refs if r not in listed]
            if missing:
                unconsumed_events = []
                for chunk in _chunks_of(case_id):
                    if chunk.ref in set(missing):
                        unconsumed_events.append({"chunk_ref": chunk.ref, "event_ids": list(chunk.event_ids)})
                entry = {"code": STOP_UNCONSUMED_EVIDENCE, "case_id": case_id, "unconsumed": unconsumed_events}
                gate_blockers.append(entry)
                record["blockers"].append(dict(entry))
                continue
        if module_spec is None:
            gate_blockers.append({"code": STOP_MODULE_SPEC_MISSING, "case_id": case_id})
            continue
        hypotheses = [h for h in item["module_hypotheses"] if h.get("module_id")]
        if not hypotheses:
            gate_blockers.append({"code": STOP_MODULE_UNASSIGNED, "case_id": case_id})
            continue
        module_ids = []
        readonly_hit = ""
        not_found = ""
        for hypothesis in hypotheses:
            module = module_spec.module(str(hypothesis["module_id"]))
            if module is None:
                not_found = str(hypothesis["module_id"])
                break
            module_ids.append(module.module_id)
            if not module.writable:
                readonly_hit = module.module_id
                break
        if not_found:
            gate_blockers.append({"code": STOP_MODULE_NOT_FOUND, "case_id": case_id, "module_id": not_found})
            continue
        if readonly_hit:
            gate_blockers.append({"code": STOP_READONLY_MODULE, "case_id": case_id, "module_id": readonly_hit})
            continue
        if len(set(module_ids)) > 1:
            gate_blockers.append(
                {"code": STOP_MULTI_MODULE, "case_id": case_id, "module_ids": sorted(set(module_ids))}
            )
            continue
        # 干净条目：计入模块信号 sum((1-score_i)*confidence_i)
        evaluation = evals_by_case[case_id]
        score = float(evaluation.get("score") or 0.0)
        hypothesis = hypotheses[0]
        hyp_conf = hypothesis.get("confidence")
        hyp_conf = confidence if hyp_conf is None else float(hyp_conf)
        module_signal[hypothesis["module_id"]] = module_signal.get(hypothesis["module_id"], 0.0) + (1.0 - score) * hyp_conf
        grade_items.append(item)

    if not grade_items:
        if rubric_hit:
            reason = STOP_RUBRIC_ISSUE
        elif environment_hit:
            reason = STOP_ENVIRONMENT_ISSUE
        elif gate_blockers:
            reason = gate_blockers[0]["code"]
        elif skill_item_blockers:
            reason = skill_item_blockers[0]
        elif passing_ids and not failed_scored:
            reason = STOP_ALL_PASSED
        elif verified_items and all(it["category"] == ATTR_UNCERTAIN for it in verified_items):
            reason = STOP_UNCERTAIN_ONLY
        elif failed_scored:
            reason = STOP_NO_ATTRIBUTOR if not proposals else STOP_ZERO_SIGNAL
        elif record["triage"]:
            # 无可比分数的终态（infra/无效/安全）：无变异对象，按确定性分流给原因
            reason = (
                STOP_ENVIRONMENT_ISSUE
                if any(t["kind"] == "environment" for t in record["triage"])
                else STOP_NO_SCORED_FAILURE
            )
        else:
            reason = STOP_ALL_PASSED
        record["mutation_gate"] = {
            "allowed": False,
            "reason": reason,
            "module_id": "",
            "signal": 0.0,
            "blockers": gate_blockers,
        }
        record["blockers"].extend(dict(b) for b in gate_blockers if b not in record["blockers"])
    else:
        total_signal = sum(module_signal.values())
        top = max(module_signal.values())
        tops = sorted(m for m, s in module_signal.items() if abs(s - top) <= resolved_gates.signal_epsilon)
        if total_signal <= resolved_gates.min_signal:
            record["mutation_gate"] = {
                "allowed": False, "reason": STOP_ZERO_SIGNAL, "module_id": "",
                "signal": round(total_signal, 9), "blockers": gate_blockers,
            }
        elif len(tops) > 1:
            record["mutation_gate"] = {
                "allowed": False, "reason": STOP_TIED_TOP_SIGNAL, "module_id": "",
                "signal": round(total_signal, 9), "tied_modules": tops, "blockers": gate_blockers,
            }
        else:
            target = tops[0]
            preserve_refs = [
                {"case_id": p["case_id"], "citations": p["citations"]} for p in record["preserve"]
            ]
            brief = {
                "target_module": target,
                "signal": round(top, 9),
                "based_on": [it["feedback_id"] for it in grade_items if it["module_hypotheses"][0]["module_id"] == target],
                "citations": [
                    c for it in grade_items if it["module_hypotheses"][0]["module_id"] == target for c in it["citations"]
                ],
                "preserve": preserve_refs,
                "constraint": (
                    "只改目标 Module（DE-32 有界编辑契约）；成功 Case 行为保持；"
                    "不新增工具/权限/依赖；不得硬编码 Case 答案；其余源片段哈希不变"
                ),
                "note": "本简报只是证据定位与约束；patch 由 DE-32 变异器生成",
            }
            record["mutation_gate"] = {
                "allowed": True, "reason": "", "module_id": target,
                "signal": round(total_signal, 9), "blockers": gate_blockers,
            }
            record["mutation_candidates"] = [brief]
            if mutator is not None:
                try:
                    result = mutator(brief)
                    record["mutation_result"] = {"invoked": True, "result": result if isinstance(result, dict) else {"value": str(result)}}
                except Exception as exc:  # noqa: BLE001 - 变异器失败落账，不中断反馈编译
                    record["mutation_result"] = {"invoked": True, "error": f"{type(exc).__name__}: {exc}"}
                    record["blockers"].append({"code": STOP_MUTATOR_FAILED, "detail": str(exc)})
            else:
                record["mutation_result"] = {"invoked": False, "note": "mutator not provided（DE-32 接入前仅产出简报）"}

    # ── 6. 被拦截的 skill_body_defect 条目补分流去向 ─────────────────────────
    routed_cases = {t["case_id"] for t in record["triage"]}
    for item in skill_body_items:
        case_id = item["case_id"]
        if case_id in routed_cases:
            continue
        codes = [b["code"] for b in item["blockers"]] + [
            b["code"] for b in gate_blockers if b.get("case_id") == case_id
        ]
        record["triage"].append(
            {
                "case_id": case_id,
                "kind": "manual",
                "route": ROUTE_MANUAL_CLARIFICATION,
                "write": False,
                "reason_codes": codes or [STOP_NO_HARD_EVIDENCE],
                "evidence": [{"ref": str(c.get("event_ref") or c.get("case_id")), "sha256": _sha256_text(str(c.get("event_ref") or c.get("case_id")))} for c in item["citations"]],
                "note": "技能正文缺陷候选未过变异门：人工复核（停止原因已结构化落盘）",
            }
        )

    # ── 7. IR 映射（原 experience-standard/v1 的证据/决策规则 + event 引用） ──
    for item in verified_items:
        if item["category"] != ATTR_SKILL_ISSUE:
            item["ir"] = None
            continue
        case = cases_by_id.get(item["case_id"]) or {}
        item["ir"] = _build_ir_mapping(item, str(case.get("question") or ""), candidate_id)
        if item["ir"] and item["ir"]["validation_errors"]:
            record["blockers"].append(
                {"code": "ir_validation_failed", "case_id": item["case_id"], "errors": item["ir"]["validation_errors"]}
            )

    record["items"] = verified_items
    record["trace_refs"] = sorted({_trace_ref_of(cid) for cid in s_case_ids if _trace_ref_of(cid)})

    # ── 8. S3 安全终检：H 内容不得进入反馈记录（= 将被日志消费的载荷） ────────
    assert_no_sealed_content(json.dumps(record, ensure_ascii=False), sealed_samples, context="compile_feedback record")
    return record


def _sealed_samples(cases: list[dict[str, Any]], role_of: dict[str, str]) -> list[str]:
    samples: list[str] = []
    for case in cases:
        if role_of.get(str(case.get("case_id") or "")) != ROLE_H:
            continue
        for key in ("question", "expected", "expected_answer"):
            value = str(case.get(key) or "").strip()
            if value:
                samples.append(value)
    return samples


def save_feedback(record: dict[str, Any], path: str | Path) -> dict[str, Any]:
    """反馈记录 + 结构化分流决策 JSON 落盘（搜索命名空间内，调用方给路径）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    target.write_text(payload, encoding="utf-8")
    return {"ok": True, "path": str(target), "bytes": len(payload.encode("utf-8"))}
