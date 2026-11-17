"""DE-29 最小 Module 定位回归：外置 ModuleSpec、有界编辑与候选重定位。

覆盖（tasks.json DE-29 acceptance_scenarios / SKILL-EVOLUTION-SEARCH §5/§9）：

- S2：交叠 span / 不唯一 span / 陈旧 hash / 外部编辑（manifest 冻结后被改）
  全部拒绝；只读 guardrails 不可改写、业务可写范围可写；frontmatter 身份区只读
- S3：多 Module 在正文长度变化（前后各加段落）后按标题/锚点重定位正确；
  单一整文 Module 的小 Skill 合法建立并正常重定位；子版本 ModuleSpec 保持
  模块逻辑 ID 与可写策略不变
- 默认不向用户 SKILL.md 添加任何定位标记：建立/校验/重定位/候选落盘全程
  原文件字节不变（定位信息只在外置结构/搜索命名空间旁车）
- 复用 agents.frontmatter 解析（不引入第二 Skill 格式），候选编辑为确定性
  精确合并（无 fuzzy apply）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import skill_tiers as st
from agents.skill_search import contracts as sc
from agents.skill_search import modules as sm
from agents.skills import reset_skill_cache

EVOLUTION_DIR_ENVS = ("EVOHARNESS_EVOLUTION_DIR", "EVOHARNESS_SKILLS_ROOT")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for env in EVOLUTION_DIR_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "off")
    reset_skill_cache()
    st.reset_tier_cache()
    yield tmp_path
    reset_skill_cache()
    st.reset_tier_cache()


MULTI_SECTION_SKILL = """---
name: multi-section
description: 多段落业务技能
version: 0.4.0
producer-kind: executable_sop
---

# 总览

本技能处理多段落业务流程。

## 工作流程

按顺序执行三个步骤，先核对输入再执行。

## 输出格式

结果一律使用 Markdown 表格输出。

## 安全边界

未经确认不得执行不可逆操作。
"""

MULTI_DEFS = [
    {"module_id": "workflow", "type": "capability.rollout", "writable": True, "heading": "## 工作流程"},
    {"module_id": "output-format", "type": "shared.response", "writable": True, "heading": "## 输出格式"},
    {"module_id": "guardrails", "type": "shared.guardrails", "writable": False, "heading": "## 安全边界"},
]

SMALL_SKILL = """---
name: tiny-sop
description: 单段小技能
version: 0.1.0
---

# 步骤

