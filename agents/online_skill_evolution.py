from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable


SideQuery = Callable[[str, str], Awaitable[str]]
ConfirmWrite = Callable[[str], Awaitable[bool]]


@dataclass
class OnlineSkillCandidate:
    name: str
    description: str
    when_to_use: str = ""
    instructions: str = ""
    evidence: str = ""
    tags: list[str] = field(default_factory=list)
    # ── 以下为 IR 门（EVOHARNESS_IR_MODE=annotate/strict）新增字段，旧 6 字段一字未动 ──
    evidence_items: list[Any] = field(default_factory=list)   # 内部结构化视图：仅由 evidence 列表形态填充，绝不读名为 evidence_items 的模型键
    knowledge_gaps: list[Any] = field(default_factory=list)   # 模型键 knowledge_gaps 原样透传（{question, why_needed} 或畸形）
    decision_rules: list[Any] = field(default_factory=list)   # 模型键 decision_rules 原样透传（[{when,then,because}] 或畸形）
    ir_mode: str = ""
    ir_record: dict[str, Any] | None = None                   # 修复后十字段 IR；off 与 gate 降级时恒 None
    ir_errors: list[str] = field(default_factory=list)
    ir_repairs: list[str] = field(default_factory=list)
    ir_grounding: list[dict[str, Any]] = field(default_factory=list)


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except Exception:
            return {}
    return {}


