"""经验标准 IR（experience-standard/v1）：对话经验的统一结构化表示与校验。

移植自 experience-capture 的 scripts/experience_standard.py（experience-standard/v1），
纯 stdlib（json/re/typing），不 import agents.* 任何模块——供沉淀链
（agents/online_skill_evolution.py）预检与评测工具包（eval/experience_eval/）复用。

纪律：
- validate_record / repair_record / record_from_mapping 均为纯函数：零 I/O、不改输入、确定性输出。
- repair 只修「缺失」，绝不覆盖「错误值」，绝不把 basis 修成 explicit，绝不发明 text/question
  内容；修复码词表（7 码）钉死在 repair_record 的 docstring，跨模块测试只允许引用登记内的码。
- 不移植 B 侧的渲染函数：渲染决策 reject（item 5），SKILL.md 正文区渲染归沉淀链调用方（M2）。
- default_tokenize 是 agents/skills.py 分词器（_TOKEN_RE/_STOP_TOKENS/_token_list）的逐字副本，
  由 tests/test_experience_standard.py 的等价断言互锁：A 侧改分词器则该测试变红要求同步。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

SCHEMA_VERSION = "experience-standard/v1"
ITEM_BASIS = {"explicit", "inferred"}
PRIMARY_TYPES = {
    "principle", "decision_rule", "procedure", "diagnostic",
    "failure_pattern", "constraint", "preference", "fact", "heuristic"
}
# 十个顶层字段：原 B 侧 validate 内联 required 列表提升为常量，供 record_from_mapping /
# repair_record / 测试复用（骨架完整性由三方共享同一份清单保证）。
RECORD_FIELDS = (
    "schema_version", "source", "identity", "situation", "guidance",
    "applicability", "outcome", "evidence", "indexing", "knowledge_gaps",
)

SYSTEM_PROMPT = """你是通用经验结构化器。你的任务不是扩写知识，而是把人类自然语言中的经验拆清楚。

必须遵守：
1. 只输出一个 JSON 对象，不要输出 Markdown。
2. 原文明确表达的内容，basis=explicit；为使经验可执行而合理推导的内容，basis=inferred。
3. 不知道的内容留空并放进 knowledge_gaps，不得补造专业事实、案例或证据。
4. 每条经验都要回答：什么场景、解决什么问题、依据什么判断、应当怎么做、何时不适用、如何判断结果。
5. 标签要使用便于检索的短语；允许多个领域和跨领域标签。
6. confidence 是对“结构化结果忠于原文”的置信度，不是对经验本身绝对正确性的判断。

输出结构严格使用下面这些字段：
{
  "schema_version": "experience-standard/v1",
  "source": {"raw_text": "", "language": "zh-CN", "source_type": "human_explicit"},
  "identity": {
    "title": "",
    "summary": "",
    "primary_type": "principle|decision_rule|procedure|diagnostic|failure_pattern|constraint|preference|fact|heuristic",
    "secondary_types": []
  },
  "situation": {"goal": [], "triggers": [], "context": [], "entities": []},
  "guidance": {
    "principles": [],
    "decision_rules": [{"when": [], "then": [], "because": []}],
    "steps": [],
    "avoid": []
  },
  "applicability": {"conditions": [], "prerequisites": [], "exceptions": [], "boundaries": []},
  "outcome": {"expected": [], "success_signals": [], "failure_signals": []},
  "evidence": {"observations": [], "counterexamples": [], "validation_methods": [], "confidence": 0.0},
  "indexing": {"domains": [], "tasks": [], "tags": [], "aliases": [], "cross_domain_links": []},
  "knowledge_gaps": [{"question": "", "why_needed": ""}]
}