核对输入，执行，回填结果。
"""


def _write_skill(root: Path, slug: str, text: str) -> Path:
    skill_dir = root / ".evoharness" / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


def _manifest_for(skill_dir: Path, text: str, spec) -> object:
    out = sc.build_search_manifest(
        skill_dir=skill_dir,
        tenant="acme",
        space="sre",
        module_spec=spec,
    )
    assert out["ok"] is True, out
    return out["manifest"]


# ── 建立：单 Module 小技能 / 多 Module 业务段落定位 ───────────────────────


def test_single_module_small_skill_is_valid_whole_body(sandbox):
    text = SMALL_SKILL
    out = sm.build_module_spec(text)
    assert out["ok"] is True, out
    spec = out["spec"]
    assert len(spec.modules) == 1
    only = spec.modules[0]
    assert only.module_id == sm.DEFAULT_MODULE_ID
    assert only.writable is True and only.whole_body is True
    boundary, _ = sm.frontmatter_boundary_of(text)
    assert (only.start, only.end) == (0, len(text[boundary:]))
    assert only.anchor_hash == sm._sha256_text(text[boundary:])
    assert spec.approved_spec_hash == sm.compute_spec_hash(spec)
    # 短 Skill 无多层章节也可合法建立单 Module（正文区即可写范围）
    writable = spec.writable_modules()
    assert [m.module_id for m in writable] == [sm.DEFAULT_MODULE_ID]


def test_multi_module_spans_partition_and_writable_split(sandbox):
    out = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)
    assert out["ok"] is True, out
    spec = out["spec"]
    assert [m.module_id for m in spec.modules] == ["workflow", "output-format", "guardrails"]
    by_id = {m.module_id: m for m in spec.modules}
    # 可写范围划分：业务段可写，guardrails 只读
    assert [m.module_id for m in spec.writable_modules()] == ["workflow", "output-format"]
    assert [m.module_id for m in spec.readonly_modules()] == ["guardrails"]
    # span 互不交叠且都在正文区
    ordered = sorted(spec.modules, key=lambda m: m.start)
    for prev, nxt in zip(ordered, ordered[1:]):
        assert prev.end <= nxt.start
    # 各模块区段原文正确（标题分区规则：到下一个标题行为止）
    assert sm.module_section_text(spec, MULTI_SECTION_SKILL, "workflow").startswith("## 工作流程")
    assert "按顺序执行三个步骤" in sm.module_section_text(spec, MULTI_SECTION_SKILL, "workflow")
    assert "Markdown 表格" in sm.module_section_text(spec, MULTI_SECTION_SKILL, "output-format")
    assert "不可逆操作" in sm.module_section_text(spec, MULTI_SECTION_SKILL, "guardrails")
    assert "本技能处理多段落业务流程" not in sm.module_section_text(spec, MULTI_SECTION_SKILL, "workflow")
    # 正文首段（第一个标题之前）不属于任何 Module：默认只读
    intro_start = 0
    assert all(m.start > intro_start for m in spec.modules)
    # 锚点 = 区段内容 hash
    for item in spec.modules:
        assert item.anchor_hash == sm._sha256_text(sm.module_section_text(spec, MULTI_SECTION_SKILL, item.module_id))


def test_guardrails_must_not_be_declared_writable(sandbox):
    bad_defs = [dict(MULTI_DEFS[2], writable=True)]
    out = sm.build_module_spec(MULTI_SECTION_SKILL, bad_defs)
    assert out["ok"] is False and out["error"] == "guardrails_must_be_readonly"


def test_unknown_module_type_rejected(sandbox):
    out = sm.build_module_spec(MULTI_SECTION_SKILL, [{"module_id": "x", "type": "magic.section", "writable": True, "heading": "## 工作流程"}])
    assert out["ok"] is False and out["error"] == "unknown_module_type"


# ── S2：交叠 / 不唯一 / 陈旧 hash / 外部编辑 / 只读全部拒绝 ───────────────


def test_s2_overlapping_explicit_spans_rejected(sandbox):
    boundary, _ = sm.frontmatter_boundary_of(MULTI_SECTION_SKILL)
    body_len = len(MULTI_SECTION_SKILL[boundary:])
    defs = [
        {"module_id": "a", "type": "capability.a", "writable": True, "start": 0, "end": body_len // 2 + 20},
        {"module_id": "b", "type": "capability.b", "writable": True, "start": body_len // 2 - 20, "end": body_len},
    ]
    out = sm.build_module_spec(MULTI_SECTION_SKILL, defs)
    assert out["ok"] is False and out["error"] == "span_overlap"


def test_s2_duplicate_heading_rejected_as_not_unique(sandbox):
    duplicated = MULTI_SECTION_SKILL.replace("## 安全边界", "## 工作流程")
    out = sm.build_module_spec(duplicated, MULTI_DEFS)
    assert out["ok"] is False and out["error"] == "span_not_unique"
    # 显式 span 越界同样拒绝
    defs = [{"module_id": "a", "type": "capability.a", "writable": True, "start": 0, "end": 10 ** 6}]
    out2 = sm.build_module_spec(MULTI_SECTION_SKILL, defs)
    assert out2["ok"] is False and out2["error"] == "span_out_of_range"


def test_s2_external_edit_after_manifest_freeze_rejected(sandbox):
    skill_dir = _write_skill(sandbox, "multi-section", MULTI_SECTION_SKILL)
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    manifest = _manifest_for(skill_dir, MULTI_SECTION_SKILL, spec)

    # manifest 冻结后文件被外部编辑
    skill_dir.joinpath("SKILL.md").write_text(MULTI_SECTION_SKILL.replace("先核对输入再执行", "先核对输入并留痕再执行"), encoding="utf-8")
    current_text = skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8-sig")
    drift = manifest.verify_integrity()
    assert drift["ok"] is False and drift["reason"] == "blocked_source_changed"
    checked = sm.validate_candidate_edit(
        spec,
        module_id="workflow",
        replacement_text="## 工作流程\n\n新步骤。\n",
        current_text=current_text,
        skill_text_sha256=manifest.skill_text_sha256,
    )
    assert checked["ok"] is False
    assert checked["error"] == "blocked_source_changed"
    assert checked["expected_sha256"] == manifest.skill_text_sha256

    # 外部编辑同样污染 apply_module_edit（不产出候选文本）
    applied = sm.apply_module_edit(
        spec,
        current_text,
        module_id="workflow",
        replacement_text="## 工作流程\n\n新步骤。\n",
        skill_text_sha256=manifest.skill_text_sha256,
    )
    assert applied["ok"] is False and applied["error"] == "blocked_source_changed"


def test_s2_stale_anchor_rejected_when_span_content_changed(sandbox):
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    # 先做一次合法编辑得到子版本（workflow 内容已变）
    applied = sm.apply_module_edit(
        spec,
        MULTI_SECTION_SKILL,
        module_id="workflow",
        replacement_text="## 工作流程\n\n按顺序执行四个步骤，新增回滚预案。\n",
    )
    assert applied["ok"] is True, applied
    candidate_text = applied["candidate_text"]
    # 用种子版 ModuleSpec（旧行号/旧锚点）继续改更长的子版本 → 陈旧 hash 拒绝
    # （workflow 区段内容已变）
    stale = sm.validate_candidate_edit(
        spec,
        module_id="workflow",
        replacement_text="## 工作流程\n\n第五步。\n",
        current_text=candidate_text,
    )
    assert stale["ok"] is False and stale["error"] == "stale_anchor_hash"
    # 子版本 ModuleSpec 才能在子版本上继续编辑（§5.1 子版本契约）
    child = applied["child_spec"]
    ok2 = sm.validate_candidate_edit(
        child,
        module_id="workflow",
        replacement_text="## 工作流程\n\n按顺序执行五个步骤。\n",
        current_text=candidate_text,
    )
    assert ok2["ok"] is True, ok2


def test_s2_readonly_guardrails_and_business_writable_split(sandbox):
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    # 只读 guardrails：编辑拒绝
    denied = sm.validate_candidate_edit(
        spec,
        module_id="guardrails",
        replacement_text="## 安全边界\n\n放开限制。\n",
        current_text=MULTI_SECTION_SKILL,
    )
    assert denied["ok"] is False and denied["error"] == "readonly_module"
    applied_denied = sm.apply_module_edit(
        spec,
        MULTI_SECTION_SKILL,
        module_id="guardrails",
        replacement_text="## 安全边界\n\n放开限制。\n",
    )
    assert applied_denied["ok"] is False and applied_denied["error"] == "readonly_module"
    # 业务可写范围：capability / shared.response 可编辑
    allowed = sm.apply_module_edit(
        spec,
        MULTI_SECTION_SKILL,
        module_id="output-format",
        replacement_text="## 输出格式\n\n结果一律使用 JSON 输出。\n",
    )
    assert allowed["ok"] is True, allowed
    assert "JSON 输出" in allowed["candidate_text"]
    assert "Markdown 表格" not in sm.module_section_text(allowed["child_spec"], allowed["candidate_text"], "output-format")
    # 不存在的 Module / 被篡改的 spec 都拒绝
    missing = sm.validate_candidate_edit(
        spec, module_id="nope", replacement_text="x", current_text=MULTI_SECTION_SKILL
    )
    assert missing["ok"] is False and missing["error"] == "module_not_found"
    tampered = sm.spec_from_dict(sm.spec_to_dict(spec))
    tampered.modules[0].writable = False
    broken = sm.validate_candidate_edit(
        tampered, module_id="workflow", replacement_text="x", current_text=MULTI_SECTION_SKILL
    )
    assert broken["ok"] is False and broken["error"] == "spec_hash_mismatch"


def test_s2_frontmatter_identity_region_readonly(sandbox):
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    hijacked = MULTI_SECTION_SKILL.replace("name: multi-section", "name: hijacked")
    checked = sm.validate_candidate_edit(
        spec, module_id="workflow", replacement_text="## 工作流程\n\n新步骤。\n", current_text=hijacked
    )
    assert checked["ok"] is False and checked["error"] == "frontmatter_modified"
    applied = sm.apply_module_edit(
        spec, hijacked, module_id="workflow", replacement_text="## 工作流程\n\n新步骤。\n"
    )
    assert applied["ok"] is False and applied["error"] == "frontmatter_modified"


# ── S3：正文长度变化后按标题/锚点重定位正确 ───────────────────────────────


def test_s3_multi_module_relocation_after_length_change(sandbox):
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    before = {m.module_id: sm.module_section_text(spec, MULTI_SECTION_SKILL, m.module_id) for m in spec.modules}

    # 正文前后各加段落（首段前导语 + 文末补充），模块区段内容不变
    candidate_text = MULTI_SECTION_SKILL.replace(
        "# 总览\n\n", "# 总览\n\n新增前置说明：本版本扩充了前置上下文。\n\n"
    ).rstrip("\n") + "\n\n## 附注\n\n文末补充说明，不属于任何业务模块。\n"
    relocated = sm.relocate_modules(spec, candidate_text)
    assert relocated["ok"] is True, relocated
    child = relocated["spec"]
    # 模块逻辑 ID / 类型 / 可写策略跨代不变
    for item in child.modules:
        parent = spec.module(item.module_id)
        assert parent is not None
        assert (item.type, item.writable, item.heading) == (parent.type, parent.writable, parent.heading)
    # 新 span 指向同一内容（前缀偏移正确平移）
    for module_id, old_text in before.items():
        assert sm.module_section_text(child, candidate_text, module_id) == old_text
    workflow = child.module("workflow")
    old_workflow = spec.module("workflow")
    assert workflow.start == old_workflow.start + len("新增前置说明：本版本扩充了前置上下文。\n\n")
    # 文末新增的附注段落落在任何 Module 冻结区段之外（extent 规则保留各区段
    # 自身内容），与正文首段一样属于默认只读的间隙；要优化它须显式进 spec
    guardrails = child.module("guardrails")
    assert "附注" not in sm.module_section_text(child, candidate_text, "guardrails")
    assert guardrails.end < len(candidate_text[sm.frontmatter_boundary_of(candidate_text)[0]:])
    # approved hash 随子版本重算（模块 id/策略不变，span/锚点/源 hash 变化）
    assert child.approved_spec_hash != spec.approved_spec_hash


def test_s3_single_whole_body_module_relocates_and_policy_unchanged(sandbox):
    spec = sm.build_module_spec(SMALL_SKILL)["spec"]
    grown = SMALL_SKILL.replace("核对输入，执行，回填结果。", "核对输入。\n\n执行。\n\n回填结果。\n")
    relocated = sm.relocate_modules(spec, grown)
    assert relocated["ok"] is True, relocated
    child = relocated["spec"]
    only = child.modules[0]
    boundary, _ = sm.frontmatter_boundary_of(grown)
    assert (only.start, only.end) == (0, len(grown[boundary:]))
    assert only.module_id == sm.DEFAULT_MODULE_ID and only.writable is True and only.whole_body is True
    assert child.skill_name == spec.skill_name


def test_s3_relocation_fails_closed_on_lost_or_ambiguous_anchor(sandbox):
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    missing = MULTI_SECTION_SKILL.replace("## 工作流程\n\n", "")
    out = sm.relocate_modules(spec, missing)
    assert out["ok"] is False and out["error"] == "anchor_lost"
    duplicated = MULTI_SECTION_SKILL.replace("## 安全边界", "## 工作流程")
    out2 = sm.relocate_modules(spec, duplicated)
    assert out2["ok"] is False and out2["error"] == "span_not_unique"


def test_s3_apply_module_edit_relocates_siblings_and_keeps_frontmatter(sandbox):
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    replacement = "## 工作流程\n\n按顺序执行四个步骤，新增灰度观察与回滚预案。\n"
    applied = sm.apply_module_edit(
        spec, MULTI_SECTION_SKILL, module_id="workflow", replacement_text=replacement
    )
    assert applied["ok"] is True, applied
    candidate_text = applied["candidate_text"]
    child = applied["child_spec"]
    # 目标模块新锚点 = 替换文本 hash；其余模块区段字节不变
    assert sm.module_section_text(child, candidate_text, "workflow") == replacement
    assert sm.module_section_text(child, candidate_text, "output-format") == sm.module_section_text(
        spec, MULTI_SECTION_SKILL, "output-format"
    )
    assert sm.module_section_text(child, candidate_text, "guardrails") == sm.module_section_text(
        spec, MULTI_SECTION_SKILL, "guardrails"
    )
    # frontmatter 字节不变；除目标模块外其余源片段 hash 完全不变
    boundary, _ = sm.frontmatter_boundary_of(candidate_text)
    original_boundary, _ = sm.frontmatter_boundary_of(MULTI_SECTION_SKILL)
    assert candidate_text[:boundary] == MULTI_SECTION_SKILL[:original_boundary]
    # 只改变一个目标 Module：替换精确发生在冻结 span 上
    assert candidate_text.count("按顺序执行三个步骤") == 0
    assert candidate_text.count("按顺序执行四个步骤") == 1


# ── 默认不向用户 SKILL.md 添加新标记（前后字节不变） ───────────────────────


def test_no_markers_added_to_user_skill_md(sandbox, tmp_path):
    skill_dir = _write_skill(sandbox, "multi-section", MULTI_SECTION_SKILL)
    before_bytes = skill_dir.joinpath("SKILL.md").read_bytes()
    before_listing = sorted(p.name for p in skill_dir.iterdir())

    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    manifest = _manifest_for(skill_dir, MULTI_SECTION_SKILL, spec)
    applied = sm.apply_module_edit(
        spec,
        MULTI_SECTION_SKILL,
        module_id="output-format",
        replacement_text="## 输出格式\n\n结果一律使用 JSON 输出。\n",
    )
    assert applied["ok"] is True
    candidate_dir = sandbox / "search-root" / "acme" / "sre" / "multi-section" / manifest.search_id / "candidates" / "c-9"
    saved = sc.save_candidate_module_spec(manifest, candidate_dir, applied["child_spec"])
    assert saved["ok"] is True, saved

    # 用户 SKILL.md 字节不变；目录无新增文件；文本中无任何定位标记
    assert skill_dir.joinpath("SKILL.md").read_bytes() == before_bytes
    assert sorted(p.name for p in skill_dir.iterdir()) == before_listing
    raw = skill_dir.joinpath("SKILL.md").read_bytes()
    assert b"<!--" not in raw
    assert b"module_id" not in raw
    assert b"anchor" not in raw
    assert b"search" not in raw.lower()
    # 重定位信息只存在于候选目录旁车（module-spec.json），不在源目录
    assert (candidate_dir / "module-spec.json").is_file()
    # 重建的 spec 也不落盘：纯内存结构可序列化往返
    round_trip = sm.spec_from_dict(sm.spec_to_dict(spec))
    assert round_trip.approved_spec_hash == spec.approved_spec_hash
