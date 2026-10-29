"""经验标准 IR 回归测试：schema 常量 + 校验/修复/形状补全 + grounding 原语 + 分词器副本互锁。

覆盖 agents/experience_standard.py（移植自 experience-capture experience-standard/v1）：
- golden 互锁：两条 golden 记录不经任何适配直接过 validate_record（决策书 item 1 反平凡满足）
- 设防回归：B 侧六处对错型 section 会 AttributeError 崩溃的路径，现返回错误码而非异常
- repair 七码：只修缺失、绝不覆盖错误值、绝不把 basis 修成 explicit、绝不发明内容
- 分词器副本：default_tokenize 与 agents.skills._token_list 逐条等价（维护互锁，
  A 侧改分词器则本文件变红要求同步）
- import 白名单：模块仅 import {json, re, typing}，无 agents.* 引用，无 render_markdown

测试文件 import agents.skills 属测试行为（决策书明示），不破坏被测模块的运行时零依赖。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.experience_standard as experience_standard
from agents.experience_standard import (
    PRIMARY_TYPES,
    RECORD_FIELDS,
    SCHEMA_VERSION,
    default_tokenize,
    empty_record,
    grounding_flag,
    grounding_overlap,
    item,
    parse_json_content,
    record_from_mapping,
    repair_record,
    validate_record,
)
from agents.skills import _token_list

RAW = "用户原文：用于关联校验的原始经验叙述。"

# ---- golden 内联（测试不得依赖 experience-capture 的外部路径）----

# zh golden：B 侧 examples/constitutive-model-selection.v1.json 内容同构移植（值级一致）。
GOLDEN_ZH = {
    "schema_version": "experience-standard/v1",
    "source": {
        "raw_text": "仿真中本构的选择要根据材料、损伤预期来决定，断裂和拉伸用的本构是不一样的。",
        "language": "zh-CN",
        "source_type": "human_explicit",
    },
    "identity": {
        "title": "依据材料与损伤预期选择仿真本构",
        "summary": "本构选择需要匹配材料和预期损伤模式，断裂与拉伸场景不能默认使用同一套本构描述。",
        "primary_type": "decision_rule",
        "secondary_types": ["principle", "constraint"],
    },
    "situation": {
        "goal": [{"text": "为仿真场景选择合适的本构", "basis": "explicit", "confidence": 0.95}],
        "triggers": [{"text": "需要建立材料模型", "basis": "inferred", "confidence": 0.75}],
        "context": [{"text": "工程仿真", "basis": "explicit", "confidence": 0.98}],
        "entities": [
            {"text": "材料", "basis": "explicit", "confidence": 0.99},
            {"text": "损伤预期", "basis": "explicit", "confidence": 0.99},
            {"text": "本构", "basis": "explicit", "confidence": 0.99},
        ],
    },
    "guidance": {
        "principles": [
            {"text": "本构选择需要同时考虑材料与损伤预期", "basis": "explicit", "confidence": 0.99}
        ],
        "decision_rules": [
            {
                "when": [{"text": "预期主要关注断裂行为", "basis": "explicit", "confidence": 0.9}],
                "then": [{"text": "选择或校核适用于断裂场景的本构", "basis": "inferred", "confidence": 0.8}],
                "because": [{"text": "断裂与拉伸使用的本构不同", "basis": "explicit", "confidence": 0.98}],
            }
        ],
        "steps": [
            {"text": "确认材料类型", "basis": "inferred", "confidence": 0.8},
            {"text": "确认预期损伤模式", "basis": "inferred", "confidence": 0.8},
            {"text": "据此选择并校核本构", "basis": "inferred", "confidence": 0.8},
        ],
        "avoid": [
            {"text": "不区分断裂与拉伸场景直接复用同一本构", "basis": "inferred", "confidence": 0.82}
        ],
    },
    "applicability": {
        "conditions": [{"text": "正在进行涉及材料响应的仿真", "basis": "inferred", "confidence": 0.75}],
        "prerequisites": [{"text": "材料和预期损伤模式能够被识别", "basis": "inferred", "confidence": 0.72}],
        "exceptions": [],
        "boundaries": [{"text": "原文没有给出具体本构名称或参数", "basis": "inferred", "confidence": 0.95}],
    },
    "outcome": {
        "expected": [{"text": "本构与材料和损伤预期相匹配", "basis": "inferred", "confidence": 0.82}],
        "success_signals": [],
        "failure_signals": [{"text": "模型无法合理描述目标损伤模式", "basis": "inferred", "confidence": 0.65}],
    },
    "evidence": {
        "observations": [{"text": "人类明确指出断裂与拉伸所用本构不同", "basis": "explicit", "confidence": 0.99}],
        "counterexamples": [],
        "validation_methods": [],
        "confidence": 0.94,
    },
    "indexing": {
        "domains": ["工程仿真", "材料力学"],
        "tasks": ["本构选择", "材料建模"],
        "tags": ["本构模型", "材料", "损伤", "断裂", "拉伸"],
        "aliases": ["材料模型选择"],
        "cross_domain_links": ["有限元分析", "失效分析"],
    },
    "knowledge_gaps": [
        {"question": "具体材料、载荷条件和目标损伤模式是什么？", "why_needed": "用于进一步确定可选本构及其参数标定方法"},
        {"question": "如何验证所选本构能够描述目标响应？", "why_needed": "原文没有提供验证标准"},
    ],
}

# coding 域 golden：素材取自本仓真实教训——frontmatter 中文冒号静默丢字段
# （tests/test_skill_load_errors.py docstring 与 agents/skills.py 的 record_skill_load_issue）。
# 覆盖空规则分支（decision_rules=[]）与 secondary_types 键存在且合法两个路径。
GOLDEN_CODING = {
    "schema_version": "experience-standard/v1",
    "source": {
        "raw_text": "复盘：SKILL.md 的 frontmatter 里 name 写成了中文冒号，frontmatter.py 按英文冒号定位分隔符时找不到就整行跳过，技能名称静默丢失，检索里这个 skill 像消失了一样，也没有任何报错日志。",
        "language": "zh-CN",
        "source_type": "human_explicit",
    },
    "identity": {
        "title": "frontmatter 中文冒号导致字段静默丢失",
        "summary": "SKILL.md frontmatter 使用中文冒号时，按英文冒号定位的解析器会整行跳过该字段，字段静默丢失且无报错，必须用英文冒号加空格书写。",
        "primary_type": "failure_pattern",
        "secondary_types": ["diagnostic"],
    },
    "situation": {
        "goal": [{"text": "让 SKILL.md frontmatter 字段被可靠解析", "basis": "explicit", "confidence": 0.95}],
        "triggers": [{"text": "技能名称或描述在列表里凭空缺失", "basis": "inferred", "confidence": 0.7}],
        "context": [{"text": "EvoHarness 技能加载链路", "basis": "explicit", "confidence": 0.9}],
        "entities": [
            {"text": "SKILL.md", "basis": "explicit", "confidence": 0.95},
            {"text": "frontmatter", "basis": "explicit", "confidence": 0.95},
        ],
    },
    "guidance": {
        "principles": [
            {"text": "frontmatter 字段必须用英文冒号加空格，中文冒号会被按行跳过", "basis": "explicit", "confidence": 0.95}
        ],
        "decision_rules": [],
        "steps": [
            {"text": "用 utf-8-sig 读取 SKILL.md 复现解析结果", "basis": "inferred", "confidence": 0.75},
            {"text": "逐行检查 frontmatter 是否存在中文冒号", "basis": "inferred", "confidence": 0.8},
        ],
        "avoid": [
            {"text": "不要假设用户输入的 frontmatter 一定是英文标点", "basis": "inferred", "confidence": 0.7}
        ],
    },
    "applicability": {
        "conditions": [{"text": "解析器按英文冒号定位键值分隔", "basis": "explicit", "confidence": 0.85}],
        "prerequisites": [],
        "exceptions": [],
        "boundaries": [{"text": "只覆盖冒号类故障，编码错误走 load_failed 通道", "basis": "inferred", "confidence": 0.65}],
    },
    "outcome": {
        "expected": [{"text": "字段不再静默丢失，坏行进入 load_warning 溯源记录", "basis": "inferred", "confidence": 0.8}],
        "success_signals": [{"text": "skill_load_errors.jsonl 中出现中文冒号的 load_warning 记录", "basis": "inferred", "confidence": 0.7}],
        "failure_signals": [],
    },
    "evidence": {
        "observations": [{"text": "tests/test_skill_load_errors.py 固化了中文冒号跳过行为", "basis": "explicit", "confidence": 0.9}],
        "counterexamples": [],
        "validation_methods": [],
        "confidence": 0.85,
    },
    "indexing": {
        "domains": ["coding-agent", "skill 工程"],
        "tasks": ["技能排障", "frontmatter 修复"],
        "tags": ["frontmatter", "中文冒号", "静默失败"],
        "aliases": ["colon 字段丢失"],
        "cross_domain_links": ["配置文件解析"],
    },
    "knowledge_gaps": [],
}


# ---- 夹具 ----

def _base_record() -> dict:
    """最小合法记录骨架：empty_record + 非空 title/summary，validate == []。"""
    record = empty_record(RAW)
    record["identity"]["title"] = "标题"
    record["identity"]["summary"] = "摘要"
    return record


def _legacy_mixed_record() -> dict:
    """集齐全部可修复缺陷的 legacy 记录：缺 schema_version、字符串条目、字符串 gap、缺 why_needed。"""
    return {
        "source": {"raw_text": RAW, "language": "zh-CN", "source_type": "human_explicit"},
        "identity": {"title": "旧格式经验", "summary": "含各类可修复缺陷的候选", "primary_type": "heuristic"},
        "situation": {"goal": ["明确目标"], "triggers": [], "context": ["在线沉淀"], "entities": []},
        "guidance": {"principles": ["先读后写"], "decision_rules": [], "steps": [], "avoid": []},
        "applicability": {"conditions": [], "prerequisites": [], "exceptions": [], "boundaries": []},
        "outcome": {"expected": [], "success_signals": [], "failure_signals": []},
        "evidence": {"observations": ["人工确认"], "counterexamples": [], "validation_methods": [], "confidence": 0.9},
        "indexing": {"domains": ["测试域"], "tasks": [], "tags": ["legacy"], "aliases": [], "cross_domain_links": []},
        "knowledge_gaps": ["还缺什么证据？", {"question": "如何验证？"}],
        # schema_version 故意缺失（R1）
    }


# ---- golden 互锁 ----

def test_golden_zh_simulation_zero_errors():
    # 互锁条款：golden 不经适配直接调 validate_record；zh 半边 = B 侧 examples 内容同构移植
    assert GOLDEN_ZH["schema_version"] == SCHEMA_VERSION
    assert validate_record(GOLDEN_ZH, GOLDEN_ZH["source"]["raw_text"]) == []


def test_golden_coding_agent_zero_errors():
    assert validate_record(GOLDEN_CODING, GOLDEN_CODING["source"]["raw_text"]) == []
    assert GOLDEN_CODING["guidance"]["decision_rules"] == []      # 空规则分支被覆盖
    assert GOLDEN_CODING["identity"]["primary_type"] == "failure_pattern"
    assert GOLDEN_CODING["identity"]["secondary_types"] == ["diagnostic"]


# ---- primary_type 全枚举 ----

@pytest.mark.parametrize("primary_type", sorted(PRIMARY_TYPES))
def test_all_nine_primary_types_minimal_valid(primary_type):
    record = _base_record()
    record["identity"]["primary_type"] = primary_type
    assert validate_record(record, RAW) == []


# ---- 畸形变体：全部报错码、零崩溃 ----

MALFORMED_VARIANTS = [
    # (变体名, 变异函数, 期望错误码子串；⑭ 用另一段原文调 validate)
    ("①条目缺basis", lambda r: r["guidance"].update(principles=[{"text": "内容", "confidence": 0.9}]),
     ("invalid:guidance.principles[0].basis",)),
    ("②confidence越界", lambda r: r["guidance"].update(principles=[{"text": "内容", "basis": "explicit", "confidence": 1.5}]),
     ("invalid:guidance.principles[0].confidence",)),
    ("③evidence非dict", lambda r: r.update(evidence=[]),
     ("invalid:evidence",)),
    ("④guidance非dict", lambda r: r.update(guidance="oops"),
     ("invalid:guidance",)),
    ("⑤secondary_types错型元素", lambda r: r["identity"].update(secondary_types=[123, "not_a_type"]),
     ("invalid:identity.secondary_types[0]", "invalid:identity.secondary_types[1]")),
    ("⑥规则缺then", lambda r: r["guidance"].update(decision_rules=[{"when": [], "because": []}]),
     ("invalid:guidance.decision_rules[0].then",)),
    ("⑦identity非dict", lambda r: r.update(identity="oops"),
     ("invalid:identity.primary_type",)),
    ("⑧indexing非dict", lambda r: r.update(indexing="oops"),
     ("invalid:indexing",)),
    ("⑨gap问题非str", lambda r: r.update(knowledge_gaps=[{"question": 123}]),
     ("invalid:knowledge_gaps[0]",)),
    ("⑩条目text空串", lambda r: r["guidance"].update(principles=[{"text": "", "basis": "explicit", "confidence": 1.0}]),
     ("invalid:guidance.principles[0].text",)),
    ("⑪tags含空串", lambda r: r["indexing"].update(tags=[""]),
     ("invalid:indexing.tags",)),
    ("⑫schema_version错值", lambda r: r.update(schema_version="experience-standard/v2"),
     ("invalid:schema_version",)),
    ("⑬缺顶层outcome", lambda r: r.pop("outcome"),
     ("missing:outcome",)),
    ("⑭raw_text不一致", lambda r: None,
     ("invalid:source.raw_text",)),
]


@pytest.mark.parametrize("variant_id, mutate, expected", MALFORMED_VARIANTS, ids=[v[0] for v in MALFORMED_VARIANTS])
def test_malformed_variants_all_caught_without_crash(variant_id, mutate, expected):
    record = _base_record()
    mutate(record)
    raw_text = "另一段原文" if variant_id.startswith("⑭") else RAW
    errors = validate_record(record, raw_text)          # 不抛异常即通过六处设防
    assert isinstance(errors, list) and errors          # 非空 list[str]，catch_rate 累计 == 1.0
    for code in expected:
        assert code in errors, (variant_id, errors)


# ---- repair 七码 ----

def test_repair_fills_missing_basis_confidence_and_schema_version():
    record = _base_record()
    del record["schema_version"]                                                        # R1：缺失
    record["guidance"]["principles"] = [{"text": "内容"}]                                # R3+R4：双缺
    record["guidance"]["steps"] = [{"text": "步骤", "basis": "  ", "confidence": None}]  # R3：空白；R4：None
    repaired, applied = repair_record(record)

    assert repaired["schema_version"] == SCHEMA_VERSION
    assert repaired["guidance"]["principles"][0] == {"text": "内容", "basis": "inferred", "confidence": 0.5}
    assert repaired["guidance"]["steps"][0] == {"text": "步骤", "basis": "inferred", "confidence": 0.5}
    assert "fill_schema_version" in applied
    assert "fill_entry_basis:guidance.principles[0]" in applied
    assert "fill_entry_confidence:guidance.principles[0]" in applied
    assert "fill_entry_basis:guidance.steps[0]" in applied
    assert "fill_entry_confidence:guidance.steps[0]" in applied
    # R1 空白变体
    record2 = _base_record()
    record2["schema_version"] = "   "
    repaired2, applied2 = repair_record(record2)
    assert repaired2["schema_version"] == SCHEMA_VERSION and "fill_schema_version" in applied2
    # 修复后 validate 全绿（repair_then_validate_rate 可修复半边）
    assert validate_record(repaired, RAW) == []


def test_repair_upgrades_legacy_string_entries():
    record = _base_record()
    record["situation"]["goal"] = ["选出本构"]
    record["guidance"]["principles"] = ["先看材料", "再看损伤"]
    record["guidance"]["decision_rules"] = [
        {"when": ["预期断裂"], "then": [item("选断裂本构")], "because": []}
    ]
    repaired, applied = repair_record(record)

    assert repaired["situation"]["goal"] == [{"text": "选出本构", "basis": "inferred", "confidence": 0.5}]
    assert repaired["guidance"]["principles"] == [
        {"text": "先看材料", "basis": "inferred", "confidence": 0.5},
        {"text": "再看损伤", "basis": "inferred", "confidence": 0.5},
    ]
    assert repaired["guidance"]["decision_rules"][0]["when"] == [{"text": "预期断裂", "basis": "inferred", "confidence": 0.5}]
    # 码带索引级路径（M2 按前缀 upgrade_string_entries: 断言兼容）
    assert "upgrade_string_entries:situation.goal[0]" in applied
    assert "upgrade_string_entries:guidance.principles[0]" in applied
    assert "upgrade_string_entries:guidance.principles[1]" in applied
    assert "upgrade_string_entries:guidance.decision_rules[0].when[0]" in applied
    # 空白字符串不升级（两种下场都报 invalid:{path}.text，升级无意义）
    record2 = _base_record()
    record2["guidance"]["avoid"] = ["   "]
    repaired2, applied2 = repair_record(record2)
    assert repaired2["guidance"]["avoid"] == ["   "]
    assert not any(code.startswith("upgrade_string_entries:") for code in applied2)
    assert validate_record(repaired, RAW) == []          # 升级产物直接过校验


def test_repair_normalizes_knowledge_gaps():
    record = _base_record()
    record["knowledge_gaps"] = "oops"                    # R5：非 list
    repaired, applied = repair_record(record)
    assert repaired["knowledge_gaps"] == []
    assert "normalize_knowledge_gaps" in applied

    record2 = _base_record()
    record2["knowledge_gaps"] = ["还缺什么证据？", {"question": "如何验证？"}]   # R6 + R7
    repaired2, applied2 = repair_record(record2)
    assert repaired2["knowledge_gaps"] == [
        {"question": "还缺什么证据？", "why_needed": ""},
        {"question": "如何验证？", "why_needed": ""},
    ]
    assert "upgrade_string_gap:0" in applied2
    assert "fill_gap_why_needed:1" in applied2
    assert validate_record(repaired2, RAW) == []

    record3 = _base_record()
    record3["knowledge_gaps"] = ["   "]                  # 空白 gap 不升级
    repaired3, applied3 = repair_record(record3)
    assert repaired3["knowledge_gaps"] == ["   "]
    assert not any(code.startswith("upgrade_string_gap:") for code in applied3)


def test_repair_never_touches_unrepairable():
    record = _base_record()
    record["guidance"]["principles"] = [
        {"text": "真内容", "basis": "guess", "confidence": 1.5},   # 错值 basis / 越界 confidence 均不修
        {"text": "", "basis": "explicit", "confidence": 1.0},      # 空 text 不修
    ]
    record["guidance"]["decision_rules"] = [{"when": [], "because": []}]   # 缺 then 不修
    record["schema_version"] = "experience-standard/v2"            # 错值不修
    record["knowledge_gaps"] = [{"why_needed": "缺问题的 gap"}]     # question 缺失不修
    repaired, applied = repair_record(record)

    assert repaired["guidance"]["principles"][0] == {"text": "真内容", "basis": "guess", "confidence": 1.5}
    assert repaired["guidance"]["principles"][1] == {"text": "", "basis": "explicit", "confidence": 1.0}
    assert repaired["guidance"]["decision_rules"] == [{"when": [], "because": []}]
    assert repaired["schema_version"] == "experience-standard/v2"
    assert repaired["knowledge_gaps"] == [{"why_needed": "缺问题的 gap"}]
    assert applied == []   # 全部不可修复 → 无任何修复码（basis 永不被改写，更不会被修成 explicit）


def test_repair_record_is_pure_and_deterministic():
    record = _legacy_mixed_record()
    snapshot = copy.deepcopy(record)
    repaired1, applied1 = repair_record(record)
    assert record == snapshot                                # 输入深比对不变
    repaired2, applied2 = repair_record(snapshot)
    assert repaired1 == repaired2 and applied1 == applied2   # 两次运行逐项相等


def test_repair_then_validate_zero_errors_mixed():
    repaired, applied = repair_record(_legacy_mixed_record())
    assert validate_record(repaired, RAW) == []              # 决策书 item 7 通过率=1.0 的 M1 侧断言
    assert "fill_schema_version" in applied
    assert "upgrade_string_entries:situation.goal[0]" in applied
    assert "upgrade_string_entries:situation.context[0]" in applied
    assert "upgrade_string_entries:guidance.principles[0]" in applied
    assert "upgrade_string_entries:evidence.observations[0]" in applied
    assert "upgrade_string_gap:0" in applied
    assert "fill_gap_why_needed:1" in applied


# ---- record_from_mapping 形状补全 ----

def test_record_from_mapping_completes_ten_fields():
    record = record_from_mapping({"identity": {"title": "仅标题"}, "unknown_key": {"x": 1}})
    assert set(record.keys()) == set(RECORD_FIELDS)
    assert record["identity"] == {"title": "仅标题", "summary": "", "primary_type": "principle", "secondary_types": []}
    assert record["situation"] == empty_record("")["situation"]
    assert "unknown_key" not in record                       # 未知键一律丢弃
    assert record_from_mapping({}) == empty_record("")       # 十键全缺 -> canonical 空骨架


def test_record_from_mapping_passthrough_and_purity():
    mapping = {"situation": "oops", "indexing": {"tags": ("a", "b")}}
    snapshot = copy.deepcopy(mapping)
    record = record_from_mapping(mapping)
    assert record["situation"] == "oops"                     # 错型 section 原样透传
    assert record["indexing"]["tags"] == ["a", "b"]          # tuple -> list
    assert record["indexing"]["domains"] == []               # 缺键补默认
    assert mapping == snapshot                               # 输入不变
    errors = validate_record(record, "")
    assert "invalid:situation" in errors                     # 错误留给 validate，不静默吞
    assert "invalid:indexing" not in errors                  # tags 归一后已是合法 list[str]
    assert record_from_mapping(None) == empty_record("")


@pytest.mark.parametrize("bad_input", [["x"], "oops", 123])
def test_record_mapping_inputs_reject_non_mapping(bad_input):
    with pytest.raises(TypeError):
        record_from_mapping(bad_input)
    with pytest.raises(TypeError):
        repair_record(bad_input)


# ---- grounding 原语 ----

def test_grounding_flag_semantics():
    window = "仿真中本构的选择要根据材料、损伤预期来决定，断裂和拉伸用的本构是不一样的。"
    real = "断裂和拉伸用的本构是不一样的"            # 原文子串：全部词元与窗口共享
    fabricated = "模型部署前必须清理显卡缓存"         # 编造条目：与窗口零共享词元
    assert grounding_overlap(real, window) == 13     # 实测 13 个共享 CJK bigram（探针钉死）
    assert grounding_flag(real, window) is True
    assert grounding_overlap(fabricated, window) == 0
    assert grounding_flag(fabricated, window) is False
    # 纯停用词：全部 bigram 均命中停用词表 -> item 无词元 -> 0
    assert grounding_overlap("请帮我做一次", window) == 0
    assert grounding_flag("请帮我做一次", window) is False
    # 词元非空（{帮帮, 我}）但与窗口零共享 -> 仍 False
    assert grounding_flag("请帮帮帮我", window) is False


def test_grounding_flag_custom_tokenizer():
    custom = lambda t: t.split()
    # 默认分词器把下划线归一为空格并小写 -> 共享；自定义 split() 不归一 -> 零共享（钩子被尊重）
    assert grounding_flag("Skill_Loader", "skill loader") is True
    assert grounding_flag("Skill_Loader", "skill loader", tokenizer=custom) is False
    assert grounding_overlap("Skill_Loader", "skill loader", tokenizer=custom) == 0
    assert grounding_overlap("gamma delta", "gamma beta", tokenizer=custom) == 1   # 供 M4 校准注入


# ---- 分词器副本互锁 ----

TOKENIZER_CORPUS = [
    "Fix the skill_loader bug quickly",        # 下划线 + 英文
    "skill-repair 的 tags 已更新 skills",       # 连字符 + 复数 s
    "本构选择要根据材料与损伤预期",              # 纯 CJK
    "skills",                                   # 复数 s 展开
    "",                                         # 空串
    "2026-10-11 修订 v2",                       # 日期数字
    "请帮我做一次这个回答生成",                  # 停用词
]


@pytest.mark.parametrize("text", TOKENIZER_CORPUS)
def test_default_tokenize_equals_agents_skills_token_list(text):
    # 决策书钦定的唯一等价断言：A 侧改 _token_list/_STOP_TOKENS 则本测试变红要求同步副本
    assert default_tokenize(text) == _token_list(text)


# ---- 模块级纪律 ----

def test_module_imports_stdlib_only():
    import ast

    source = Path(experience_standard.__file__).read_text(encoding="utf-8")
    imported_roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "禁止相对导入"
            if node.module != "__future__":
                imported_roots.add((node.module or "").split(".")[0])
    assert imported_roots <= {"json", "re", "typing"}, imported_roots
    assert "agents" not in imported_roots          # 无 agents.* 引用（白名单已保证，显式双保险）


def test_validate_record_pure_no_mutation():
    record = _base_record()
    record["guidance"]["principles"] = [{"text": "内容", "confidence": 0.9}]
    record["identity"]["secondary_types"] = [123]
    snapshot = copy.deepcopy(record)
    errors1 = validate_record(record, RAW)
    errors2 = validate_record(record, RAW)
    assert record == snapshot                       # 输入不变
    assert errors1 == errors2 and errors1           # 两次调用一致且非空


def test_no_render_markdown_export():
    assert not hasattr(experience_standard, "render_markdown")
    source = Path(experience_standard.__file__).read_text(encoding="utf-8")
    assert "render_markdown" not in source          # 决策书 item 5 reject 的仓库内冗余守卫


def test_parse_json_content_and_item_helpers():
    assert item("内容") == {"text": "内容", "basis": "explicit", "confidence": 1.0}
    assert item("内容", "inferred", 0.5) == {"text": "内容", "basis": "inferred", "confidence": 0.5}
    assert parse_json_content('{"a": 1}') == {"a": 1}
    assert parse_json_content('```json\n{"a": 1}\n```') == {"a": 1}   # 剥 ``` 围栏
    assert parse_json_content('```\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_content("[1, 2]")      # 合法 JSON 但非对象 -> model_output_must_be_object
    with pytest.raises(ValueError):
        parse_json_content("not json")    # JSONDecodeError 是 ValueError 子类
