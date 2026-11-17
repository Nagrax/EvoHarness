"""DE-32 有界单 Module 编辑与拒绝历史回归（SKILL-EVOLUTION-SEARCH §9）。

覆盖 tasks.json DE-32 的 4 条 acceptance_scenarios：

1. 合法单 Module 精确替换：span+预期原文匹配 → 程序合并成功；其他
   Module/frontmatter/其余正文字节不变（无 fuzzy apply，模型只交操作不交整文件）。
2. 拒绝面：改第二 Module / guardrails / 工具依赖 / IR 区 / 包装器区 / frontmatter
   注入 / 硬编码考卷答案与 case_id / 锚点不匹配 / 兄弟标题歧义 → 全部拒绝并留
   拒绝历史（原因+有界 diff）；重复相同被拒 patch 再次拒绝且不重复存储。
3. 单轮小改但累计超冻结上限被挡；上限硬顶不可绕过；无效 patch 不修改生产
   Skill（active 字节不变、不注册 tier、不写 usage 台账）。
4. S3：实际评测 hash == 最终交付 hash（组合 DE-18）：评测绑定最终包装 → 发布
   后 active 内容 hash 与评测一致 → 晋级成功；评测后追加 Evolution Notes →
   hash 变化 → apply_eval_promotions 以 active_drift 拒绝；带追加内容的 bundle
   冒用旧 hash 发布 → bundle_hash_mismatch；评测绑定包装前裸候选 → 制品一致门拒绝。
5. 关闭新策略：原 heuristic/LLM/trigger 路由保持；投影重建（write_managed_projection）
   与普通在线沉淀（evolve_skill_file）不经单 Module 门。

全部离线（tmp 合成 SKILL.md / 合成 patch），不依赖真实模型。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import skill_tiers as st
from agents.skill_search import contracts as sc
from agents.skill_search import modules as sm
from agents.skill_search import mutator as mu
from agents.skills import reset_skill_cache
from evoharness.knowledge.release import publish_skill_bundle

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


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _replace_op(section_text: str, expected: str, replacement: str, *, module: str = "") -> dict:
    start = section_text.index(expected)
    return {
        "op": "replace",
        "start": start,
        "end": start + len(expected),
        "expected": expected,
        "replacement": replacement,
        "module": module,
    }


def _write_skill(root: Path, slug: str, text: str) -> Path:
    skill_dir = root / ".evoharness" / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


# ── 合成技能与考卷材料 ───────────────────────────────────────────────────────

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

EXAM_CASES = [
    {
        "case_id": "case-20261114-a",
        "question": "客户空调显示E5故障码，现场应如何处置？",
        "expected": "先断电复位，再检查室外机风机电容，必要时更换并记录工单。",
    }
]
EXAM_MATERIALS = [c["expected"] for c in EXAM_CASES] + [c["question"] for c in EXAM_CASES]
CASE_IDS = [c["case_id"] for c in EXAM_CASES]

POLICY_ON = mu.MutationPolicy(enable_bounded_module_edit=True)


def _big_step(index: int) -> str:
    return f"第{index}步：核对工单第{index}项配置，确认现场条件满足要求后执行并记录结果。"


def _big_section_skill(steps: int = 60) -> str:
    workflow_body = "\n".join(_big_step(i) for i in range(1, steps + 1))
    return (
        "---\n"
        "name: big-sop\n"
        "description: 长正文业务技能\n"
        "version: 0.4.0\n"
        "producer-kind: executable_sop\n"
        "---\n\n"
        "# 总览\n\n本技能处理长正文业务流程。\n\n"
        "## 工作流程\n\n" + workflow_body + "\n\n"
        "## 输出格式\n\n结果一律使用 Markdown 表格输出。\n\n"
        "## 安全边界\n\n未经确认不得执行不可逆操作。\n"
    )


BIG_DEFS = [
    {"module_id": "workflow", "type": "capability.rollout", "writable": True, "heading": "## 工作流程"},
    {"module_id": "output-format", "type": "shared.response", "writable": True, "heading": "## 输出格式"},
    {"module_id": "guardrails", "type": "shared.guardrails", "writable": False, "heading": "## 安全边界"},
]

# 累计测试预算：单轮 ~750 净增字节合法，累计 1000 字节冻结上限
CUM_BUDGET = mu.freeze_budget(
    max_ops=4,
    max_round_added_bytes=900,
    max_round_deleted_bytes=900,
    max_round_estimated_tokens=300,
    max_round_growth_ratio=0.4,
    max_cumulative_added_bytes=1000,
    max_cumulative_growth_ratio=0.9,
)


def _append_patch(section_text: str, expected: str, appendix: str) -> dict:
    replacement = expected + appendix
    return {
        "target_module": "workflow",
        "base_text_sha256": "",  # 由调用方补齐
        "ops": [_replace_op(section_text, expected, replacement)],
        "rationale": "补充执行留痕说明",
    }


# ── 场景 1：合法单 Module 精确替换 → 程序合并，其余字节不变 ──────────────────


def test_s1_valid_single_module_exact_replace_merges_and_protects_rest(sandbox):
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    section = sm.module_section_text(spec, MULTI_SECTION_SKILL, "workflow")
    expected = "按顺序执行三个步骤，先核对输入再执行。"
    replacement = "按顺序执行三个步骤，先核对输入，逐条留痕，再执行。"
    patch = {
        "target_module": "workflow",
        "base_text_sha256": sha_text(MULTI_SECTION_SKILL),
        "ops": [_replace_op(section, expected, replacement)],
        "rationale": "补充留痕要求",
    }
    result = mu.apply_candidate_patch(
        spec,
        MULTI_SECTION_SKILL,
        patch,
        policy=POLICY_ON,
        exam_materials=EXAM_MATERIALS,
        case_ids=CASE_IDS,
    )
    assert result["ok"] is True, result
    candidate_text = result["candidate_text"]
    child = result["child_spec"]

    # 目标 Module = 替换文本（程序合并，模型没有输出整份文件）
    assert sm.module_section_text(child, candidate_text, "workflow") == section.replace(expected, replacement)
    # 其他 Module 区段字节不变
    for module_id in ("output-format", "guardrails"):
        assert sm.module_section_text(child, candidate_text, module_id) == sm.module_section_text(
            spec, MULTI_SECTION_SKILL, module_id
        )
    # frontmatter 与目标 span 前后正文逐字节不变：整份文本只在冻结 span 上发生变化
    boundary, _ = sm.frontmatter_boundary_of(candidate_text)
    original_boundary, _ = sm.frontmatter_boundary_of(MULTI_SECTION_SKILL)
    assert candidate_text[:boundary] == MULTI_SECTION_SKILL[:original_boundary]
    item = spec.module("workflow")
    abs_start = original_boundary + item.start
    abs_end = original_boundary + item.end
    assert candidate_text == (
        MULTI_SECTION_SKILL[:abs_start] + section.replace(expected, replacement) + MULTI_SECTION_SKILL[abs_end:]
    )
    # 子版本 ModuleSpec：模块逻辑 ID 与可写策略跨代不变
    for module in child.modules:
        parent = spec.module(module.module_id)
        assert (module.type, module.writable, module.heading) == (parent.type, parent.writable, parent.heading)
    # 业务 diff 单独返回；累计计数器代际推进
    assert "replace" in result["business_diff"]
    assert result["cumulative"]["generations"] == 1
    assert result["cumulative"]["added_bytes"] == len(replacement.encode("utf-8")) - len(expected.encode("utf-8"))


# ── 场景 2：拒绝面 + 拒绝历史（去重） ────────────────────────────────────────


def test_s2_rejection_surfaces_record_history_and_dedup(sandbox):
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    section = sm.module_section_text(spec, MULTI_SECTION_SKILL, "workflow")
    base_sha = sha_text(MULTI_SECTION_SKILL)
    history = mu.RejectionHistory()
    expected = "按顺序执行三个步骤，先核对输入再执行。"
    exam_answer = EXAM_CASES[0]["expected"]

    def attempt(patch, **kwargs):
        kwargs.setdefault("policy", POLICY_ON)
        kwargs.setdefault("exam_materials", EXAM_MATERIALS)
        kwargs.setdefault("case_ids", CASE_IDS)
        kwargs.setdefault("rejection_history", history)
        return mu.apply_candidate_patch(spec, MULTI_SECTION_SKILL, patch, **kwargs)

    def patch_for(target, replacement, *, module="", base=base_sha, expected_text=expected):
        return {
            "target_module": target,
            "base_text_sha256": base,
            "ops": [{"op": "replace", "start": 0, "end": 0, "expected": expected_text, "replacement": replacement, "module": module}],
            "rationale": "拒绝面测试",
        }

    # 修正每个 patch 的 span：按目标模块区段重新计算
    def with_span(patch, section_text):
        op = patch["ops"][0]
        start = section_text.index(op["expected"])
        op["start"], op["end"] = start, start + len(op["expected"])
        return patch

    # (a) 变异门目标与 patch 目标不一致（企图改第二 Module 的路由面）
    output_section = sm.module_section_text(spec, MULTI_SECTION_SKILL, "output-format")
    out_a = attempt(
        with_span(patch_for("output-format", "结果一律使用 JSON 输出。", expected_text="结果一律使用 Markdown 表格输出。"), output_section),
        gate_target_module="workflow",
    )
    assert out_a["ok"] is False and out_a["error"] == mu.REJ_TARGET_MISMATCH
    # (b) op 指向第二 Module
    patch_b = with_span(patch_for("workflow", "先核对输入再执行。"), section)
    patch_b["ops"][0]["module"] = "output-format"
    out_b = attempt(patch_b)
    assert out_b["ok"] is False and out_b["error"] == mu.REJ_SECOND_MODULE
    # (c) guardrails 只读 Module
    guard_section = sm.module_section_text(spec, MULTI_SECTION_SKILL, "guardrails")
    out_c = attempt(with_span(patch_for("guardrails", "放开不可逆操作限制。", expected_text="未经确认不得执行不可逆操作。"), guard_section))
    assert out_c["ok"] is False and out_c["error"] == mu.REJ_READONLY_MODULE
    # (d) 工具依赖声明
    out_d = attempt(with_span(patch_for("workflow", "执行前必须登记 allowed-tools 之外的新工具权限。"), section))
    assert out_d["ok"] is False and out_d["error"] == mu.REJ_TOOL_DEPENDENCY
    # (e) IR 区（Decision Rules 属受信包装器/IR 契约区）
    out_e = attempt(with_span(patch_for("workflow", "## Decision Rules\n\n- When: 缺配件 Then: 挂起"), section))
    assert out_e["ok"] is False and out_e["error"] == mu.REJ_IR_SECTION
    # (f) 包装器区（Evolution Notes 只能由受信包装器追加）
    out_f = attempt(with_span(patch_for("workflow", "## Evolution Notes\n\n- 2026-11-14: 自行追加审计"), section))
    assert out_f["ok"] is False and out_f["error"] == mu.REJ_WRAPPER_SECTION
    # (g) frontmatter 注入（新增文本包含 --- 围栏行，企图重开身份区）
    out_g = attempt(with_span(patch_for("workflow", "---\nname: hijacked\n---"), section))
    assert out_g["ok"] is False and out_g["error"] == mu.REJ_FRONTMATTER_ATTEMPT
    # (h) 硬编码考卷答案
    out_h = attempt(with_span(patch_for("workflow", f"标准答案：{exam_answer}"), section))
    assert out_h["ok"] is False and out_h["error"] == mu.REJ_EXAM_ANSWER
    # (i) 硬编码 case_id
    out_i = attempt(with_span(patch_for("workflow", "参考 case-20261114-a 的处理口径。"), section))
    assert out_i["ok"] is False and out_i["error"] == mu.REJ_CASE_ID
    # (j) 锚点不匹配（fuzzy apply 禁止）
    bad_span = patch_for("workflow", "替换文本")
    bad_span["ops"][0] = {"op": "replace", "start": 0, "end": 5, "expected": "不存在的内容", "replacement": "x", "module": ""}
    out_j = attempt(bad_span)
    assert out_j["ok"] is False and out_j["error"] == mu.REJ_ANCHOR_MISMATCH
    # (k) 新增文本复制兄弟 Module 标题 → 程序合并器拒绝（span 不再唯一）
    out_k = attempt(with_span(patch_for("workflow", "## 输出格式\n\n双份输出格式区。"), section))
    assert out_k["ok"] is False and out_k["error"] == "span_not_unique"

    # 拒绝历史：每条拒绝都有原因 + 有界 diff，按 Module 保存
    workflow_rejections = history.rejections_for("workflow")
    assert {e.reason for e in workflow_rejections} == {
        mu.REJ_SECOND_MODULE, mu.REJ_TOOL_DEPENDENCY, mu.REJ_IR_SECTION, mu.REJ_WRAPPER_SECTION,
        mu.REJ_FRONTMATTER_ATTEMPT, mu.REJ_EXAM_ANSWER, mu.REJ_CASE_ID, mu.REJ_ANCHOR_MISMATCH,
        "span_not_unique",
    }
    assert history.rejections_for("output-format")[0].reason == mu.REJ_TARGET_MISMATCH
    assert history.rejections_for("guardrails")[0].reason == mu.REJ_READONLY_MODULE
    for entry in history.entries:
        assert entry.diff and entry.patch_sha256 and entry.detail

    # 重复提交相同被拒 patch：再次拒绝且不重复存储
    total_before = len(history.entries)
    exam_patch = with_span(patch_for("workflow", f"标准答案：{exam_answer}"), section)
    dup = attempt(exam_patch)
    assert dup["ok"] is False and dup["error"] == mu.REJ_PREVIOUSLY_REJECTED
    assert dup["first_reason"] == mu.REJ_EXAM_ANSWER and dup["seen_count"] == 2
    assert len(history.entries) == total_before
    # 有效 patch 不受污染：合法编辑仍可应用（历史只拒重复被拒 patch）
    ok = attempt(with_span(patch_for("workflow", "按顺序执行三个步骤，先核对输入、留痕，再执行。"), section))
    assert ok["ok"] is True, ok


# ── 场景 3：累计冻结上限 + 硬顶不可绕过 + 无效 patch 不改生产 Skill ──────────


def test_s3_cumulative_cap_blocks_small_rounds_and_hard_caps_cannot_be_bypassed(sandbox):
    text = _big_section_skill()
    spec = sm.build_module_spec(text, BIG_DEFS)["spec"]
    section = sm.module_section_text(spec, text, "workflow")
    sha = sha_text(text)

    # 第 1 代：单轮 ~750 净增字节，在单轮与累计上限内 → 合法
    patch1 = _append_patch(section, _big_step(1), "；增补动作：" + "复核工单影像并归档。" * 25)
    patch1["base_text_sha256"] = sha
    round1 = mu.apply_candidate_patch(spec, text, patch1, policy=POLICY_ON, budget=CUM_BUDGET)
    assert round1["ok"] is True, round1
    cum1 = round1["cumulative"]
    assert cum1["added_bytes"] > 0 and cum1["generations"] == 1

    # 第 2 代：单轮同样小改，但相对种子累计净增超冻结上限 → 被挡
    child = round1["child_spec"]
    candidate_text = round1["candidate_text"]
    child_section = sm.module_section_text(child, candidate_text, "workflow")
    patch2 = _append_patch(child_section, _big_step(2), "；增补动作：" + "复核工单影像并归档。" * 25)
    patch2["base_text_sha256"] = sha_text(candidate_text)
    round2 = mu.apply_candidate_patch(
        child, candidate_text, patch2, policy=POLICY_ON, budget=CUM_BUDGET, cumulative=cum1
    )
    assert round2["ok"] is False and round2["error"] == mu.REJ_CUMULATIVE_EXCEEDED
    assert str(round2["reason"]).startswith("相对种子累计净增")

    # 上限硬顶不可绕过：调用方放大预算被夹到硬顶，超大 patch 仍拒绝
    inflated = mu.freeze_budget(
        max_round_added_bytes=10**9,
        max_round_deleted_bytes=10**9,
        max_round_estimated_tokens=10**9,
        max_round_growth_ratio=10.0,
        max_cumulative_added_bytes=10**9,
        max_cumulative_growth_ratio=10.0,
    )
    assert set(inflated.clamped_fields) == {
        "max_round_added_bytes", "max_round_deleted_bytes", "max_round_estimated_tokens",
        "max_round_growth_ratio", "max_cumulative_added_bytes", "max_cumulative_growth_ratio",
    }
    assert inflated.max_round_added_bytes == mu.HARD_MAX_ROUND_ADDED_BYTES
    patch3 = _append_patch(section, _big_step(3), "复核。" * 2000)
    patch3["base_text_sha256"] = sha
    out = mu.apply_candidate_patch(spec, text, patch3, policy=POLICY_ON, budget=inflated)
    assert out["ok"] is False and out["error"] == mu.REJ_BUDGET_EXCEEDED


def test_s3_invalid_patch_never_touches_production_skill(sandbox):
    text = _big_section_skill()
    skill_dir = _write_skill(sandbox, "big-sop", text)
    spec = sm.build_module_spec(text, BIG_DEFS)["spec"]
    manifest = sc.build_search_manifest(skill_dir=skill_dir, tenant="acme", space="sre", module_spec=spec)
    assert manifest["ok"] is True
    manifest = manifest["manifest"]
    section = sm.module_section_text(spec, text, "workflow")
    history = mu.RejectionHistory()

    before_bytes = skill_dir.joinpath("SKILL.md").read_bytes()
    before_listing = sorted(p.name for p in skill_dir.iterdir())

    # 锚点不匹配的无效 patch（带冻结 hash 绑定走完整管线）
    bad = {
        "target_module": "workflow",
        "base_text_sha256": sha_text(text),
        "ops": [{"op": "replace", "start": 0, "end": 5, "expected": "锚点已漂移的内容", "replacement": "x", "module": ""}],
        "rationale": "无效 patch",
    }
    out = mu.apply_candidate_patch(
        spec,
        text,
        bad,
        policy=POLICY_ON,
        skill_text_sha256=manifest.skill_text_sha256,
        exam_materials=EXAM_MATERIALS,
        case_ids=CASE_IDS,
        rejection_history=history,
    )
    assert out["ok"] is False and out["error"] == mu.REJ_ANCHOR_MISMATCH
    assert out["recorded"]["stored"] is True

    # 生产 Skill 零副作用：字节不变、无新增文件、无 evolve 台账、无生产 tier 审计行
    assert skill_dir.joinpath("SKILL.md").read_bytes() == before_bytes
    assert sorted(p.name for p in skill_dir.iterdir()) == before_listing
    assert not (sandbox / ".evoharness" / "skill-evolution" / "usage.jsonl").exists()
    assert not (sandbox / ".evoharness" / "skill-evolution" / "tier_audit.jsonl").exists()

    # 候选产物写冻结源目录 → 拒绝（新 patch 只写搜索命名空间隔离制品）
    guard = mu.save_candidate_text(manifest, str(skill_dir), {"ok": True, "final_text": "candidate"})
    assert guard["ok"] is False and guard["error"] == mu.REJ_FROZEN_SOURCE
    assert skill_dir.joinpath("SKILL.md").read_bytes() == before_bytes

    # 合法候选写搜索命名空间 → 成功，且源目录依旧不变
    ok_dir = sandbox / "search-root" / "acme" / "sre" / "big-sop" / manifest.search_id / "candidates" / "c-1"
    saved = mu.save_candidate_text(manifest, str(ok_dir), {"ok": True, "final_text": "candidate body"})
    assert saved["ok"] is True, saved
    assert skill_dir.joinpath("SKILL.md").read_bytes() == before_bytes


# ── 场景 4（S3 关键）：评测 hash == 交付 hash，组合 DE-18 晋级门 ─────────────


def _promotion_report(skill: str, active_snapshot_hash: str) -> dict:
    return {
        "generated_at": "2026-11-14T00:00:00Z",
        "thresholds": {},
        "skills": [
            {
                "skill": skill,
                "status": "healthy",
                "reasons": [],
                "retrieved": 5,
                "relevant": 3,
                "used": 2,
                "used_rate": 0.4,
                "relevance_rate": 0.6,
                "replay": {"count": 3, "promotion_test": 1},
                "eval": {"pass_rate": 1.0, "hard_failures": 0},
                "artifacts": {
                    "promotion": {"promoted": True, "status": "active_champion"},
                    "active_snapshot_hash": active_snapshot_hash,
                    "candidate_snapshot_hash": active_snapshot_hash,
                },
            }
        ],
    }


def test_s4_evaluated_hash_equals_delivered_hash_and_promotion_gate_combo(sandbox):
    text = _big_section_skill(steps=8)
    skill_dir = _write_skill(sandbox, "big-sop", text)
    spec = sm.build_module_spec(text, BIG_DEFS)["spec"]
    manifest = sc.build_search_manifest(skill_dir=skill_dir, tenant="acme", space="sre", module_spec=spec)["manifest"]
    section = sm.module_section_text(spec, text, "workflow")

    patch = _append_patch(section, _big_step(1), "；增补动作：执行后回填工单影像。")
    patch["base_text_sha256"] = sha_text(text)
    applied = mu.apply_candidate_patch(spec, text, patch, policy=POLICY_ON)
    assert applied["ok"] is True, applied
    candidate_text = applied["candidate_text"]

    # 受信包装器最终包装：先渲染最终制品（业务 diff 与包装 diff 分开记录）
    packaged = mu.package_candidate(
        candidate_text,
        lesson="采用搜索冠军候选 bounded-patch",
        rationale="DE-32 S3 制品一致门验证",
    )
    assert packaged["ok"] is True, packaged
    assert packaged["wrapper_body"].startswith("## Evolution Notes")
    assert "采用搜索冠军候选" in packaged["wrapper_body"]
    assert packaged["final_content_hash"] != packaged["candidate_content_hash"]

    # 评测绑定最终交付 hash → 一致门放行
    evaluation = {
        "candidate_id": "cand-1",
        "evaluated_bundle_sha256": packaged["final_bundle_sha256"],
        "evaluated_content_hash": packaged["final_content_hash"],
    }
    aligned = mu.assert_evaluated_artifact_is_delivered(packaged, evaluation)
    assert aligned["ok"] is True, aligned

    # 评测绑定包装前裸候选（先测原始子文档再发布带 Notes 的不同制品）→ 拒绝
    # （release_bundle_sha256 即包装前正文的规范化内容 hash）
    pre_wrapper_bundle_sha = packaged["release_bundle_sha256"]
    assert pre_wrapper_bundle_sha != packaged["final_bundle_sha256"]
    stale = mu.assert_evaluated_artifact_is_delivered(
        packaged,
        {"candidate_id": "cand-1", "evaluated_bundle_sha256": pre_wrapper_bundle_sha},
    )
    assert stale["ok"] is False and stale["error"] == mu.REJ_EVALUATION_MISMATCH
    unbound = mu.assert_evaluated_artifact_is_delivered(packaged, {"candidate_id": "cand-1"})
    assert unbound["ok"] is False and unbound["error"] == mu.REJ_EVALUATION_UNBOUND

    # 显式采用认证候选（CAS 绑定 manifest 冻结的种子字节 hash）→ active 即最终包装
    release = mu.build_release_bundle(packaged)
    published = publish_skill_bundle(
        skill_name="big-sop",
        bundle=release["bundle"],
        expected_active_sha256=manifest.expected_active_sha256,
        lesson=release["publish_kwargs"]["lesson"],
        reason=release["publish_kwargs"]["reason"],
    )
    assert published["ok"] is True, published
    delivered_hash = st._current_active_content_hash("big-sop")
    assert delivered_hash == packaged["final_content_hash"]  # 实际评测 hash == 交付 hash

    # DE-18 晋级门：healthy + champion + active_snapshot_hash 一致 → 晋级成功
    report = _promotion_report("big-sop", packaged["final_content_hash"])
    promoted = st.apply_eval_promotions(report)
    assert [p["skill"] for p in promoted["promoted"]] == ["big-sop"], promoted

    # 评测后追加 Evolution Notes → active hash 变化 → 旧制品成绩晋级被拒（active_drift）
    assert st.set_tier("big-sop", st.TIER_EXPERIMENTAL, trigger="test_reset", actor="test")["ok"]
    skill_file = skill_dir / "SKILL.md"
    drifted = skill_file.read_text(encoding="utf-8").rstrip() + "\n\n## Evolution Notes\n\n- 2026-11-15: 评测后追加的包装\n"
    skill_file.write_text(drifted, encoding="utf-8")
    reset_skill_cache()  # 直接写盘后刷新技能缓存（生产路径由写入者统一刷新）
    assert st._current_active_content_hash("big-sop") != packaged["final_content_hash"]
    blocked = st.apply_eval_promotions(_promotion_report("big-sop", packaged["final_content_hash"]))
    assert promoted["promoted"] and not blocked["promoted"]
    drift = [s for s in blocked["skipped"] if s["skill"] == "big-sop"]
    assert drift and drift[0]["why"].startswith("active_drift"), blocked

    # 拿追加过内容的 bundle 冒用旧制品 hash 发布 → bundle_hash_mismatch
    smuggled = dict(release["bundle"])
    smuggled["instructions"] = smuggled["instructions"] + "\n\n## Evolution Notes\n\n- 评测后再追加一段"
    refused = publish_skill_bundle(
        skill_name="big-sop",
        bundle=smuggled,
        lesson=release["publish_kwargs"]["lesson"],
        reason=release["publish_kwargs"]["reason"],
    )
    assert refused["ok"] is False and refused["error"] == "bundle_hash_mismatch", refused


# ── 场景 5：关闭新策略 → 原候选/trigger 路由与原生链路回归保持 ───────────────


def test_s5_policy_disabled_keeps_legacy_routes_and_native_paths_uninterrupted(sandbox):
    history = mu.RejectionHistory()
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    section = sm.module_section_text(spec, MULTI_SECTION_SKILL, "workflow")
    patch = {
        "target_module": "workflow",
        "base_text_sha256": sha_text(MULTI_SECTION_SKILL),
        "ops": [_replace_op(section, "先核对输入再执行。", "先核对输入再执行，并留痕。")],
        "rationale": "策略关闭测试",
    }

    # 策略关闭：不产生 patch、不写历史
    out = mu.apply_candidate_patch(spec, MULTI_SECTION_SKILL, patch, policy=mu.MutationPolicy(), rejection_history=history)
    assert out["ok"] is False and out["error"] == mu.REJ_STRATEGY_DISABLED
    assert history.entries == []

    # 策略路由：新策略关闭 → legacy 回退；旧 heuristic/LLM/trigger 轨道原样放行
    route_off = mu.route_candidate_strategy(mu.MutationPolicy(), "bounded_module_patch")
    assert route_off["ok"] is False and route_off["error"] == mu.REJ_STRATEGY_DISABLED and route_off["route"] == "legacy"
    policy_on = mu.MutationPolicy(enable_bounded_module_edit=True)
    for requested in ("heuristic", "llm", "trigger"):
        route = mu.route_candidate_strategy(policy_on, requested)
        assert route["ok"] is True and route["route"] == "legacy" and route["strategy"] == requested
        assert route["bounded_module_edit"] is False  # 旧轨道不被新策略劫持

    # DE-31 反馈适配器：策略关闭 → 简报不触发变异；开启但无生成器 → fail-closed
    disabled_mutator = mu.make_feedback_mutator(mu.MutationPolicy())
    adapter_result = disabled_mutator({"target_module": "workflow"})
    assert adapter_result == {
        "invoked": False,
        "ok": False,
        "error": mu.REJ_STRATEGY_DISABLED,
        "note": "新编辑策略关闭：DE-31 简报不触发变异，原局部候选流程保持",
    }
    open_mutator = mu.make_feedback_mutator(policy_on, None)
    assert open_mutator({"target_module": "workflow"})["error"] == "no_patch_builder"

    # 原生链路不经单 Module 门（即使新策略开启）：普通在线沉淀照常写入
    from agents.skill_evolution import create_skill_file, evolve_skill_file

    created = create_skill_file(
        name="online-sediment",
        description="普通在线沉淀技能",
        instructions="# 步骤\n\n核对输入，执行，回填结果。",
    )
    assert created["ok"] is True, created
    evolved = evolve_skill_file(skill_name="online-sediment", lesson="沉淀一条新教训")
    assert evolved["ok"] is True, evolved

    # 投影重建照常进行（write_managed_projection 不经单 Module 门）
    from agents.skill_evolution import write_managed_projection

    projected = write_managed_projection(
        skills_root=sandbox / "proj-skills",
        evolution_root=sandbox / "proj-evo",
        name="feishu-hvac-sync",
        description="空调领域知识投影",
        instructions="# 领域知识\n\n空调故障码表。",
        when_to_use="空调领域问答",
        scope={"tenant": "acme", "owner": "ops"},
        source_revisions=[],
        actor="test",
    )
    assert projected["ok"] is True, projected


# ── 拒绝历史持久化与有界性 ──────────────────────────────────────────────────


def test_rejection_history_persists_and_stays_bounded(sandbox):
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    section = sm.module_section_text(spec, MULTI_SECTION_SKILL, "workflow")
    history = mu.RejectionHistory(max_per_module=2)
    sha = sha_text(MULTI_SECTION_SKILL)
    tiny_budget = mu.freeze_budget(
        max_round_added_bytes=5,
        max_round_deleted_bytes=5,
        max_round_estimated_tokens=5,
        max_round_growth_ratio=0.01,
    )
    for tag in ("tag-a", "tag-b", "tag-c"):
        patch = {
            "target_module": "workflow",
            "base_text_sha256": sha,
            "ops": [_replace_op(section, "先核对输入再执行。", f"先核对输入再执行（{tag}）。")],
            "rationale": tag,
        }
        out = mu.apply_candidate_patch(
            spec, MULTI_SECTION_SKILL, patch, policy=POLICY_ON, budget=tiny_budget, rejection_history=history, scope="s-1"
        )
        assert out["ok"] is False and out["error"] == mu.REJ_BUDGET_EXCEEDED

    # 有界：每 Module 只保留最新 2 条
    assert len(history.rejections_for("workflow")) == 2
    assert all("tag-b" in e.diff or "tag-c" in e.diff for e in history.rejections_for("workflow"))
    path = sandbox / "search-root" / "rejections.json"
    saved = mu.save_rejection_history(history, str(path))
    assert saved["ok"] is True
    loaded = mu.load_rejection_history(str(path))
    assert loaded.to_dict() == history.to_dict()
    # scope 去重：同 patch 在不同协议范围视为不同条目
    entry = history.rejections_for("workflow")[0]
    assert history.find(entry.patch_sha256, scope="s-2") is None
    assert history.find(entry.patch_sha256, scope="s-1") is not None


# ── 变异提示词契约（真实模型路径材料，离线只验渲染） ─────────────────────────


def test_mutation_prompt_contract_renders_budget_and_rejection_history(sandbox):
    spec = sm.build_module_spec(MULTI_SECTION_SKILL, MULTI_DEFS)["spec"]
    budget = mu.freeze_budget(max_ops=3)
    history = mu.RejectionHistory()
    history.record(
        module_id="workflow",
        reason=mu.REJ_ANCHOR_MISMATCH,
        detail="锚点不匹配",
        patch_sha256="deadbeef",
        diff="replace [0:5]",
    )
    mutation_input = mu.build_mutation_input(
        spec=spec,
        raw_text=MULTI_SECTION_SKILL,
        target_module="workflow",
        brief={"target_module": "workflow", "signal": 0.9, "constraint": "只改目标 Module"},
        rejection_history=history,
        budget=budget,
    )
    assert mutation_input["ok"] is True
    assert mutation_input["target_module"]["module_id"] == "workflow"
    assert mutation_input["module_text"].startswith("## 工作流程")
    assert mutation_input["rejection_history"][0]["reason"] == mu.REJ_ANCHOR_MISMATCH
    assert mutation_input["edit_budget"]["max_ops"] == 3
    prompt = mu.render_mutation_prompt(mutation_input)
    assert "单 Module 变异器" in prompt
    assert "mutation_input" in prompt
    assert '"max_ops": 3' in prompt
    # 不存在的目标 Module → 拒绝（fail-closed）
    missing = mu.build_mutation_input(spec=spec, raw_text=MULTI_SECTION_SKILL, target_module="nope")
    assert missing["ok"] is False and missing["error"] == mu.REJ_MODULE_NOT_FOUND