def _normalize_identity(text: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", raw).strip()


def _candidate_search_text(candidate: OnlineSkillCandidate) -> str:
    return "\n".join(
        [
            candidate.name,
            candidate.description,
            candidate.when_to_use,
            candidate.instructions,
            " ".join(candidate.tags),
        ]
    )


def _coerce_candidate(obj: dict[str, Any]) -> OnlineSkillCandidate | None:
    name = str(obj.get("name") or "").strip()
    description = str(obj.get("description") or "").strip()
    instructions = str(obj.get("instructions") or obj.get("prompt") or "").strip()
    if not name or not description or not instructions:
        return None
    tags_raw = obj.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [part.strip() for part in re.split(r"[,，]", tags_raw) if part.strip()]
    elif isinstance(tags_raw, list):
        tags = [str(part).strip() for part in tags_raw if str(part).strip()]
    else:
        tags = []
    # evidence 单键双形态（模型键恒为 evidence，不存在 evidence_items 模型键）：
    # 字符串 → 旧语义；列表 → evidence_items 原样保留（含畸形条目，供 IR 校验），
    # evidence 退化为各条目 text 的连接串，保住旧消费点（lesson / Creation Evidence）。
    evidence_raw = obj.get("evidence")
    evidence_items: list[Any] = []
    if isinstance(evidence_raw, list):
        evidence_items = list(evidence_raw)
        evidence_texts: list[str] = []
        for entry in evidence_raw:
            text = str(entry.get("text") or "").strip() if isinstance(entry, dict) else str(entry).strip()
            if text:
                evidence_texts.append(text)
        evidence = "；".join(evidence_texts)
    else:
        evidence = str(evidence_raw or "").strip()
    return OnlineSkillCandidate(
        name=name,
        description=description,
        when_to_use=str(obj.get("when_to_use") or obj.get("when-to-use") or "").strip(),
        instructions=instructions,
        evidence=evidence,
        tags=tags[:8],
        evidence_items=evidence_items,
        knowledge_gaps=obj.get("knowledge_gaps") or [],
        decision_rules=obj.get("decision_rules") or [],
    )


# ─── IR 门（EVOHARNESS_IR_MODE）：经验标准中间表示接入沉淀链路 ──────────────
# off（缺省）：整段跳过，提示词与全部写盘逐字符回到现网行为；
# annotate：只观测（basis 不改写，errors/grounding 只落 provenance）；
# strict：先修复再降级后拦截（降级后仍有 validate 错误 → action="ir_rejected" 不写盘）。
# M1（agents/experience_standard.py）经 _ir_tools 惰性导入，缺失时 fail-open 降级旧路径。

_EXTRACT_SYSTEM_BASE = (
    "You are EvoHarness's online Skill Extractor.\n"
    "Extract at most ONE reusable skill candidate from a live conversation window.\n"
    "Output ONLY strict JSON: {\"skills\": []} or {\"skills\": [{...}]}.\n\n"
    "Candidate fields: name, description, when_to_use, instructions, evidence, tags.\n\n"
    "Rules:\n"
    "- USER turns are the primary evidence. Assistant turns are context only.\n"
    "- A next user feedback turn may confirm, reject, or refine the prior assistant behavior.\n"
    "- Do not extract assistant-only guesses, weak confirmations, one-off task payload, secrets, project facts, URLs, account IDs, exact dates, or temporary parameters.\n"
    "- Extract only durable workflow, output policy, implementation preference, correction, or repeated constraint likely useful for future similar tasks.\n"
    "- Remove entity names and runtime-specific payload; use placeholders where needed.\n"
    "- retrieved_reference is identity context only; never treat it as new user evidence.\n"
    "- If evidence is weak, generic, or low-value, return {\"skills\": []}.\n"
)

_IR_EXTRACT_ADDENDUM = (
    "\n## Structured experience addendum (IR mode)\n\n"
    "You are now in structured-experience mode. Each skill object may additionally carry two optional keys:\n\n"
    "1. \"decision_rules\": a list of decision rules, each {\"when\": [item], \"then\": [item], \"because\": [item]},\n"
    "   where item is {\"text\": str, \"basis\": \"explicit\" or \"inferred\", \"confidence\": 0.0-1.0}.\n"
    "2. \"knowledge_gaps\": a list of {\"question\": str, \"why_needed\": str} for information the conversation did not settle.\n\n"
    "The \"evidence\" field accepts two forms:\n"
    "- a plain string (legacy form), or\n"
    "- a list of items, each {\"text\": str, \"basis\": \"explicit\" or \"inferred\", \"confidence\": 0.0-1.0}.\n\n"
    "Item rules:\n"
    "- basis=explicit ONLY for content the USER states in their own words; anything derived from assistant context is basis=inferred.\n"
    "- confidence grades how faithfully the item reflects the conversation (0.0-1.0), not how correct the advice is.\n"
    "- Item text must be non-empty and self-contained.\n"
    "- Never fabricate facts, cases, or evidence to fill gaps; put unknowns into knowledge_gaps instead.\n"
    "- Do NOT write '## Decision Rules' or '## Open Questions' sections inside instructions yourself; the harness renders those sections deterministically from the structured fields.\n"
)

_DECISION_RULES_HEADER = "## Decision Rules"
_OPEN_QUESTIONS_HEADER = "## Open Questions"
# 评测层（online_skill_eval）对 skill 文本做关键词触发：固定模板串刻意避开
# "json"/"结构化输出"（json_parseable, hard）与 "markdown table"/"表格"（markdown_table）。
_IR_SECTION_HEADER_RE = re.compile(r"^## (?:Decision Rules|Open Questions)\s*$")
_ATX_HEADER_RE = re.compile(r"^#{1,6}\s")


def _extract_system_prompt(mode: str) -> str:
    # off：逐字符等于现网字面量；annotate/strict：base + 追加块。maintain 提示词全模式不动。
    if mode in {"annotate", "strict"}:
        return _EXTRACT_SYSTEM_BASE + _IR_EXTRACT_ADDENDUM
    return _EXTRACT_SYSTEM_BASE


def _ir_mode() -> str:
    raw = str(os.environ.get("EVOHARNESS_IR_MODE") or "").strip().lower()
    return raw if raw in {"off", "annotate", "strict"} else "off"


def _ir_tools() -> Any | None:
    # M1 惰性导入单点：绝不顶层 import（agent.py 对 online_ingest 的 import 自带
    # try/except，顶层依赖会让 M1 单独回滚时静默关闭整条沉淀链路）。
    try:
        from . import experience_standard as tools
    except Exception:
        return None
    required = ("SCHEMA_VERSION", "repair_record", "validate_record", "grounding_flag")
    if all(hasattr(tools, name) for name in required):
        return tools
    return None


@dataclass
class IrGateResult:
    mode: str = "off"                                                   # off|annotate|strict
    record: dict[str, Any] | None = None                                # 修复后的完整十字段 IR；降级/失败时 None
    errors: list[str] = field(default_factory=list)                     # validate_record 原样输出（修复/降级后仍剩的）或降级码
    repairs: list[str] = field(default_factory=list)                    # repair_record 应用的修复码（M1 七码，见 C1 裁决）
    grounding: list[dict[str, Any]] = field(default_factory=list)       # [{path,text,basis,has_overlap,downgraded}]
    blocked: bool = False                                               # 仅 strict 且 errors 非空时 True；降级恒 False


def _window_text(messages: list[dict[str, Any]]) -> str:
    # 与 skill_evolution._compact_messages 同窗口（末 12 条 user/assistant 轮、单条截 4000 字），
    # 但不加 "...[truncated]" 后缀、不做单行压缩——这是 provenance messages 通道的拼接字符串
    # 形态（同源同参、内容不相等）；validate 的 raw_text 相等检查以同一 window_text 闭包自洽。
    lines: list[str] = []
    for item in list(messages or [])[-12:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        lines.append(f"{role}: {content[:4000]}")
    return "\n".join(lines)


def build_ir_record(
    candidate: OnlineSkillCandidate,
    *,
    window_text: str,
    schema_version: str = "experience-standard/v1",
) -> dict[str, Any]:
    # 十字段 IR 骨架（与 experience-standard/v1 empty_record 同形，全部子键恒在）。
    # 只做确定性搬运：evidence_items / decision_rules / knowledge_gaps 畸形原样透传，
    # 交 M1.repair_record（可修复项）与 M1.validate_record（报错项），此处绝不静默修正。
    window = str(window_text or "")
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", window))
    ascii_count = len(re.findall(r"[A-Za-z]", window))
    language = "zh-CN" if cjk_count * 2 >= ascii_count else "en"
    observations: list[Any] = []
    confidences: list[float] = []
    if candidate.evidence_items:
        observations = list(candidate.evidence_items)
        for entry in observations:
            if not isinstance(entry, dict):
                continue
            value = entry.get("confidence")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if 0.0 <= float(value) <= 1.0:
                confidences.append(float(value))
    elif candidate.evidence:
        observations = [{"text": candidate.evidence}]
    # 记录级 confidence：仅对数值型且落在 [0,1] 的条目 confidence 取均值再钳制；
    # 越界值（如 1.5）被排除在均值外，但仍会在条目级被 validate 报错（观测信号不洗白）。
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    mean_confidence = round(min(1.0, max(0.0, mean_confidence)), 6)
    return {
        "schema_version": schema_version,
        "source": {"raw_text": window, "language": language, "source_type": "online_conversation"},
        "identity": {
            "title": candidate.name,
            "summary": candidate.description,
            "primary_type": "heuristic",
            "secondary_types": [],
        },
        "situation": {
            "goal": [],
            "triggers": [{"text": candidate.when_to_use}] if candidate.when_to_use else [],
            "context": [],
            "entities": [],
        },
        "guidance": {
            "principles": [],
            "decision_rules": list(candidate.decision_rules),
            "steps": [],
            "avoid": [],
        },
        "applicability": {"conditions": [], "prerequisites": [], "exceptions": [], "boundaries": []},
        "outcome": {"expected": [], "success_signals": [], "failure_signals": []},
        "evidence": {
            "observations": observations,
            "counterexamples": [],
            "validation_methods": [],
            "confidence": mean_confidence,
        },
        "indexing": {
            "domains": [],
            "tasks": [],
            "tags": list(candidate.tags),
            "aliases": [],
            "cross_domain_links": [],
        },
        "knowledge_gaps": list(candidate.knowledge_gaps),
    }


def _walk_explicit_items(
    record: dict[str, Any],
    window_text: str,
    *,
    grounding_flag: Any,
    downgrade: bool,
) -> list[dict[str, Any]]:
    # 遍历全部条目形列表中的 explicit 条目做 grounding 观测；strict 下对零重叠条目
    # 就地降级（basis→inferred、confidence 封顶 0.5）。报告的 basis 恒为声明值 explicit。
    entries: list[dict[str, Any]] = []

    def visit(path: str, bucket: Any) -> None:
        if not isinstance(bucket, list):
            return
        for index, value in enumerate(bucket):
            if not isinstance(value, dict) or value.get("basis") != "explicit":
                continue
            text = str(value.get("text") or "")
            has_overlap = bool(grounding_flag(text, window_text))
            downgraded = False
            if downgrade and not has_overlap:
                value["basis"] = "inferred"
                confidence = value.get("confidence")
                if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                    value["confidence"] = 0.5
                else:
                    value["confidence"] = min(float(confidence), 0.5)
                downgraded = True
            entries.append(
                {
                    "path": f"{path}[{index}]",
                    "text": text,
                    "basis": "explicit",
                    "has_overlap": has_overlap,
                    "downgraded": downgraded,
                }
            )

    situation = record.get("situation") if isinstance(record, dict) else None
    if isinstance(situation, dict):
        for key in ("goal", "triggers", "context", "entities"):
            visit(f"situation.{key}", situation.get(key))
    guidance = record.get("guidance") if isinstance(record, dict) else None
    if isinstance(guidance, dict):
        rules = guidance.get("decision_rules")
        if isinstance(rules, list):
            for rule_index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    continue
                for key in ("when", "then", "because"):
                    visit(f"guidance.decision_rules[{rule_index}].{key}", rule.get(key))
    evidence = record.get("evidence") if isinstance(record, dict) else None
    if isinstance(evidence, dict):
        visit("evidence.observations", evidence.get("observations"))
    return entries


def _ground_explicit_items(record: dict[str, Any], window_text: str, *, grounding_flag: Any) -> list[dict[str, Any]]:
    return _walk_explicit_items(record, window_text, grounding_flag=grounding_flag, downgrade=False)


def _downgrade_ungrounded(record: dict[str, Any], window_text: str, *, grounding_flag: Any) -> list[dict[str, Any]]:
    return _walk_explicit_items(record, window_text, grounding_flag=grounding_flag, downgrade=True)


def _sync_ir_candidate_state(candidate: OnlineSkillCandidate, gate: IrGateResult) -> None:
    candidate.ir_mode = gate.mode
    candidate.ir_record = gate.record
    candidate.ir_errors = list(gate.errors)
    candidate.ir_repairs = list(gate.repairs)
    candidate.ir_grounding = [dict(item) for item in gate.grounding]


def apply_ir_gate(candidate: OnlineSkillCandidate, *, messages: list[dict[str, Any]]) -> IrGateResult:
    # IR 中间步骤：candidate+messages → build_ir_record → M1.repair_record → M1.validate_record
    # →（strict）降级零重叠 explicit 条目 → 拦截判定。
    # 整体 fail-open：M1 缺失（ir_gate_unavailable）或任何运行时异常（ir_gate_error）都降级为
    # 完全旧路径 + provenance 留痕，绝不向调用方抛——后台路径 agent._schedule_background_skill_task
    # 的 _done 回调会吞异常、interactive extract_now 是裸 await，gate 一旦抛错就会让演化无声
    # 消失或污染交互层。strict 降级方向为放行：门是观察哨，故障时退化为旧行为 + 留痕可接受。
    mode = _ir_mode()
    if mode == "off":
        return IrGateResult(mode="off")
    try:
        tools = _ir_tools()
        if tools is None:
            gate = IrGateResult(mode=mode, errors=["ir_gate_unavailable"])
        else:
            window_text = _window_text(messages)
            record = build_ir_record(candidate, window_text=window_text, schema_version=tools.SCHEMA_VERSION)
            record, repairs = tools.repair_record(record)
            errors = [str(item) for item in tools.validate_record(record, window_text)]
            grounding: list[dict[str, Any]] = []
            if mode == "strict":
                grounding = _downgrade_ungrounded(record, window_text, grounding_flag=tools.grounding_flag)
                errors = [str(item) for item in tools.validate_record(record, window_text)]
            else:
                grounding = _ground_explicit_items(record, window_text, grounding_flag=tools.grounding_flag)
            gate = IrGateResult(
                mode=mode,
                record=record,
                errors=errors,
                repairs=list(repairs),
                grounding=grounding,
                blocked=bool(mode == "strict" and errors),
            )
    except Exception as exc:  # fail-open：绝不外抛
        gate = IrGateResult(mode=mode, errors=[f"ir_gate_error: {type(exc).__name__}: {exc}"[:500]])
    _sync_ir_candidate_state(candidate, gate)
    return gate


def _flatten_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _flatten_rule_texts(bucket: Any) -> str:
    if not isinstance(bucket, list):
        return ""
    texts: list[str] = []
    for entry in bucket:
        text = _flatten_text(entry.get("text") if isinstance(entry, dict) else entry)
        if text:
            texts.append(text)
    return "；".join(texts)


def _render_gap_lines(gaps: Any) -> list[str]:
    lines: list[str] = []
    if not isinstance(gaps, list):
        return lines
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        question = _flatten_text(gap.get("question"))
        if not question:
            continue
        why_needed = _flatten_text(gap.get("why_needed"))
        lines.append(f"- {question}（why needed: {why_needed}）" if why_needed else f"- {question}")
    return lines


def render_ir_sections(record: dict[str, Any] | None) -> str:
    # M2→M3 区格式契约权威实现（C2 裁决：bullet 形态）。渲染防御规则：非 dict 的
    # rule/gap 条目跳过；when 或 then 压平后为空跳过（缺键同空处理）；because 空则
    # 省略该行；gap question 空跳过、why_needed 空则省略括注；两区分别仅在有内容时
    # 出现，均在前时 Decision Rules 在前。条目 text 压单行，可被 M3 侧正则确定性解析。
    if not isinstance(record, dict):
        return ""
    rule_blocks: list[str] = []
    guidance = record.get("guidance")
    rules = guidance.get("decision_rules") if isinstance(guidance, dict) else None
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            when = _flatten_rule_texts(rule.get("when"))
            then = _flatten_rule_texts(rule.get("then"))
            because = _flatten_rule_texts(rule.get("because"))
            if not when or not then:
                continue
            lines = [f"- When: {when}", f"  Then: {then}"]
            if because:
                lines.append(f"  Because: {because}")
            rule_blocks.append("\n".join(lines))
    gap_lines = _render_gap_lines(record.get("knowledge_gaps"))
    sections: list[str] = []
    if rule_blocks:
        sections.append("\n\n".join([_DECISION_RULES_HEADER, *rule_blocks]))
    if gap_lines:
        sections.append("\n\n".join([_OPEN_QUESTIONS_HEADER, *gap_lines]))
    return "\n\n".join(sections)


def _strip_ir_sections(instructions: str) -> str:
    # 剥除状态机（追加前必经，防 maintainer 回显存量正文里的两区产生双同名区）。
    # 逐行扫描：命中 IR 区头 → 进入剥除态并弹掉此前紧邻空行；剥除态下吞掉区内容行
    # （含空行）；仅当遇到非 IR 区的 ATX 标题行时退出剥除态并保留该行（退出前上一保留
    # 行非空则补一个分隔空行）。IR 区头判定先于剥除态分支——两区连续时逐一剥净。
    # 语义边界（有界损失）：回显区之后若无任何标题行而直接跟散文，该散文随区一并吞掉；
    # canonical 存量 body 的两区后恒跟 '## Creation Evidence'/'## Evolution Notes' 等标题。
    kept: list[str] = []
    stripping = False
    for line in str(instructions or "").split("\n"):
        if _IR_SECTION_HEADER_RE.match(line):
            stripping = True
            while kept and not kept[-1].strip():
                kept.pop()
            continue
        if stripping:
            if _ATX_HEADER_RE.match(line):
                if kept and kept[-1].strip():
                    kept.append("")
                stripping = False
                kept.append(line)
            continue
        kept.append(line)
    return "\n".join(kept)


def _with_ir_sections(instructions: str, record: dict[str, Any] | None) -> str:
    body = str(instructions or "")
    if not isinstance(record, dict):
        # off / gate 降级：不剥不追加，逐字符原样（含存量回显也不动）。
        return body
    body = _strip_ir_sections(body)
    extra = render_ir_sections(record)
    if not extra:
        return body
    return (body.rstrip() + "\n\n" + extra) if body.strip() else extra


_LEGACY_CANDIDATE_FIELDS = ("name", "description", "when_to_use", "instructions", "evidence", "tags")


def _legacy_candidate_view(candidate: OnlineSkillCandidate) -> dict[str, Any]:
    # 旧 6 字段视图（声明序）：maintainer payload 与 result.candidate 与现网 asdict 逐字节相等，
    # IR 新增字段一律不外泄给提示词与返回值。
    full = asdict(candidate)
    return {key: full[key] for key in _LEGACY_CANDIDATE_FIELDS}


def _record_without_raw_text(record: dict[str, Any]) -> dict[str, Any]:
    # source.raw_text 是窗口拼接视图而非 messages 通道副本：剔除以免 provenance 里
    # 同一窗口出现第二份近似重复，不是"内容相等所以省略"。
    trimmed = dict(record)
    source = trimmed.get("source")
    if isinstance(source, dict):
        trimmed["source"] = {key: value for key, value in source.items() if key != "raw_text"}
    return trimmed


def _attach_ir_summary(result: dict[str, Any], gate: IrGateResult | None) -> None:
    # provenance/返回值的 IR 增量：仅在 IR 模式出现这些键。gate=None（off 模式）零注入——
    # off 模式全部写盘逐字符回现网，provenance 行也不得多出任何 ir_* 键。
    if gate is None:
        return
    result["ir_mode"] = gate.mode
    if gate.repairs:
        result["ir_repairs"] = list(gate.repairs)
    if gate.errors:
        result["ir_errors"] = list(gate.errors)
    if gate.grounding:
        result["ir_grounding"] = [
            {**dict(item), "text": str(item.get("text") or "")[:200]} for item in gate.grounding[:20]
        ]
    if isinstance(gate.record, dict):
        result["ir_record"] = _record_without_raw_text(gate.record)


async def extract_online_skill_candidate(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    hint: str = "",
) -> OnlineSkillCandidate | None:
    system = _extract_system_prompt(_ir_mode())
    payload = {
        "messages": messages,
        "hint": hint,
        "retrieved_reference": retrieved_reference or None,
    }
    parsed = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    skills = parsed.get("skills")
    if not isinstance(skills, list) or not skills:
        return None
    first = skills[0]
    if not isinstance(first, dict):
        return None
    return _coerce_candidate(first)


def _exact_identity_match(candidate: OnlineSkillCandidate, skills: list[Any]) -> str:
    candidate_ids = {
        _normalize_identity(candidate.name),
        _normalize_identity(candidate.description),
        _normalize_identity(candidate.when_to_use),
    }
    candidate_ids.discard("")
    for skill in skills:
        skill_ids = {
            _normalize_identity(getattr(skill, "name", "")),
            _normalize_identity(getattr(skill, "description", "")),
            _normalize_identity(getattr(skill, "when_to_use", "") or ""),
        }
        skill_ids.discard("")
        if candidate_ids & skill_ids:
            return getattr(skill, "name", "")
    return ""


async def maintain_online_skill_candidate(
    *,
    candidate: OnlineSkillCandidate,
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    confirm_write: ConfirmWrite | None = None,
    target: str = "project",
) -> dict[str, Any]:
    from .skills import create_skill, discover_skills, evolve_skill, retrieve_relevant_skills

    skills = discover_skills()
    exact_target = _exact_identity_match(candidate, skills)
    similar_hits = retrieve_relevant_skills(_candidate_search_text(candidate), limit=8, min_score=0.03)
    top_reference_name = str((retrieved_reference or {}).get("name") or "").strip()

    system = (
        "You are EvoHarness's online Skill Set Manager.\n"
        "Decide whether a candidate should add a new skill, merge into an existing skill, "
        "revise an existing skill (contradiction replacement), or be discarded.\n"
        "Output ONLY strict JSON.\n\n"
        "Schema:\n"
        "{\"action\":\"add|merge|revise|discard\",\"target_skill\":\"existing name for merge/revise\","
        "\"reason\":\"short reason\",\"merged_description\":\"optional\","
        "\"merged_when_to_use\":\"optional\",\"merged_instructions\":\"optional full merged body\","
        "\"revised_instructions\":\"optional full replacement body for revise\"}\n\n"
        "Rules:\n"
        "- Prefer merge over add when the same capability already exists.\n"
        "- Use revise ONLY when the new guidance contradicts or supersedes the target skill's current "
        "instructions (the user changed their preference); produce a complete replacement body, not a union merge. "
        "Merging opposite rules would create self-contradicting instructions.\n"
        "- Discard if the candidate duplicates an existing shared/project skill and adds no user-specific durable improvement.\n"
        "- If merging, synthesize a complete merged instruction body, preserving useful existing guidance and adding only durable new guidance.\n"
        "- Do not preserve one-off payload, secrets, transient project facts, URLs, exact dates, or assistant-only claims.\n"
    )
    payload = {
        "candidate": _legacy_candidate_view(candidate),
        "exact_identity_target": exact_target,
        "retrieved_reference": retrieved_reference or None,
        "similar_skills": similar_hits,
        "existing_skills": [
            {
                "name": getattr(skill, "name", ""),
                "description": getattr(skill, "description", ""),
                "when_to_use": getattr(skill, "when_to_use", "") or "",
                "source": getattr(skill, "source", ""),
                "context": getattr(skill, "context", ""),
                "instructions": (getattr(skill, "prompt_template", "") or "")[:6000],
            }
            for skill in skills[:80]
        ],
    }

    decision = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    action = str(decision.get("action") or "").strip().lower()
    target_skill = str(decision.get("target_skill") or "").strip()

    if exact_target:
        action = "merge"
        target_skill = exact_target
    elif action == "add" and similar_hits:
        top = similar_hits[0]
        if float(top.get("score", 0.0)) >= 0.55:
            action = "merge"
            target_skill = str(top.get("name") or "")
    elif action == "merge" and not target_skill:
        target_skill = top_reference_name

    if action not in {"add", "merge", "revise", "discard"}:
        action = "discard"

    if action == "discard":
        return {"ok": True, "action": "discard", "skill": "", "decision": decision}

    write_summary = f"online skill evolution: {action} {target_skill or candidate.name}"
    if confirm_write is not None and not await confirm_write(write_summary):
        return {
            "ok": False,
            "action": f"{action}_denied",
            "skill": target_skill or candidate.name,
            "error": "permission denied",
            "decision": decision,
        }

    if action == "revise":
        # 冲突偏好的显式处理：新经验与现有指令明确矛盾时整体替换（旧版自动存快照），
        # 而不是 merge 出一份自相矛盾的指令体，也不是 add 出同类型双 skill 打擂台。
        target_skill = target_skill or top_reference_name
        if not target_skill:
            return {"ok": False, "action": "revise", "error": "missing target_skill", "decision": decision}
        result = evolve_skill(
            skill_name=target_skill,
            lesson=candidate.evidence or candidate.description,
            rationale=str(decision.get("reason") or "Online maintainer contradiction revision"),
            target="active",
            instructions=_with_ir_sections(
                str(decision.get("revised_instructions") or candidate.instructions), candidate.ir_record
            ),
            description=str(decision.get("merged_description") or ""),
            when_to_use=str(decision.get("merged_when_to_use") or candidate.when_to_use),
            tags=candidate.tags,
        )
        return {"action": "revise", "candidate": _legacy_candidate_view(candidate), "decision": decision, **result}

    if action == "merge":
        target_skill = target_skill or top_reference_name
        if not target_skill:
            return {"ok": False, "action": "merge", "error": "missing target_skill", "decision": decision}
        result = evolve_skill(
            skill_name=target_skill,
            lesson=candidate.evidence or candidate.description,
            rationale=str(decision.get("reason") or "Online maintainer merge"),
            target="active",
            instructions=_with_ir_sections(
                str(decision.get("merged_instructions") or candidate.instructions), candidate.ir_record
            ),
            description=str(decision.get("merged_description") or ""),
            when_to_use=str(decision.get("merged_when_to_use") or candidate.when_to_use),
            tags=candidate.tags,
        )
        return {"action": "merge", "candidate": _legacy_candidate_view(candidate), "decision": decision, **result}

    result = create_skill(
        name=candidate.name,
        description=candidate.description,
        instructions=_with_ir_sections(candidate.instructions, candidate.ir_record),
        when_to_use=candidate.when_to_use,
        target=target,
        context="inline",
        user_invocable=False,
        evidence=candidate.evidence,
        actor="online",
        tags=candidate.tags,
    )
    return {"action": "add", "candidate": _legacy_candidate_view(candidate), "decision": decision, **result}


async def online_ingest(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    hint: str = "",
    confirm_write: ConfirmWrite | None = None,
    target: str = "project",
) -> dict[str, Any]:
    from .skills import record_online_provenance

    try:
        candidate = await extract_online_skill_candidate(
            messages=messages,
            side_query=side_query,
            retrieved_reference=retrieved_reference,
            hint=hint,
        )
    except Exception as exc:
        result = {"ok": False, "action": "failed", "error": str(exc)}
        record_online_provenance(
            action="failed",
            result=result,
            messages=messages,
            retrieved_reference=retrieved_reference,
            error=str(exc),
        )
        return result

    if candidate is None:
        result = {"ok": True, "action": "none"}
        record_online_provenance(
            action="none",
            result=result,
            messages=messages,
            retrieved_reference=retrieved_reference,
        )
        return result

    # IR 门：off 短路（gate=None，后续零注入）；strict 拦截在 confirm_write 之前发生，
    # 且 apply_ir_gate 内部整体 fail-open 保证不抛，此处无需再包 try。
    gate: IrGateResult | None = None
    if _ir_mode() != "off":
        gate = apply_ir_gate(candidate, messages=messages)
        if gate.blocked:
            result = {
                "ok": True,
                "action": "ir_rejected",
                "skill": candidate.name,
                "ir_mode": gate.mode,
                "ir_errors": list(gate.errors),
                "ir_repairs": list(gate.repairs),
            }
            record_online_provenance(
                action="ir_rejected",
                skill_name=candidate.name,
                result=result,
                messages=messages,
                retrieved_reference=retrieved_reference,
            )
            return result

    try:
        result = await maintain_online_skill_candidate(
            candidate=candidate,
            side_query=side_query,
            retrieved_reference=retrieved_reference,
            confirm_write=confirm_write,
            target=target,
        )
    except Exception as exc:
        result = {"ok": False, "action": "failed", "skill": candidate.name, "error": str(exc)}

    _attach_ir_summary(result, gate)

    record_online_provenance(
        action=str(result.get("action") or "none"),
        skill_name=str(result.get("skill") or candidate.name),
        result=result,
        messages=messages,
        retrieved_reference=retrieved_reference,
        decision=result.get("decision") if isinstance(result.get("decision"), dict) else None,
        error="" if result.get("ok") else str(result.get("error") or ""),
    )
    return result


async def judge_retrieved_skill_usage(
    *,
    hits: list[dict[str, Any]],
    user_message: str,
    assistant_text: str,
    side_query: SideQuery | None = None,
) -> list[dict[str, Any]]:
    if not hits:
        return []
    if side_query is None:
        assistant_lower = assistant_text.lower()
        return [
            {
                "name": hit.get("name", ""),
                "source": hit.get("source", ""),
                "skill_dir": hit.get("skill_dir", ""),
                "retrieved": True,
                "relevant": False,
                "used": str(hit.get("name", "")).lower() in assistant_lower,
                "score": float(hit.get("score", 0.0)),
                "reason": "heuristic fallback",
            }
            for hit in hits
        ]

    system = (
        "Judge whether retrieved skills were relevant to the user request and actually used in the assistant reply.\n"
        "Output ONLY strict JSON: {\"judgments\":[{\"name\":\"...\",\"relevant\":true|false,\"used\":true|false,\"reason\":\"short\"}]}.\n"
        "A skill is used only if the reply follows its distinctive workflow or policy, not merely because it was retrieved."
    )
    payload = {"user_message": user_message, "assistant_reply": assistant_text, "retrieved_skills": hits}
    parsed = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    raw_judgments = parsed.get("judgments") if isinstance(parsed.get("judgments"), list) else []
    by_name = {str(item.get("name") or ""): item for item in raw_judgments if isinstance(item, dict)}
    judgments: list[dict[str, Any]] = []
    for hit in hits:
        name = str(hit.get("name") or "")
        raw = by_name.get(name, {})
        judgments.append(
            {
                "name": name,
                "source": hit.get("source", ""),
                "skill_dir": hit.get("skill_dir", ""),
                "retrieved": True,
                "relevant": bool(raw.get("relevant")),
                "used": bool(raw.get("used")),
                "score": float(hit.get("score", 0.0)),
                "reason": str(raw.get("reason") or ""),
            }
        )
    return judgments