除 title、summary、分类、标签和 knowledge_gaps 外，数组中的每一项必须是：
{"text": "具体内容", "basis": "explicit|inferred", "confidence": 0.0}
decision_rules 中的 when、then、because 也使用这种条目。没有内容时返回空数组。"""

# 条目型列表字段（值应为 [{text,basis,confidence}, ...]）。validate 与 repair 共用同一份
# 路径清单，保证错误码路径与修复码路径永远同构；guidance.decision_rules[i].when/then/because
# 的条目位在两者中单独处理（不在本表内）。
_ENTRY_LIST_FIELDS = {
    "situation": ["goal", "triggers", "context", "entities"],
    "guidance": ["principles", "steps", "avoid"],
    "applicability": ["conditions", "prerequisites", "exceptions", "boundaries"],
    "outcome": ["expected", "success_signals", "failure_signals"],
    "evidence": ["observations", "counterexamples", "validation_methods"],
}


def item(text: str, basis: str = "explicit", confidence: float = 1.0) -> dict[str, Any]:
    return {"text": text, "basis": basis, "confidence": confidence}


def empty_record(raw_text: str = "") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {"raw_text": raw_text, "language": "zh-CN", "source_type": "human_explicit"},
        "identity": {"title": "", "summary": "", "primary_type": "principle", "secondary_types": []},
        "situation": {"goal": [], "triggers": [], "context": [], "entities": []},
        "guidance": {"principles": [], "decision_rules": [], "steps": [], "avoid": []},
        "applicability": {"conditions": [], "prerequisites": [], "exceptions": [], "boundaries": []},
        "outcome": {"expected": [], "success_signals": [], "failure_signals": []},
        "evidence": {"observations": [], "counterexamples": [], "validation_methods": [], "confidence": 0.0},
        "indexing": {"domains": [], "tasks": [], "tags": [], "aliases": [], "cross_domain_links": []},
        "knowledge_gaps": [],
    }


def parse_json_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("model_output_must_be_object")
    return value


def _copy_value(value: Any) -> Any:
    """深拷贝：dict/list 递归、tuple 归一为 list（JSON 无元组）、标量原样。"""
    if isinstance(value, dict):
        return {key: _copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_value(entry) for entry in value]
    if isinstance(value, tuple):
        return [_copy_value(entry) for entry in value]
    return value


def record_from_mapping(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    """形状补全（不做语义修复）：任何 Mapping 或 None -> 十字段齐全的规范形 record。

    - None -> empty_record("")；非 Mapping -> TypeError（鸭子类型边界显式化）。
    - 十个顶层键：缺/None -> empty_record 默认；值为 dict 的 section 与默认逐键浅合并
      （partial identity {"title"} 补出 summary/primary_type/secondary_types）；
      其余类型（错型 section 值、knowledge_gaps 的任意值、标量 schema_version）原样透传，
      错误留给 validate_record 报告，不静默吞。
    - 十键之外的未知键一律丢弃（validate 本就忽略未知键，保持 record 规范形）。
    - 输入不被改动；tuple 值经 _copy_value 归一为 list。
    """
    if mapping is None:
        return empty_record("")
    if not isinstance(mapping, Mapping):
        raise TypeError("record_from_mapping expects a Mapping or None")
    base = empty_record("")
    record: dict[str, Any] = {}
    for key in RECORD_FIELDS:
        if key not in mapping or mapping[key] is None:
            record[key] = base[key]
            continue
        value = _copy_value(mapping[key])
        default = base[key]
        if isinstance(value, dict) and isinstance(default, dict):
            merged = dict(default)
            merged.update(value)
            record[key] = merged
        else:
            record[key] = value
    return record


def repair_record(record: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """便宜修复（纯函数）：返回 (修复后深拷贝, applied_repairs)。

    只修「缺失」，绝不覆盖「错误值」，绝不把 basis 修成 explicit，绝不发明 text/question
    内容。修复码（固定、有序、确定性；登记词表，跨模块测试只允许引用这些码）：
      R1 fill_schema_version                 schema_version 缺失/None/空白 -> SCHEMA_VERSION
                                             （错值如 ".../v2" 不修，留给 validate）
      R2 upgrade_string_entries:{path}       条目位（_ENTRY_LIST_FIELDS 各列表 + guidance.
                                             decision_rules[i].when/then/because）上的非空白
                                             字符串 -> {"text": 原串, "basis": "inferred",
                                             "confidence": 0.5}；空白串不升级（两种下场都报
                                             invalid:{path}.text，升级无意义）
      R3 fill_entry_basis:{path}             dict 条目 basis 缺失/None/空白 -> "inferred"
                                             （错值如 "guess" 不修）
      R4 fill_entry_confidence:{path}        dict 条目 confidence 缺失/None -> 0.5
                                             （越界/错型不修——1.5 是必须保留的观测信号）
      R5 normalize_knowledge_gaps            knowledge_gaps 键存在且非 list -> []
                                             （顶层键缺失不修——那是 record_from_mapping 职责）
      R6 upgrade_string_gap:{i}              gap 项是非空白字符串 -> {"question": 原串,
                                             "why_needed": ""}
      R7 fill_gap_why_needed:{i}             dict gap 的 why_needed 缺失/None -> ""
                                             （question 缺失/非 str 不修）
    {path} 为 validate 同款的索引级路径（如 guidance.principles[0]）。执行序
    R1 -> R2/R3/R4（按 schema 字段序遍历）-> R5 -> R6/R7。
    """
    if not isinstance(record, Mapping):
        raise TypeError("repair_record expects a Mapping")
    repaired = _copy_value(dict(record))
    applied: list[str] = []

    schema_version = repaired.get("schema_version")
    if schema_version is None or (isinstance(schema_version, str) and not schema_version.strip()):
        repaired["schema_version"] = SCHEMA_VERSION
        applied.append("fill_schema_version")

    def repair_entry_list(container: dict[str, Any], field: str, path: str) -> None:
        values = container.get(field)
        if not isinstance(values, list):
            return
        for index, value in enumerate(values):
            entry_path = f"{path}[{index}]"
            if isinstance(value, str):
                if value.strip():
                    values[index] = {"text": value, "basis": "inferred", "confidence": 0.5}
                    applied.append(f"upgrade_string_entries:{entry_path}")
                continue
            if not isinstance(value, dict):
                continue
            basis = value.get("basis")
            if basis is None or (isinstance(basis, str) and not basis.strip()):
                value["basis"] = "inferred"
                applied.append(f"fill_entry_basis:{entry_path}")
            confidence = value.get("confidence")
            if confidence is None:
                value["confidence"] = 0.5
                applied.append(f"fill_entry_confidence:{entry_path}")

    for section, fields in _ENTRY_LIST_FIELDS.items():
        section_value = repaired.get(section)
        if not isinstance(section_value, dict):
            continue
        for field in fields:
            repair_entry_list(section_value, field, f"{section}.{field}")
        if section == "guidance":
            rules = section_value.get("decision_rules")
            if isinstance(rules, list):
                for rule_index, rule in enumerate(rules):
                    if not isinstance(rule, dict):
                        continue
                    for field in ("when", "then", "because"):
                        repair_entry_list(rule, field, f"guidance.decision_rules[{rule_index}].{field}")

    gaps = repaired.get("knowledge_gaps")
    if "knowledge_gaps" in repaired and not isinstance(gaps, list):
        repaired["knowledge_gaps"] = []
        applied.append("normalize_knowledge_gaps")
    if isinstance(gaps, list):
        for index, gap in enumerate(gaps):
            if isinstance(gap, str):
                if gap.strip():
                    gaps[index] = {"question": gap, "why_needed": ""}
                    applied.append(f"upgrade_string_gap:{index}")
                continue
            if not isinstance(gap, dict):
                continue
            if "why_needed" not in gap or gap["why_needed"] is None:
                gap["why_needed"] = ""
                applied.append(f"fill_gap_why_needed:{index}")

    return repaired, applied


def validate_record(record: dict[str, Any], raw_text: str) -> list[str]:
    """校验 record 是否为合法 experience-standard/v1 记录，返回错误码列表（词表见下）。

    纯函数：零 I/O、不改输入。raw_text 是「该经验声称来自的原文」，必须与
    record["source"]["raw_text"] 逐字节一致（全文件唯一原文关联）。

    错误码词表（B 侧原生全套原样保留 + 新增 2 码，登记处，跨模块测试只引用登记内码）：
      missing:{key}                          十顶层键缺失（缺任一键则短路返回）
      invalid:schema_version                 != SCHEMA_VERSION
      invalid:source.raw_text                source 非 dict 或 raw_text 与第二参不一致
      invalid:identity.primary_type          identity 非 dict 或 primary_type 不在枚举
      invalid:identity.secondary_types       键存在但非 list
      invalid:identity.secondary_types[{i}]  元素非 str 或不在 PRIMARY_TYPES（新增 2 码）
      invalid:identity.{title,summary}       非空 str 校验
      invalid:evidence.confidence            非 int/float 或越界 [0,1]
      invalid:{section}                      section 非 dict
      missing:{section}.{field}              section 缺必填字段
      invalid:{section}.{field}              字段值非 list
      invalid:{path}.text/.basis/.confidence 条目形状校验（path 含索引）
      invalid:guidance.decision_rules([i])?  规则形状校验（含 .when/.then/.because）
      invalid:indexing.{field}               非 list 或含非空白 str 之外的元素
      invalid:knowledge_gaps([{i}])?         gaps 非 list / 单项形状坏
    """
    errors: list[str] = []
    for key in RECORD_FIELDS:
        if key not in record:
            errors.append(f"missing:{key}")
    if errors:
        return errors
    if record["schema_version"] != SCHEMA_VERSION:
        errors.append("invalid:schema_version")
    if not isinstance(record["source"], dict) or record["source"].get("raw_text") != raw_text:
        errors.append("invalid:source.raw_text")
    if not isinstance(record["identity"], dict) or record["identity"].get("primary_type") not in PRIMARY_TYPES:
        errors.append("invalid:identity.primary_type")
    identity_section = record["identity"]
    # 设防 1：secondary_types 补校验（B 原版只查 primary_type，错型元素静默通过）。
    # 键缺席不报错——保持 B 对缺席的宽松，且不要求构造方必须产出该键。
    if isinstance(identity_section, dict) and "secondary_types" in identity_section:
        secondary = identity_section["secondary_types"]
        if not isinstance(secondary, list):
            errors.append("invalid:identity.secondary_types")
        else:
            for index, value in enumerate(secondary):
                if not isinstance(value, str) or value not in PRIMARY_TYPES:
                    errors.append(f"invalid:identity.secondary_types[{index}]")
    # 设防 2：identity 非 dict 时原版 .get 链会 AttributeError——此处跳过
    # （primary_type 已报 invalid:identity.primary_type），错误码不变。
    if isinstance(identity_section, dict):
        for field in ("title", "summary"):
            if not isinstance(identity_section.get(field), str) or not identity_section[field].strip():
                errors.append(f"invalid:identity.{field}")
    # 设防 3：evidence 非 dict（如 []）时原版在 .get("confidence") 处崩溃——跳过，
    # required_fields 循环会报唯一一条 invalid:evidence，不产生重复错误码。
    if isinstance(record["evidence"], dict):
        confidence = record["evidence"].get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append("invalid:evidence.confidence")

    required_fields = {
        **_ENTRY_LIST_FIELDS,
        "guidance": ["principles", "decision_rules", "steps", "avoid"],
        "indexing": ["domains", "tasks", "tags", "aliases", "cross_domain_links"],
    }
    for section, fields in required_fields.items():
        value = record.get(section)
        if not isinstance(value, dict):
            errors.append(f"invalid:{section}")
            continue
        for field in fields:
            if field not in value:
                errors.append(f"missing:{section}.{field}")

    def check_item(value: Any, path: str) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("text"), str) or not value["text"].strip():
            errors.append(f"invalid:{path}.text")
            return
        if value.get("basis") not in ITEM_BASIS:
            errors.append(f"invalid:{path}.basis")
        score = value.get("confidence")
        if not isinstance(score, (int, float)) or not 0 <= score <= 1:
            errors.append(f"invalid:{path}.confidence")

    for section, fields in _ENTRY_LIST_FIELDS.items():
        section_value = record.get(section)
        # 设防 4：section 非 dict 时原版 .get 链崩溃——跳过（invalid:{section} 已报）。
        if not isinstance(section_value, dict):
            continue
        for field in fields:
            values = section_value.get(field, [])
            if not isinstance(values, list):
                errors.append(f"invalid:{section}.{field}")
                continue
            for index, value in enumerate(values):
                check_item(value, f"{section}.{field}[{index}]")

    # 设防 5：guidance 非 dict 时原版 .get 链崩溃——整段跳过（invalid:guidance 已报）。
    guidance_section = record.get("guidance")
    if isinstance(guidance_section, dict):
        rules = guidance_section.get("decision_rules", [])
        if not isinstance(rules, list):
            errors.append("invalid:guidance.decision_rules")
        else:
            for rule_index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    errors.append(f"invalid:guidance.decision_rules[{rule_index}]")
                    continue
                for field in ("when", "then", "because"):
                    values = rule.get(field)
                    if not isinstance(values, list):
                        errors.append(f"invalid:guidance.decision_rules[{rule_index}].{field}")
                        continue
                    for item_index, value in enumerate(values):
                        check_item(value, f"guidance.decision_rules[{rule_index}].{field}[{item_index}]")

    # 设防 6：indexing 非 dict 时原版 .get 链崩溃——跳过（invalid:indexing 已报）。
    indexing_section = record.get("indexing")
    if isinstance(indexing_section, dict):
        for field in ("domains", "tasks", "tags", "aliases", "cross_domain_links"):
            values = indexing_section.get(field, [])
            if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
                errors.append(f"invalid:indexing.{field}")

    gaps = record.get("knowledge_gaps")
    if not isinstance(gaps, list):
        errors.append("invalid:knowledge_gaps")
    else:
        for index, gap in enumerate(gaps):
            if not isinstance(gap, dict) or not isinstance(gap.get("question"), str) or not gap["question"].strip() or not isinstance(gap.get("why_needed"), str):
                errors.append(f"invalid:knowledge_gaps[{index}]")
    return errors


# ---- 分词器：agents/skills.py _TOKEN_RE/_STOP_TOKENS/_token_list 的逐字副本（互锁测试钉死）----

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{1,2}")
_STOP_TOKENS = {
    "请帮",
    "帮我",
    "我做",
    "做一",
    "一次",
    "一下",
    "这个",
    "那个",
    "一个",
    "用户",
    "问题",
    "回答",
    "生成",
    "使用",
    "需要",
}


def default_tokenize(text: str) -> list[str]:
    raw = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens = [m.group(0) for m in _TOKEN_RE.finditer(raw)]
    for chunk in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(chunk) >= 2:
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    expanded: list[str] = []
    for token in tokens:
        if not token.strip() or token in _STOP_TOKENS:
            continue
        expanded.append(token)
        if len(token) > 3 and token.endswith("s"):
            expanded.append(token[:-1])
    return expanded


def grounding_overlap(item_text: str, window_text: str, *, tokenizer: Callable[[str], list[str]] | None = None) -> int:
    """item 与窗口在去重后词元集上的共享数；item 无词元（如全停用词）-> 0。"""
    tokenize = tokenizer if tokenizer is not None else default_tokenize
    item_tokens = set(tokenize(str(item_text or "")))
    if not item_tokens:
        return 0
    window_tokens = set(tokenize(str(window_text or "")))
    return len(item_tokens & window_tokens)


def grounding_flag(item_text: str, window_text: str, *, tokenizer: Callable[[str], list[str]] | None = None) -> bool:
    """≥1 共享词元原语（grounding 原语，不揽语义）；误拦率校准由调用方用 grounding_overlap 做。"""
    return grounding_overlap(item_text, window_text, tokenizer=tokenizer) > 0
