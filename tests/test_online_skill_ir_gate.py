"""M2 沉淀链路 IR 门回归测试：off 逐字符兼容 + annotate 观测 + strict 拦截 + 区渲染/剥除。

覆盖 agents/online_skill_evolution.py 的 IR 门接入（EVOHARNESS_IR_MODE=off|annotate|strict）：
- off 模式：extract 提示词、maintain 提示词、SKILL.md 写盘、provenance 行逐字符回现网（F-3：provenance 零 ir_* 注入）
- annotate：只观测（basis 不改写、errors/grounding 只落 provenance），无 gaps/rules 不新增正文区
- strict：先修复再降级后拦截（ir_rejected 在 confirm_write 之前，不写盘）
- _with_ir_sections：先剥除 maintainer 回显的两区再从 ir_record 权威渲染（单区不变量，硬伤 1）
- fail-open：M1 缺失/运行时异常降级完全旧路径 + provenance 留痕，绝不外抛（应修 b）

沙箱纪律（蓝图 U1）：monkeypatch.chdir + Path.home 补丁 + 前后 reset_skill_cache()，
off-vs-annotate 比对用双沙箱（同测试内 chdir 到不同子目录并重置进程内缓存）。
逐字节比对一律用 frontmatter 结束分隔符后的原始切片（parse_frontmatter().body 会 strip，不能用）；
result 级比对弹 ir_* 键（F-4 口径）与易变键（time/file 等含沙箱绝对路径）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import online_skill_evolution as ose
from agents.skills import create_skill, reset_skill_cache

try:
    from agents import experience_standard as _m1

    HAS_M1 = True
except Exception:  # pragma: no cover - M1 未落地时的固定形态
    _m1 = None
    HAS_M1 = False

requires_m1 = pytest.mark.skipif(
    not HAS_M1,
    reason="M1（agents/experience_standard.py）尚未落地；M1 交付后这些互锁测试自动启用",
)

requires_smoke = pytest.mark.skipif(
    os.environ.get("EVOHARNESS_IR_SMOKE", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="手动模型冒烟：设 EVOHARNESS_IR_SMOKE=1 且 .env 配好 API/APIKEY/MODEL 后运行",
)


# ── 固化现网字面量（HEAD=5c6ad55 的 extract/maintain system prompt，逐字节锚点） ──

LEGACY_EXTRACT_SYSTEM = (
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

LEGACY_MAINTAIN_SYSTEM = (
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

GOLDEN_OFF_BODY = "# Skill Instructions\n\n1. 先分析\n2. 再改\n\n## Creation Evidence\n\n用户说：先分析再改\n"

RECORD_TOP_KEYS = {
    "schema_version",
    "source",
    "identity",
    "situation",
    "guidance",
    "applicability",
    "outcome",
    "evidence",
    "indexing",
    "knowledge_gaps",
}


# ── 沙箱与假模型 ────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _ir_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "off")
    reset_skill_cache()
    yield tmp_path
    reset_skill_cache()


def _enter_sandbox(tmp_path: Path, monkeypatch, name: str) -> Path:
    # 双沙箱：discover_skills 按 cwd 扫描且有进程内缓存，切换前后都必须 reset。
    root = tmp_path / name
    root.mkdir()
    reset_skill_cache()
    monkeypatch.chdir(root)
    reset_skill_cache()
    return root


def make_messages() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "帮我把项目 README 的安装步骤整理成有序列表。"},
        {"role": "assistant", "content": "好的，我先读取 README，再按安装顺序重排小节。"},
        {"role": "user", "content": "以后所有文档更新都要：先分析现有文档结构再改写，输出用有序列表。"},
        {"role": "assistant", "content": "明白：文档更新一律先分析现有结构，再改写，列表编号输出。"},
        {"role": "user", "content": "对，这就是我要的固定流程。"},
        {"role": "assistant", "content": "已按该流程完成本次更新。"},
        {"role": "user", "content": "下次改贡献指南也照此办理。"},
        {"role": "assistant", "content": "收到，贡献指南更新同样先分析再改。"},
    ]


def base_extract_obj(**overrides) -> dict:
    obj = {
        "name": "readme-analyze-first",
        "description": "Update README with analyze-first workflow",
        "when_to_use": "User asks to update a README or docs page",
        "instructions": "1. 先分析现有文档结构\n2. 再按分析结果改写",
        "evidence": "用户说：先分析再改",
        "tags": ["docs"],
    }
    obj.update(overrides)
    return obj


def extract_json(obj: dict) -> str:
    return json.dumps({"skills": [obj]}, ensure_ascii=False)


def decision_json(**overrides) -> str:
    decision = {"action": "add", "reason": "durable docs workflow"}
    decision.update(overrides)
    return json.dumps(decision, ensure_ascii=False)


def make_side_query(responses: list[str]):
    calls: list[tuple[str, str]] = []

    async def side_query(system: str, user: str) -> str:
        calls.append((system, user))
        index = len(calls) - 1
        return responses[index] if index < len(responses) else responses[-1]

    side_query.calls = calls
    return side_query


def run_ingest(side_query, **kwargs) -> dict:
    return asyncio.run(
        ose.online_ingest(
            messages=kwargs.pop("messages", make_messages()),
            side_query=side_query,
            retrieved_reference=kwargs.pop("retrieved_reference", None),
            hint=kwargs.pop("hint", ""),
            **kwargs,
        )
    )


def skill_body(root: Path, skill_name: str) -> str:
    raw = (root / ".evoharness" / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
    return raw.split("---\n\n", 1)[1]


def last_provenance_row(root: Path) -> dict:
    path = root / ".evoharness" / "skill-evolution" / "online_provenance.jsonl"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, f"provenance 未落盘: {path}"
    return json.loads(lines[-1])


VOLATILE_KEYS = {"time", "file", "history", "to", "from"}


def strip_volatile(value):
    if isinstance(value, dict):
        return {k: strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [strip_volatile(v) for v in value]
    return value


def strip_ir_keys(result: dict) -> dict:
    return {k: v for k, v in result.items() if not str(k).startswith("ir_")}


# ── C2 裁决的 M3 侧解析契约（round-trip 互锁用） ───────────────────

_WHEN_RE = re.compile(r"^- When: (.+)$")
_THEN_RE = re.compile(r"^  Then: (.+)$")
_BECAUSE_RE = re.compile(r"^  Because: (.+)$")
_GAP_RE = re.compile(r"^- (.+?)(?:（why needed: (.+?)）)?$")


def parse_ir_sections(body: str) -> tuple[list[dict], list[dict]]:
    """按 M3 §3.2 契约解析最终 SKILL.md：区头 ## Decision Rules / ## Open Questions，
    块起始 ^\\s*[-*]?\\s*when[:：]，Then/Because 两空格续行，gaps 为 bullet 行。"""
    rules: list[dict] = []
    gaps: list[dict] = []
    current: dict | None = None
    in_gaps = False
    for line in body.split("\n"):
        if line.strip() == "## Decision Rules":
            current, in_gaps = None, False
            continue
        if line.strip() == "## Open Questions":
            current, in_gaps = None, True
            continue
        if line.startswith("#"):
            current, in_gaps = None, False
            continue
        matched = _WHEN_RE.match(line)
        if matched and not in_gaps:
            current = {"when": matched.group(1), "then": [], "because": []}
            rules.append(current)
            continue
        matched = _THEN_RE.match(line)
        if matched and current is not None:
            current["then"].append(matched.group(1))
            continue
        matched = _BECAUSE_RE.match(line)
        if matched and current is not None:
            current["because"].append(matched.group(1))
            continue
        if in_gaps:
            matched = _GAP_RE.match(line)
            if matched:
                gaps.append({"question": matched.group(1), "why_needed": matched.group(2) or ""})
    return rules, gaps


# ── #1-#3 提示词契约 ────────────────────────────────────────────

def test_extract_prompt_off_mode_unchanged(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "off")
    assert ose._ir_mode() == "off"
    assert ose._extract_system_prompt(ose._ir_mode()) == LEGACY_EXTRACT_SYSTEM
    assert ose._EXTRACT_SYSTEM_BASE == LEGACY_EXTRACT_SYSTEM
    assert ose._extract_system_prompt("off").endswith('return {"skills": []}.\n')
    # 非法值归 off
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "bogus")
    assert ose._ir_mode() == "off"
    monkeypatch.delenv("EVOHARNESS_IR_MODE", raising=False)
    assert ose._ir_mode() == "off"


def test_maintain_prompt_always_unchanged(tmp_path, monkeypatch):
    for name, mode in (("maintain-off", "off"), ("maintain-annotate", "annotate")):
        root = _enter_sandbox(tmp_path, monkeypatch, name)
        monkeypatch.setenv("EVOHARNESS_IR_MODE", mode)
        side_query = make_side_query([extract_json(base_extract_obj()), decision_json(action="add")])
        result = run_ingest(side_query)
        assert result["action"] == "add", result
        # calls[0]=extract（off 契约已由 #1 锁定），calls[1]=maintain：全模式逐字节等于现网字面量
        assert side_query.calls[1][0] == LEGACY_MAINTAIN_SYSTEM


def test_extract_prompt_annotate_addendum(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "annotate")
    assert ose._ir_mode() == "annotate"
    annotate = ose._extract_system_prompt("annotate")
    assert annotate == ose._EXTRACT_SYSTEM_BASE + ose._IR_EXTRACT_ADDENDUM
    for keyword in ("basis", "confidence", "knowledge_gaps", "decision_rules", "evidence"):
        assert keyword in ose._IR_EXTRACT_ADDENDUM
    assert ose._extract_system_prompt("strict") == annotate
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "strict")
    assert ose._extract_system_prompt(ose._ir_mode()) == annotate


# ── #4 coercion 契约（硬伤 2 回归锚） ────────────────────────────

def test_coerce_evidence_single_key_contract():
    # 字符串形态：旧语义不变
    candidate = ose._coerce_candidate(base_extract_obj(evidence="旧字符串证据"))
    assert candidate.evidence == "旧字符串证据"
    assert candidate.evidence_items == []
    # 列表形态：dict 取 text、其余 str(item)；空 text 条目不进连接串但保留在 evidence_items
    items = [{"text": "第一条", "basis": "explicit", "confidence": 0.9}, {"text": ""}, "裸条目"]
    candidate = ose._coerce_candidate(base_extract_obj(evidence=items))
    assert candidate.evidence_items == items
    assert candidate.evidence == "第一条；裸条目"
    # 空串
    candidate = ose._coerce_candidate(base_extract_obj(evidence=""))
    assert candidate.evidence == "" and candidate.evidence_items == []
    # 模型误发 evidence_items 键被忽略（只读 evidence 一个键）
    candidate = ose._coerce_candidate(base_extract_obj(evidence="s", evidence_items=[{"text": "x"}]))
    assert candidate.evidence == "s" and candidate.evidence_items == []
    # 畸形条目原样透传不抛异常（dict 无 text → 连接串中为空）
    candidate = ose._coerce_candidate(base_extract_obj(evidence=[None, 5, {"no_text": 1}]))
    assert candidate.evidence_items == [None, 5, {"no_text": 1}]
    assert candidate.evidence == "None；5"
    # decision_rules / knowledge_gaps falsy 归 []、truthy 畸形原样透传
    candidate = ose._coerce_candidate(base_extract_obj(decision_rules=[], knowledge_gaps=None))
    assert candidate.decision_rules == [] and candidate.knowledge_gaps == []
    candidate = ose._coerce_candidate(base_extract_obj(decision_rules=[{"when": "畸形"}], knowledge_gaps={"q": 1}))
    assert candidate.decision_rules == [{"when": "畸形"}] and candidate.knowledge_gaps == {"q": 1}


# ── #5-#6 off 逐字符兼容（含 F-3 provenance 零注入） ─────────────

def test_off_mode_write_bytes_unchanged(tmp_path, monkeypatch):
    root = _enter_sandbox(tmp_path, monkeypatch, "off-golden")
    obj = base_extract_obj(instructions="1. 先分析\n2. 再改", evidence="用户说：先分析再改")
    side_query = make_side_query([extract_json(obj), decision_json(action="add")])
    result = run_ingest(side_query)
    assert result["ok"] and result["action"] == "add", result
    raw = (root / ".evoharness" / "skills" / "readme-analyze-first" / "SKILL.md").read_text(encoding="utf-8")
    assert raw.split("---\n\n", 1)[1] == GOLDEN_OFF_BODY
    # F-3：off 模式 provenance 的 result 无任何 ir_* 键
    row = last_provenance_row(root)
    assert row["action"] == "add"
    assert not [key for key in row["result"] if str(key).startswith("ir_")]


def test_annotate_no_ir_fields_no_new_sections(tmp_path, monkeypatch):
    bodies: dict[str, str] = {}
    results: dict[str, dict] = {}
    for name, mode in (("noir-off", "off"), ("noir-annotate", "annotate")):
        root = _enter_sandbox(tmp_path, monkeypatch, name)
        monkeypatch.setenv("EVOHARNESS_IR_MODE", mode)
        side_query = make_side_query([extract_json(base_extract_obj()), decision_json(action="add")])
        result = run_ingest(side_query)
        assert result["action"] == "add", result
        bodies[mode] = skill_body(root, "readme-analyze-first")
        results[mode] = last_provenance_row(root)["result"]
    # 无 gaps/rules：annotate 与 off 的 SKILL.md 切片逐字节相等，且不新增两区
    assert bodies["off"] == bodies["annotate"]
    assert "## Decision Rules" not in bodies["annotate"]
    assert "## Open Questions" not in bodies["annotate"]
    # off 零注入；annotate 的 ir_mode 恒有；剔除 ir_* 后两者 result 相等（F-4 口径）
    assert not [key for key in results["off"] if str(key).startswith("ir_")]
    assert results["annotate"]["ir_mode"] == "annotate"
    assert strip_volatile(strip_ir_keys(results["off"])) == strip_volatile(strip_ir_keys(results["annotate"]))


# ── #7-#10 annotate 观测与渲染（M1 互锁组） ──────────────────────

ANNOTATE_RICH_OBJ = base_extract_obj(
    decision_rules=[
        {
            "when": [{"text": "用户要求更新 README 或文档页", "basis": "explicit", "confidence": 0.9}],
            "then": [{"text": "先分析现有结构，再改写", "basis": "inferred", "confidence": 0.8}],
            "because": [{"text": "用户明确要求先分析再改", "basis": "explicit", "confidence": 0.9}],
        },
        {"when": ["用户要求输出有序列表"], "then": ["使用 1. 2. 3. 编号"], "because": []},
    ],
    knowledge_gaps=[
        {"question": "团队是否已有 README 模板", "why_needed": "决定是否需要保留现有小节结构"},
        {"question": "文档站部署方式未知"},
    ],
)


@requires_m1
def test_annotate_gaps_and_rules_rendered(tmp_path, monkeypatch):
    root = _enter_sandbox(tmp_path, monkeypatch, "annotate-rich")
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "annotate")
    side_query = make_side_query([extract_json(ANNOTATE_RICH_OBJ), decision_json(action="add")])
    result = run_ingest(side_query)
    assert result["ok"] and result["action"] == "add", result
    body = skill_body(root, "readme-analyze-first")
    assert body.count("## Decision Rules") == 1
    assert body.count("## Open Questions") == 1
    assert body.index("## Decision Rules") < body.index("## Open Questions")
    rules, gaps = parse_ir_sections(body)
    assert len(gaps) == 2  # gaps_render_rate = 1.0
    assert gaps[0] == {"question": "团队是否已有 README 模板", "why_needed": "决定是否需要保留现有小节结构"}
    assert gaps[1] == {"question": "文档站部署方式未知", "why_needed": ""}
    # M2→M3 互锁：渲染结果可被 M3 契约正则确定性解析回 when/then/because
    assert rules[0]["when"] == "用户要求更新 README 或文档页"
    assert rules[0]["then"] == ["先分析现有结构，再改写"]
    assert rules[0]["because"] == ["用户明确要求先分析再改"]
    # 规则 2 的裸字符串经 R2 升级后照常渲染；because 空则省略该行
    assert rules[1] == {"when": "用户要求输出有序列表", "then": ["使用 1. 2. 3. 编号"], "because": []}
    # 与 ir_record 的权威渲染互锁（ir_record 剔除了 raw_text，但不影响两区内容）
    record = result["ir_record"]
    assert record["source"].get("raw_text") is None
    rendered = ose.render_ir_sections(record)
    assert rendered in body
    assert rendered == ose.render_ir_sections({"guidance": record["guidance"], "knowledge_gaps": record["knowledge_gaps"]})


@requires_m1
def test_repairable_items_repaired_then_validated(tmp_path, monkeypatch):
    root = _enter_sandbox(tmp_path, monkeypatch, "annotate-repair")
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "annotate")
    obj = base_extract_obj(
        evidence=[
            "裸字符串条目（R2 升级）",
            {"text": "缺 basis 条目", "confidence": 0.8},
            {"text": "有 basis 缺 confidence 条目", "basis": "inferred"},
        ],
    )
    side_query = make_side_query([extract_json(obj), decision_json(action="add")])
    result = run_ingest(side_query)
    # C1 裁决：M1 七码（带 :path 后缀），M2 测试按前缀断言
    repairs = result.get("ir_repairs") or []
    assert any(code.startswith("upgrade_string_entries:") for code in repairs), repairs
    assert any(code.startswith("fill_entry_basis:") for code in repairs), repairs
    assert any(code.startswith("fill_entry_confidence:") for code in repairs), repairs
    # 设计 §2.5：ir_errors 非空才写——修复成功后键应缺席（repair_then_validate_rate=1.0）
    assert not result.get("ir_errors"), result.get("ir_errors")
    assert result["ok"] and result["action"] == "add"
    body = skill_body(root, "readme-analyze-first")
    assert "裸字符串条目（R2 升级）" in body  # repair_then_validate_rate=1.0：流程落盘成功


@requires_m1
def test_missing_schema_version_repaired():
    window = "user: 用户要求整理 README"
    record = ose.build_ir_record(ose._coerce_candidate(base_extract_obj()), window_text=window)
    assert record["schema_version"] == ose._ir_tools().SCHEMA_VERSION
    record.pop("schema_version")
    repaired, repairs = _m1.repair_record(record)
    assert "fill_schema_version" in repairs  # C1 裁决的 M1 码名（R1）
    assert repaired["schema_version"] == _m1.SCHEMA_VERSION
    assert _m1.validate_record(repaired, window) == []


@requires_m1
def test_unrepairable_item_annotates_without_blocking(tmp_path, monkeypatch):
    obj = base_extract_obj(evidence=[{"text": "", "basis": "explicit", "confidence": 0.9}])
    bodies: dict[str, str] = {}
    results: dict[str, dict] = {}
    for name, mode in (("unrepairable-off", "off"), ("unrepairable-annotate", "annotate")):
        root = _enter_sandbox(tmp_path, monkeypatch, name)
        monkeypatch.setenv("EVOHARNESS_IR_MODE", mode)
        side_query = make_side_query([extract_json(obj), decision_json(action="add")])
        result = run_ingest(side_query)
        assert result["action"] == "add" and result["ok"], result
        bodies[mode] = skill_body(root, "readme-analyze-first")
        results[mode] = last_provenance_row(root)["result"]
    # annotate：空 text 条目 R2-R5 均不触及 → provenance 行含非空 ir_errors（决策书第 7 条验收 b）
    assert results["annotate"].get("ir_errors"), results["annotate"]
    assert "invalid:evidence.observations[0].text" in results["annotate"]["ir_errors"]
    # F-4 口径：result 剔除 ir_* 键后与 off 相等；SKILL.md 切片逐字节相等（空 text → 无 Creation Evidence，两态一致）
    assert strip_volatile(strip_ir_keys(results["off"])) == strip_volatile(strip_ir_keys(results["annotate"]))
    assert bodies["off"] == bodies["annotate"]
    assert "## Decision Rules" not in bodies["annotate"]


# ── #11-#14 strict 拦截与 grounding ─────────────────────────────

UNGROUNDED_OBJ = base_extract_obj(
    evidence=[{"text": "orbi-flox quanta drift", "basis": "explicit", "confidence": 0.9}],
)


@requires_m1
def test_strict_blocks_unrepairable(tmp_path, monkeypatch):
    root = _enter_sandbox(tmp_path, monkeypatch, "strict-block")
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "strict")

    async def _deny(_summary: str) -> bool:
        return False

    # confirm_write 传 deny：若门在 confirm 之后，结果会是 add_denied——ir_rejected 证明门先于 confirm
    side_query = make_side_query([extract_json(base_extract_obj(evidence=[{"text": "", "basis": "explicit", "confidence": 0.9}]))])
    result = run_ingest(side_query, confirm_write=_deny)
    assert result["action"] == "ir_rejected", result
    assert result["ok"] is True
    assert result["ir_errors"] and "invalid:evidence.observations[0].text" in result["ir_errors"]
    skills_root = root / ".evoharness" / "skills"
    assert not skills_root.exists() or not any(skills_root.iterdir())  # 无写盘
    row = last_provenance_row(root)
    assert row["action"] == "ir_rejected" and row["ok"] is True
    assert row["result"]["ir_errors"]


@requires_m1
def test_strict_downgrades_ungrounded_explicit(tmp_path, monkeypatch):
    root = _enter_sandbox(tmp_path, monkeypatch, "strict-downgrade")
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "strict")
    side_query = make_side_query([extract_json(UNGROUNDED_OBJ), decision_json(action="add")])
    result = run_ingest(side_query)
    assert result["action"] == "add", result  # 降级后 errors 空，不拦截
    assert not result.get("ir_errors")  # 非空才写：降级干净则键缺席
    observation = result["ir_record"]["evidence"]["observations"][0]
    assert observation["basis"] == "inferred"
    assert observation["confidence"] == pytest.approx(0.5)
    entry = result["ir_grounding"][0]
    assert entry["has_overlap"] is False and entry["downgraded"] is True and entry["basis"] == "explicit"


@requires_m1
def test_annotate_keeps_declared_basis_and_flags(tmp_path, monkeypatch):
    root = _enter_sandbox(tmp_path, monkeypatch, "annotate-keep")
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "annotate")
    side_query = make_side_query([extract_json(UNGROUNDED_OBJ), decision_json(action="add")])
    result = run_ingest(side_query)
    assert result["action"] == "add", result
    observation = result["ir_record"]["evidence"]["observations"][0]
    assert observation["basis"] == "explicit"  # annotate 只观测：basis 不改写
    assert observation["confidence"] == pytest.approx(0.9)
    entry = result["ir_grounding"][0]
    assert entry["has_overlap"] is False and entry["downgraded"] is False
    body = skill_body(root, "readme-analyze-first")
    assert "orbi-flox quanta drift" in body  # 流程正常落盘


@requires_m1
def test_grounding_positive_overlap(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "annotate")
    obj = base_extract_obj(
        evidence=[{"text": "先分析现有文档结构再改写", "basis": "explicit", "confidence": 0.8}],
    )
    candidate = ose._coerce_candidate(obj)
    gate = ose.apply_ir_gate(candidate, messages=make_messages())
    assert gate.mode == "annotate" and gate.blocked is False
    entries = [item for item in gate.grounding if item["path"].startswith("evidence.observations")]
    assert entries and entries[0]["has_overlap"] is True and entries[0]["downgraded"] is False
    assert candidate.ir_record["evidence"]["observations"][0]["basis"] == "explicit"


# ── #15 merge/revise 单区不变量（硬伤 1 主测） ────────────────────

def _precreate_target_skill(root: Path) -> None:
    result = create_skill(
        name="docs-update-flow",
        description="Update docs pages with a fixed flow",
        instructions="# Docs Update Flow\n\n旧版正文，先改后分析。",
        target="project",
        actor="test",
    )
    assert result["ok"], result


MERGE_ECHO_WITH_HEADER = (
    "# Docs Update Flow\n\n"
    "## Decision Rules\n\n"
    "- When: OLD 旧触发\n"
    "  Then: OLD 旧行动\n\n"
    "## Open Questions\n\n"
    "- 旧问题（why needed: 旧理由）\n\n"
    "## Creation Evidence\n\n"
    "旧证据"
)

MERGE_ECHO_WITH_PROSE = (
    "# Docs Update Flow\n\n"
    "## Decision Rules\n\n"
    "- When: OLD 旧触发\n"
    "  Then: OLD 旧行动\n\n"
    "## Open Questions\n\n"
    "- 旧问题\n\n"
    "merge 时保留这些旧规则并补充新规则。"
)


@requires_m1
@pytest.mark.parametrize("echo_body,echo_kind", [(None, "plain"), (MERGE_ECHO_WITH_HEADER, "echo-header"), (MERGE_ECHO_WITH_PROSE, "echo-prose")])
def test_merge_revise_sections_single_and_fresh(tmp_path, monkeypatch, echo_body, echo_kind):
    root = _enter_sandbox(tmp_path, monkeypatch, f"merge-{echo_kind}")
    _precreate_target_skill(root)
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "annotate")
    merged = echo_body if echo_body is not None else "# Docs Update Flow\n\n新版正文。"
    side_query = make_side_query(
        [
            extract_json(ANNOTATE_RICH_OBJ),
            decision_json(action="merge", target_skill="docs-update-flow", merged_instructions=merged),
        ]
    )
    result = run_ingest(side_query)
    assert result["ok"] and result["action"] == "merge", result
    body = skill_body(root, "docs-update-flow")
    # 单区不变量：两区各恰 1 个、内容 == candidate.ir_record 的权威渲染
    assert body.count("## Decision Rules") == 1
    assert body.count("## Open Questions") == 1
    fresh = ose.render_ir_sections(result["ir_record"])
    assert fresh in body
    rules, gaps = parse_ir_sections(body)
    assert rules and gaps
    assert "OLD 旧触发" not in body  # 回显旧内容不残留
    if echo_kind == "echo-header":
        assert "## Creation Evidence" in body  # 回显区后的标题区被保留
    if echo_kind == "echo-prose":
        # 有界损失（设计 §2.4 如实声明）：回显区后跟非标题散文会被连带吞掉
        assert "merge 时保留这些旧规则" not in body
    # revise 同构抽查：revised_instructions 走同一包装
    root2 = _enter_sandbox(tmp_path, monkeypatch, f"revise-{echo_kind}")
    _precreate_target_skill(root2)
    side_query = make_side_query(
        [
            extract_json(ANNOTATE_RICH_OBJ),
            decision_json(action="revise", target_skill="docs-update-flow", revised_instructions="# Docs Update Flow\n\n替换正文。"),
        ]
    )
    result = run_ingest(side_query)
    assert result["ok"] and result["action"] == "revise", result
    body2 = skill_body(root2, "docs-update-flow")
    assert body2.count("## Decision Rules") == 1 and body2.count("## Open Questions") == 1
    assert ose.render_ir_sections(result["ir_record"]) in body2


# ── #16-#17 payload 视图与 confirm_write 门 ─────────────────────

def test_maintainer_payload_strips_ir_fields(tmp_path, monkeypatch):
    root = _enter_sandbox(tmp_path, monkeypatch, "payload-view")
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "annotate")
    side_query = make_side_query([extract_json(ANNOTATE_RICH_OBJ), decision_json(action="add")])
    result = run_ingest(side_query)
    assert result["action"] == "add", result
    payload = json.loads(side_query.calls[1][1])
    # 键集合与顺序 == 6 个旧字段（IR 新增字段一律不外泄给 maintainer）
    assert list(payload["candidate"].keys()) == ["name", "description", "when_to_use", "instructions", "evidence", "tags"]
    assert payload["candidate"]["evidence"] == "用户说：先分析再改"


def test_confirm_write_gate_unchanged(tmp_path, monkeypatch):
    root = _enter_sandbox(tmp_path, monkeypatch, "confirm-deny")
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "annotate")

    async def _deny(_summary: str) -> bool:
        return False

    side_query = make_side_query([extract_json(ANNOTATE_RICH_OBJ), decision_json(action="add")])
    result = run_ingest(side_query, confirm_write=_deny)
    assert result["action"] == "add_denied" and result["ok"] is False, result
    skills_root = root / ".evoharness" / "skills"
    assert not skills_root.exists() or not any(skills_root.iterdir())  # 无写盘
    row = last_provenance_row(root)
    assert row["ok"] is False and row["action"] == "add_denied"


# ── #18-#20 gate 纯函数与 fail-open ─────────────────────────────

@requires_m1
def test_build_ir_record_shape_and_interlock():
    window = "user: 请把 README 安装步骤整理成有序列表"
    valid_obj = base_extract_obj(
        when_to_use="",  # 触发条目缺 basis 由 R3/R4 在 gate 内补，裸 build 想直接过 validate 须留空
        evidence=[
            {"text": "用户明确要求有序列表", "basis": "explicit", "confidence": 0.9},
            {"text": "先分析再改写为推导结论", "basis": "inferred", "confidence": 0.8},
        ],
        decision_rules=[
            {
                "when": [{"text": "用户要求更新文档", "basis": "explicit", "confidence": 0.9}],
                "then": [{"text": "先分析现有结构", "basis": "inferred", "confidence": 0.7}],
                "because": [{"text": "用户明确要求", "basis": "explicit", "confidence": 0.9}],
            }
        ],
        knowledge_gaps=[{"question": "团队是否已有模板", "why_needed": "决定结构"}],
    )
    record = ose.build_ir_record(ose._coerce_candidate(valid_obj), window_text=window)
    assert set(record) == RECORD_TOP_KEYS  # 10 个顶层必需键，不多不少
    assert record["source"]["raw_text"] == window
    assert record["source"]["source_type"] == "online_conversation"
    assert record["identity"]["primary_type"] == "heuristic"
    assert record["indexing"]["tags"] == ["docs"]
    assert _m1.validate_record(record, window) == []  # 互锁：M2 产出可直接过 M1 校验
    # 应修 (c)：confs=[0.9, 1.5, "0.8", None] → 0.9（越界/非数值被排除在均值外）
    probe_obj = base_extract_obj(
        when_to_use="",
        evidence=[
            {"text": "a", "basis": "explicit", "confidence": 0.9},
            {"text": "b", "basis": "inferred", "confidence": 1.5},
            {"text": "c", "basis": "inferred", "confidence": "0.8"},
            {"text": "d", "basis": "inferred"},
        ],
    )
    probe = ose.build_ir_record(ose._coerce_candidate(probe_obj), window_text=window)
    assert probe["evidence"]["confidence"] == pytest.approx(0.9)
    # 越界值不拖垮记录级字段，但仍在条目级被 validate 报错（观测信号不洗白）
    assert "invalid:evidence.observations[1].confidence" in _m1.validate_record(probe, window)


def test_ir_gate_off_noop(monkeypatch):
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "off")
    candidate = ose._coerce_candidate(base_extract_obj())
    gate = ose.apply_ir_gate(candidate, messages=make_messages())
    assert gate.mode == "off"
    assert gate.record is None and gate.blocked is False
    assert gate.errors == [] and gate.repairs == [] and gate.grounding == []
    # candidate 的 ir_* 保持默认
    assert candidate.ir_mode == "" and candidate.ir_record is None
    assert candidate.ir_errors == [] and candidate.ir_repairs == [] and candidate.ir_grounding == []


class _ExplodingTools:
    SCHEMA_VERSION = "experience-standard/v1"

    def repair_record(self, record):
        return record, []

    def validate_record(self, record, raw_text):
        raise RuntimeError("boom")

    def grounding_flag(self, text, window):
        return True


def test_ir_gate_fail_open(tmp_path, monkeypatch):
    monkeypatch.setattr(ose, "_ir_tools", lambda: _ExplodingTools())
    for name, mode in (("fail-open-annotate", "annotate"), ("fail-open-strict", "strict")):
        root = _enter_sandbox(tmp_path, monkeypatch, name)
        monkeypatch.setenv("EVOHARNESS_IR_MODE", mode)
        # 候选带 decision_rules/gaps：record=None 时两区绝不出现（不剥不追加）
        side_query = make_side_query([extract_json(ANNOTATE_RICH_OBJ), decision_json(action="add")])
        result = run_ingest(side_query)
        assert result["action"] == "add" and result["ok"], result  # strict 同样不拦截
        body = skill_body(root, "readme-analyze-first")
        assert "## Decision Rules" not in body and "## Open Questions" not in body
        row = last_provenance_row(root)
        assert row["result"]["ir_mode"] == mode
        assert row["result"]["ir_errors"] and row["result"]["ir_errors"][0].startswith("ir_gate_error: ")
        assert row["result"]["ir_errors"][0].endswith("RuntimeError: boom")


# ── #21 手动模型冒烟（默认 skip） ────────────────────────────────

SMOKE_MESSAGES = [
    {"role": "user", "content": "我们团队每次发版都要手动跑一遍检查清单，太容易漏了。"},
    {"role": "assistant", "content": "可以把检查清单固化成脚本或 skill。"},
    {"role": "user", "content": "对，我的固定流程是：先跑 lint，再跑单测，最后检查变更清单，三步都绿才允许合并。"},
    {"role": "assistant", "content": "明白：lint、单测、变更清单三步检查。"},
    {"role": "user", "content": "以后所有发版前都按这个顺序执行，缺一步都不行。"},
    {"role": "assistant", "content": "已记录该发版前检查流程。"},
    {"role": "user", "content": "这个规则要长期保留。"},
    {"role": "assistant", "content": "好的，会作为固定约束保留。"},
]


def _load_env_file_manual() -> None:
    # main.py:82-87 同款意图（find_dotenv + load_dotenv, override=False）的零依赖手动版
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            break


def _build_manual_side_query(model: str, api_key: str, api_base: str | None):
    # 镜像 agent.py:230-248 的客户端构造 + main.py:91-97 的 anthropic 兼容 base_url 判定
    base = str(api_base or "").lower().rstrip("/")
    if base.endswith("/anthropic") or "/anthropic/" in base:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=api_base)

        async def _sq(system: str, user_message: str) -> str:
            resp = await client.messages.create(
                model=model,
                max_tokens=2200,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

        return _sq

    import openai

    client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)

    async def _sq_openai(system: str, user_message: str) -> str:
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=2200,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
        )
        return resp.choices[0].message.content or ""

    return _sq_openai


@requires_smoke
def test_model_smoke_annotate(tmp_path, monkeypatch):
    if not HAS_M1:
        pytest.skip("M1 未落地：smoke 需要 M1.grounding_flag")
    _load_env_file_manual()
    api_key = (
        os.environ.get("APIKEY")
        or os.environ.get("EVOHARNESS_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    if not api_key:
        pytest.fail("EVOHARNESS_IR_SMOKE=1 已激活但未配置 API key（.env 需含 API/APIKEY/MODEL，见 main.py:99-103/:380）")
    model = os.environ.get("MODEL") or "deepseek-chat"
    api_base = (
        os.environ.get("API")
        or os.environ.get("EVOHARNESS_API_BASE")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
    )
    try:
        side_query = _build_manual_side_query(model, api_key, api_base)
    except ImportError as exc:
        pytest.skip(f"模型 SDK 不可用，无法执行手动冒烟: {exc}")
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "annotate")
    monkeypatch.chdir(tmp_path)
    reset_skill_cache()
    try:
        candidate = asyncio.run(ose.extract_online_skill_candidate(messages=SMOKE_MESSAGES, side_query=side_query))
    finally:
        reset_skill_cache()
    assert candidate is not None, "smoke：extract 未产出候选"
    items = list(candidate.evidence_items)
    for rule in candidate.decision_rules:
        if isinstance(rule, dict):
            for key in ("when", "then", "because"):
                bucket = rule.get(key)
                if isinstance(bucket, list):
                    items.extend(bucket)
    assert items, "smoke：候选无任何结构化条目"
    for item in items:
        assert isinstance(item, dict) and str(item.get("text") or "").strip(), item
        assert item.get("basis") in {"explicit", "inferred"}, item
        assert isinstance(item.get("confidence"), (int, float)) and 0.0 <= float(item["confidence"]) <= 1.0, item
    explicit = [item for item in items if item.get("basis") == "explicit"]
    assert explicit, "smoke：无 explicit 条目可校验 grounding"
    window = ose._window_text(SMOKE_MESSAGES)
    grounded = [_m1.grounding_flag(str(item.get("text") or ""), window) for item in explicit]
    assert all(grounded), f"smoke：explicit 条目 grounding 通过率 {sum(grounded)}/{len(grounded)} != 1.0"
