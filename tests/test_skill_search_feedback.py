"""DE-31 反馈编译回归：证据化逐 Case 归因、引用核验、变异门与 S3 安全。

覆盖 tasks.json DE-31 四条 acceptance_scenarios（全部离线：合成轨迹/评分向量/
注入归因提案，不调真实模型；prompt 文件只是设计目标，合成测试不依赖模型调用）：

1. 虚构 event ref / 跨 Case 证据 / 与结论无关引用 → 引用核验拒绝，不能驱动自动改写；
2. 全部成功 / 归因为环境或 rubric 问题 → 不调用变异器；高 confidence 但引用不足 → 停止；
3. H（封存集）内容不可进入提示或日志；长 Trace 按引用分块，未消费关键证据状态可见；
4. 来源是投影事实 / 无关用户反馈 / 未经核验 assistant 主张 → 不自动变异 SOP。

另覆盖：单 Module 硬证据放行（变异器恰好被调用一次 + 成功行为保持 + 原 IR
映射经 validate_record 校验 + 不复制轨迹原文）、多责任归因停止、最高信号并列、
零信号、低置信、只读 Module 保护、分流决策结构化落盘。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.skill_search import contracts as sc
from agents.skill_search import feedback as fb
from agents.skill_search import modules as sm
from agents.skill_search import protocol as sp


# ── 合成夹具：独立 SOP 技能（三 Module：两可写 + 一只读 guardrails） ─────────

SKILL_TEXT = """---
name: deploy-rollout
description: 部署发布独立 SOP
producer-kind: executable_sop
version: 1.0.0
---

## 工作流程
1. 灰度 5% 观察 10 分钟。
2. 全量并回填结果。

## 结果汇报
输出发布摘要与回滚说明。

## 安全边界
禁止未经授权的生产写入与凭据外发。
"""

MODULE_DEFS = [
    {"module_id": "cap.workflow", "type": "capability.workflow", "writable": True, "heading": "## 工作流程"},
    {"module_id": "cap.report", "type": "capability.report", "writable": True, "heading": "## 结果汇报"},
    {"module_id": "guard.readonly", "type": "shared.guardrails", "writable": False, "heading": "## 安全边界"},
]

TRACE_S1 = "trace://cand-parent/s-1"
TRACE_S2 = "trace://cand-parent/s-2"
BULK_MARKER = "BULK-TRACE-BODY-MARKER-9f3a"
H_QUESTION = "密封审计专用题：季度合规报告归档校验流程 XQ77"

PARENT = {"candidate_id": "cand-parent", "generation": 3, "attributor": "offline_synthetic"}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("de31-skill")
    skill_dir = root / "deploy-rollout"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
    built = sc.build_search_manifest(skill_dir=skill_dir, tenant="acme", space="ops")
    assert built["ok"], built
    spec_out = sm.build_module_spec(SKILL_TEXT, MODULE_DEFS, skill_name="deploy-rollout")
    assert spec_out["ok"], spec_out
    return {"manifest": built["manifest"], "spec": spec_out["spec"]}


def _question(case_id: str) -> str:
    return f"case {case_id} 执行部署发布任务 topic-{case_id}-alpha"


def _case(case_id: str, *, origin: str = "score", split: str = "mutate_dev",
          role: str = "S", question: str | None = None, **extra) -> dict:
    case = {
        "case_id": case_id,
        "origin_group": origin,
        "origin_split": split,
        "family": "deploy",
        "question": question or _question(case_id),
        "expected": f"case {case_id} 期望：灰度比例 10 percent 且生成发布摘要",
    }
    if role:
        case["protocol_role"] = role
    case.update(extra)
    return case


def _eval(case_id: str, *, terminal: str, score=None, dims=None,
          trace_ref: str = "", candidate: str = "cand-parent", **extra) -> dict:
    record = {
        "case_id": case_id,
        "candidate_id": candidate,
        "terminal_status": terminal,
        "execution_level": "tool_replay",
        "valid": terminal in (fb.TERMINAL_PASS, fb.TERMINAL_SCORED_FAIL),
        "score": score,
        "dimension_scores": dict(dims or {}),
        "trace_ref": trace_ref,
    }
    record.update(extra)
    return record


def _deploy_trace() -> list[dict]:
    return [
        {"event_id": "e-1", "kind": "user_message", "text": _question("s-1")},
        {"event_id": "e-2", "kind": "tool_call", "text": "tool deploy_rollout set canary ratio 5 percent for service A"},
        {"event_id": "e-3", "kind": "verdict", "text": "verifier failed canary ratio mismatch expected 10 percent got 5"},
    ]


def _proposal(case_id: str = "s-1", *, trace_ref: str = TRACE_S1, confidence: float = 0.9,
              module_id: str = "cap.workflow", citations=None, **extra) -> dict:
    proposal = {
        "case_id": case_id,
        "category": fb.ATTR_SKILL_ISSUE,
        "sub_kind": fb.SKILL_SUB_BODY_DEFECT,
        "module_id": module_id,
        "citations": citations
        if citations is not None
        else [{"event_ref": f"{trace_ref}#e-2"}, {"event_ref": f"{trace_ref}#e-3"}],
        "observed": "canary ratio set to 5 percent",
        "expected": "canary ratio should be 10 percent",
        "failure_atoms": ["canary ratio wrong"],
        "confidence": confidence,
    }
    proposal.update(extra)
    return proposal


def _freeze_cases(cases: list[dict], *, sealed: dict | None = None):
    assigned = sp.assign_protocol_roles(cases, sealed_holdout=sealed)
    assert assigned["ok"], assigned
    frozen = sp.freeze_case_protocol(assigned["cases"])
    assert frozen["ok"], frozen
    return frozen["freeze"], assigned["cases"]


def _compile(world, *, cases, evaluations, attributions, store, sealed=None,
             consumed=None, gates=None, mutator=None, trace_refs=None):
    freeze, assigned = _freeze_cases(cases, sealed=sealed)
    return fb.compile_feedback(
        parent=PARENT,
        freeze=freeze,
        cases=assigned,
        evaluations=evaluations,
        trace_store=store,
        attributions=attributions,
        module_spec=world["spec"],
        manifest=world["manifest"],
        trace_refs=trace_refs,
        consumed_chunks=consumed,
        gates=gates,
        mutator=mutator,
    )


def _spy_mutator():
    calls: list[dict] = []

    def mutator(brief: dict):
        calls.append(brief)
        return {"ok": True, "patch_ref": "patch-synthetic-1"}

    return calls, mutator


def _gate(record: dict) -> dict:
    return record["mutation_gate"]


# ── 场景 1：虚构 event ref / 跨 Case 证据 / 无关引用 → 不能驱动自动改写 ─────


def test_s1_fabricated_event_ref_cannot_drive_mutation(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    calls, mutator = _spy_mutator()
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        attributions=[_proposal(citations=[{"event_ref": f"{TRACE_S1}#e-does-not-exist"}])],
        store=store,
        mutator=mutator,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.REJECT_CITATION_NOT_FOUND
    assert record["mutation_candidates"] == [] and calls == []
    codes = [b["code"] for b in record["blockers"]]
    assert fb.REJECT_CITATION_NOT_FOUND in codes
    # 分流决策可见：被拒条目进人工复核通道并带停止原因码
    triage = next(t for t in record["triage"] if t["case_id"] == "s-1")
    assert triage["route"] == fb.ROUTE_MANUAL_CLARIFICATION and triage["write"] is False
    assert fb.REJECT_CITATION_NOT_FOUND in triage["reason_codes"]


def test_s1_cross_case_citation_cannot_drive_mutation(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace(), TRACE_S2: _deploy_trace()})
    calls, mutator = _spy_mutator()
    record = _compile(
        world,
        cases=[_case("s-1"), _case("s-2")],
        evaluations=[
            _eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1),
            _eval("s-2", terminal=fb.TERMINAL_SCORED_FAIL, score=0.3, trace_ref=TRACE_S2),
        ],
        # s-1 的提案引用 s-2 轨迹里的真实事件：跨 Case 证据无效
        attributions=[_proposal(case_id="s-1", citations=[{"event_ref": f"{TRACE_S2}#e-2"}])],
        store=store,
        mutator=mutator,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.REJECT_CITATION_CROSS_CASE
    assert calls == [] and record["mutation_candidates"] == []


def test_s1_irrelevant_citation_cannot_drive_mutation(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        # e-1 是真实存在的用户问句事件，但与"canary 比例错误"结论零词元重叠
        attributions=[_proposal(citations=[{"event_ref": f"{TRACE_S1}#e-1"}])],
        store=store,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.REJECT_CITATION_IRRELEVANT


def test_s1_any_rejected_citation_downgrades_whole_item(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        # 一条真实硬证据 + 一条虚构引用：引用完整性破坏 → 整条降级（fail-closed）
        attributions=[
            _proposal(citations=[{"event_ref": f"{TRACE_S1}#e-2"}, {"event_ref": f"{TRACE_S1}#fabricated"}])
        ],
        store=store,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.REJECT_CITATION_NOT_FOUND
    assert record["mutation_candidates"] == []


# ── 场景 2：全成功/环境/rubric → 不调用变异器；高 confidence 不替代证据 ─────


def test_s2_all_passed_preserves_behavior_and_skips_mutator(world):
    calls, mutator = _spy_mutator()
    store = fb.TraceStore(
        {
            TRACE_S1: _deploy_trace(),
            TRACE_S2: [
                {"event_id": "v-1", "kind": "user_message", "text": _question("s-2")},
                {"event_id": "v-2", "kind": "verdict", "text": "verifier passed canary ratio 10 percent"},
            ],
        }
    )
    record = _compile(
        world,
        cases=[_case("s-1"), _case("s-2")],
        evaluations=[
            _eval("s-1", terminal=fb.TERMINAL_PASS, score=1.0, trace_ref=TRACE_S1),
            _eval("s-2", terminal=fb.TERMINAL_PASS, score=1.0, trace_ref=TRACE_S2),
        ],
        attributions=[_proposal(case_id="s-1")],  # 对成功 Case 的 SKILL_ISSUE 归因必须被拒
        store=store,
        mutator=mutator,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_ALL_PASSED
    assert record["mutation_candidates"] == [] and calls == []
    assert [p["case_id"] for p in record["preserve"]] == ["s-1", "s-2"]
    # 成功 Case 的行为保持条目必须携带评分引用（可核验的 score record）
    for entry in record["preserve"]:
        assert entry["citations"] and entry["citations"][0]["type"] == "score_record"
        assert entry["citations"][0]["hard_evidence"] is True
    codes = [b["code"] for b in record["blockers"]]
    assert fb.REJECT_SUCCESS_CASE in codes


def test_s2_infra_case_routes_environment_and_rejects_attribution(world):
    calls, mutator = _spy_mutator()
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_INFRA_ERROR, score=None, trace_ref=TRACE_S1)],
        attributions=[_proposal()],  # infra 终态无可比分数：归因提案不受理
        store=store,
        mutator=mutator,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_ENVIRONMENT_ISSUE
    assert calls == [] and record["mutation_candidates"] == []
    triage = record["triage"][0]
    assert triage["route"] == fb.ROUTE_ENVIRONMENT_REPAIR and triage["kind"] == "environment"
    assert any(b["code"] == fb.REJECT_CASE_NOT_SCORED for b in record["blockers"])


def test_s2_rubric_attribution_stops_mutator(world):
    calls, mutator = _spy_mutator()
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        attributions=[
            {
                "case_id": "s-1",
                "category": fb.ATTR_RUBRIC_ISSUE,
                "citations": [{"event_ref": f"{TRACE_S1}#e-3"}],
                "observed": "verifier rubric dimension missing causes wrong score",
                "expected": "rubric 应包含 canary ratio 维度",
                "confidence": 0.9,
            }
        ],
        store=store,
        mutator=mutator,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_RUBRIC_ISSUE
    assert calls == [] and record["mutation_candidates"] == []
    triage = record["triage"][0]
    assert triage["route"] == fb.ROUTE_RUBRIC_REVISION and triage["write"] is False
    assert "不与旧分数混比较" in triage["note"]


def test_s2_high_confidence_without_evidence_stops(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    calls, mutator = _spy_mutator()
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        attributions=[_proposal(confidence=0.99, citations=[])],
        store=store,
        mutator=mutator,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_HIGH_CONF_NO_EVIDENCE
    assert calls == []


def test_s2_no_attributor_configured_is_fail_closed(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        attributions=None,
        store=store,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_NO_ATTRIBUTOR


# ── 场景 3：H 内容不进提示/日志；长 Trace 分块；未消费证据状态可见 ───────────


def _sealed_world_cases():
    cases = [
        _case("s-1"),
        _case("s-2"),
        _case("h-1", origin="curated_run", split="", role="H", question=H_QUESTION),
    ]
    sealed = {"ref": "sealed-holdout-2026w46", "sha256": "a" * 64, "case_ids": ["h-1"]}
    return cases, sealed


def test_s3_sealed_h_excluded_from_prompts_and_record(world):
    cases, sealed = _sealed_world_cases()
    store = fb.TraceStore({TRACE_S1: _deploy_trace(), TRACE_S2: _deploy_trace()})
    h_case = next(c for c in cases if c["case_id"] == "h-1")
    s_cases = [c for c in cases if c["protocol_role"] == "S"]
    h_eval = _eval("h-1", terminal=fb.TERMINAL_PASS, score=1.0, trace_ref="trace://sealed/h-1")

    # H Case 直接构建归因输入 → 立即抛泄漏（fail-closed）
    with pytest.raises(fb.SealedContentLeak):
        fb.build_attribution_input(case=h_case, evaluation=h_eval, trace_store=store)

    inputs = fb.attribution_inputs_for_search_set(
        cases=s_cases,
        evaluations=[
            _eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1),
            _eval("s-2", terminal=fb.TERMINAL_PASS, score=1.0, trace_ref=TRACE_S2),
        ],
        s_case_ids=["s-1", "s-2"],
        trace_store=store,
        module_spec=world["spec"],
    )
    assert [i["case_id"] for i in inputs] == ["s-1"]  # 只为 S 的有效失败构建输入
    prompts = [fb.render_attribution_prompt(item) for item in inputs]
    assert prompts and all(H_QUESTION not in p and "XQ77" not in p for p in prompts)

    calls, mutator = _spy_mutator()
    record = _compile(
        world,
        cases=cases,
        evaluations=[
            _eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1),
            _eval("s-2", terminal=fb.TERMINAL_PASS, score=1.0, trace_ref=TRACE_S2),
            h_eval,  # H 评分混入也必须被排除
        ],
        attributions=[_proposal()],
        store=store,
        sealed=sealed,
        mutator=mutator,
    )
    dump = json.dumps(record, ensure_ascii=False)
    assert H_QUESTION not in dump and "XQ77" not in dump
    assert record["scope"]["h_cases_excluded"] == ["h-1"]
    assert "h-1" not in record.get("evaluation_refs", {})
    assert any(b["code"] == "sealed_case_evaluation_excluded" for b in record["blockers"])
    assert "h-1" not in {p["case_id"] for p in record["preserve"]} | {i["case_id"] for i in record["items"]}
    # 日志终检原语本身工作：记录可安全落日志，含 H 内容的文本会抛错
    fb.assert_no_sealed_content(dump, [H_QUESTION], context="unit-log-check")


def test_s3_long_trace_chunked_by_reference_and_unconsumed_visible(world):
    events = [
        {"event_id": f"e-{i}", "kind": "tool_call", "text": f"tool step {i} " + "x" * 40}
        for i in range(1, 7)
    ]
    events[2]["text"] = "tool deploy_rollout set canary ratio 5 percent for service A"
    events[5]["text"] = "verifier failed canary ratio mismatch expected 10 percent got 5"
    store = fb.TraceStore({TRACE_S1: events})
    gates = fb.AttributionGates(max_chunk_chars=60)
    chunks = store.chunks(TRACE_S1, max_chars=60)
    assert len(chunks) > 1  # 长 Trace 按引用分块

    # 归因输入反映消费状态：只声明消费第一个分块
    proposal = _proposal(citations=[{"event_ref": f"{TRACE_S1}#e-3"}, {"event_ref": f"{TRACE_S1}#e-6"}])
    consumed_all = [c.ref for c in chunks]
    partial = [chunks[0].ref]
    inputs = fb.attribution_inputs_for_search_set(
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        s_case_ids=["s-1"],
        trace_store=store,
        consumed_chunks={"s-1": partial},
        module_spec=world["spec"],
        max_chars=60,
    )
    assert inputs[0]["consumed_chunk_refs"] == partial
    assert inputs[0]["unconsumed_chunk_refs"] == [r for r in consumed_all if r not in partial]

    calls, mutator = _spy_mutator()
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        attributions=[proposal],
        store=store,
        consumed={"s-1": partial},
        gates=gates,
        mutator=mutator,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_UNCONSUMED_EVIDENCE
    assert calls == []
    blocker = next(b for b in record["blockers"] if b["code"] == fb.STOP_UNCONSUMED_EVIDENCE)
    # 未消费关键证据状态可见：未读分块 ref + 其事件清单都在 blocker 里
    assert blocker["unconsumed"] and all("chunk_ref" in u and "event_ids" in u for u in blocker["unconsumed"])
    unconsumed_events = {e for u in blocker["unconsumed"] for e in u["event_ids"]}
    assert "e-6" in unconsumed_events  # 关键 verdict 证据在未消费分块中，状态可见


def test_s3_log_guard_raises_on_sealed_leak(world):
    cases, sealed = _sealed_world_cases()
    assert fb.scan_sealed_content("正常日志，无泄漏", [H_QUESTION]) == []
    assert fb.scan_sealed_content(f"日志里混入 {H_QUESTION}", [H_QUESTION]) == [H_QUESTION]
    with pytest.raises(fb.SealedContentLeak):
        fb.assert_no_sealed_content(f"日志里混入 {H_QUESTION}", [H_QUESTION], context="log-guard")
    # 短样本防误报：小于 8 字符的样本不参与扫描
    assert fb.scan_sealed_content("XQ77 出现", ["XQ77"]) == []


# ── 场景 4：投影事实 / 无关用户反馈 / 未核验 assistant 主张 → 不自动变异 ─────


def test_s4_projection_fact_routes_to_knowledge_source_no_mutation(world):
    store = fb.TraceStore(
        {
            TRACE_S1: [
                {"event_id": "p-1", "kind": "projection_note", "text": "知识投影：服务 A 的金丝雀发布比例为 10 percent"},
            ]
        }
    )
    calls, mutator = _spy_mutator()
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.3, trace_ref=TRACE_S1)],
        attributions=[
            {
                "case_id": "s-1",
                "category": fb.ATTR_SKILL_ISSUE,
                "sub_kind": fb.SKILL_SUB_KNOWLEDGE_FACT,
                "module_id": "cap.workflow",
                "citations": [{"event_ref": f"{TRACE_S1}#p-1"}],
                "observed": "金丝雀发布比例应为 10 percent",
                "expected": "知识源中服务 A 的金丝雀比例需要修订",
                "confidence": 0.9,
            }
        ],
        store=store,
        mutator=mutator,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_PROJECTION_SOURCE
    assert calls == [] and record["mutation_candidates"] == []
    triage = record["triage"][0]
    assert triage["route"] == fb.ROUTE_KNOWLEDGE_SOURCE_SERVICE
    assert triage["route_ref"]["write"] is False
    assert "DE-16/17" in triage["route_ref"]["semantics"]  # 引用源服务语义，不直接写


def test_s4_unrelated_user_feedback_rejected_by_relevance(world):
    store = fb.TraceStore(
        {TRACE_S1: [{"event_id": "u-0", "kind": "user_message", "text": "请顺带帮我订一杯咖啡"}]}
    )
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.3, trace_ref=TRACE_S1)],
        attributions=[
            _proposal(
                citations=[{"event_ref": f"{TRACE_S1}#u-0"}],
                observed="canary ratio wrong for service A",
                expected="canary ratio should be 10 percent",
            )
        ],
        store=store,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.REJECT_CITATION_IRRELEVANT


def test_s4_preference_conflict_routes_to_maintainer_no_mutation(world):
    store = fb.TraceStore(
        {
            TRACE_S1: [
                {
                    "event_id": "u-1",
                    "kind": "user_message",
                    "text": "用户现在要求发布不要灰度，直接全量发布",
                }
            ]
        }
    )
    calls, mutator = _spy_mutator()
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.3, trace_ref=TRACE_S1)],
        attributions=[
            {
                "case_id": "s-1",
                "category": fb.ATTR_SKILL_ISSUE,
                "sub_kind": fb.SKILL_SUB_PREFERENCE_CONFLICT,
                "module_id": "cap.workflow",
                "citations": [{"event_ref": f"{TRACE_S1}#u-1"}],
                "observed": "用户偏好全量发布 不要灰度",
                "expected": "稳定偏好冲突交原 Maintainer 裁决",
                "confidence": 0.9,
            }
        ],
        store=store,
        mutator=mutator,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_NO_HARD_EVIDENCE
    assert calls == [] and record["mutation_candidates"] == []
    triage = record["triage"][0]
    assert triage["route"] == fb.ROUTE_ONLINE_MAINTAINER
    assert triage["route_ref"]["write"] is False
    assert triage["route_ref"]["entry"].endswith("maintain_online_skill_candidate")


def test_s4_unverified_assistant_claim_no_mutation(world):
    store = fb.TraceStore(
        {
            TRACE_S1: [
                {
                    "event_id": "a-1",
                    "kind": "assistant_message",
                    "verified": False,
                    "text": "我认为灰度比例 5 percent 是正确的配置",
                }
            ]
        }
    )
    calls, mutator = _spy_mutator()
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.3, trace_ref=TRACE_S1)],
        attributions=[
            _proposal(
                citations=[{"event_ref": f"{TRACE_S1}#a-1"}],
                observed="灰度比例 5 percent 被主张为正确",
                expected="灰度比例应为 10 percent",
            )
        ],
        store=store,
        mutator=mutator,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_UNVERIFIED_CLAIM
    assert calls == [] and record["mutation_candidates"] == []


# ── 补充：放行路径、多责任、并列、零信号、低置信、只读保护、落盘 ─────────────


def test_happy_single_module_invokes_mutator_once_and_maps_ir(world):
    store = fb.TraceStore(
        {
            TRACE_S1: [*_deploy_trace(), {"event_id": "e-9", "kind": "tool_call", "text": f"noise {BULK_MARKER} padding"}],
            TRACE_S2: [
                {"event_id": "v-1", "kind": "user_message", "text": _question("s-2")},
                {"event_id": "v-2", "kind": "verdict", "text": "verifier passed canary ratio 10 percent"},
            ],
        }
    )
    calls, mutator = _spy_mutator()
    record = _compile(
        world,
        cases=[_case("s-1"), _case("s-2")],
        evaluations=[
            _eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1),
            _eval("s-2", terminal=fb.TERMINAL_PASS, score=1.0, trace_ref=TRACE_S2),
        ],
        attributions=[_proposal()],
        store=store,
        mutator=mutator,
    )
    gate = _gate(record)
    assert gate["allowed"] is True and gate["module_id"] == "cap.workflow"
    assert abs(gate["signal"] - 0.72) < 1e-9  # (1-0.2)*0.9
    # 变异器恰好被调用一次，简报带目标 Module、证据引用与成功行为保持集
    assert len(calls) == 1
    brief = calls[0]
    assert brief["target_module"] == "cap.workflow"
    assert brief["citations"] and all(c.get("hard_evidence") for c in brief["citations"])
    assert [p["case_id"] for p in brief["preserve"]] == ["s-2"]
    assert record["mutation_result"]["invoked"] is True
    # 生成版本记录：generation / bundle hash / 协议指纹
    assert record["generation"] == 3
    assert record["bundle_hash"] == world["manifest"].bundle_hash
    assert record["protocol_fingerprint"]
    # 成功 Case 行为保护条目（评分引用可核验）
    assert [p["case_id"] for p in record["preserve"]] == ["s-2"]
    assert record["preserve"][0]["citations"][0]["type"] == "score_record"
    # 原 IR 映射：experience-standard/v1 记录 + event 引用，validate_record 零错误
    ir = record["items"][0]["ir"]
    assert ir is not None and ir["validation_errors"] == []
    assert ir["record"]["schema_version"] == "experience-standard/v1"
    assert ir["record"]["identity"]["primary_type"] == "failure_pattern"
    observations = ir["record"]["evidence"]["observations"]
    assert observations and all(o.get("event_refs") for o in observations)
    # 不新增第二份原始 Trace 库：未被引用的轨迹原文不进反馈记录
    assert BULK_MARKER not in json.dumps(record, ensure_ascii=False)
    assert sorted(record["trace_refs"]) == sorted([TRACE_S1, TRACE_S2])
    assert record["trace_refs"] == sorted(set(record["trace_refs"]))  # 只存引用清单


def test_multi_module_responsibility_stops(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        attributions=[
            _proposal(
                module_hypotheses=[
                    {"module_id": "cap.workflow", "confidence": 0.8},
                    {"module_id": "cap.report", "confidence": 0.8},
                ]
            )
        ],
        store=store,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_MULTI_MODULE
    assert _gate(record)["blockers"][0]["module_ids"] == ["cap.report", "cap.workflow"]


def test_tied_top_signal_stops(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace(), TRACE_S2: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1"), _case("s-2")],
        evaluations=[
            _eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1),
            _eval("s-2", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S2),
        ],
        attributions=[
            _proposal(case_id="s-1", trace_ref=TRACE_S1, module_id="cap.workflow"),
            _proposal(case_id="s-2", trace_ref=TRACE_S2, module_id="cap.report"),
        ],
        store=store,
    )
    gate = _gate(record)
    assert gate["allowed"] is False
    assert gate["reason"] == fb.STOP_TIED_TOP_SIGNAL
    assert sorted(gate["tied_modules"]) == ["cap.report", "cap.workflow"]


def test_zero_signal_stops(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1")],
        # terminal=fail 但 score=1.0：(1-score)*confidence = 0 → 零信号
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=1.0, trace_ref=TRACE_S1)],
        attributions=[_proposal()],
        store=store,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_ZERO_SIGNAL


def test_low_confidence_stops(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        attributions=[_proposal(confidence=0.4)],
        store=store,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_LOW_CONFIDENCE
    assert _gate(record)["blockers"][0]["confidence"] == 0.4


def test_readonly_module_protected(world):
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1")],
        evaluations=[_eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1)],
        attributions=[_proposal(module_id="guard.readonly")],
        store=store,
    )
    assert _gate(record)["allowed"] is False
    assert _gate(record)["reason"] == fb.STOP_READONLY_MODULE
    assert _gate(record)["blockers"][0]["module_id"] == "guard.readonly"


def test_save_feedback_writes_structured_triage_to_disk(world, tmp_path):
    store = fb.TraceStore({TRACE_S1: _deploy_trace()})
    record = _compile(
        world,
        cases=[_case("s-1"), _case("s-2")],
        evaluations=[
            _eval("s-1", terminal=fb.TERMINAL_SCORED_FAIL, score=0.2, trace_ref=TRACE_S1),
            _eval("s-2", terminal=fb.TERMINAL_INFRA_ERROR, score=None, trace_ref=TRACE_S2),
        ],
        attributions=[_proposal()],
        store=store,
    )
    out = fb.save_feedback(record, tmp_path / "search" / "feedback.json")
    assert out["ok"] is True and Path(out["path"]).is_file()
    reloaded = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    # 分流决策结构化落盘：每条都有 route/write/reason_codes
    assert reloaded["triage"]
    for entry in reloaded["triage"]:
        assert entry["route"] and entry["write"] is False and entry["reason_codes"]
    assert reloaded["mutation_gate"]["allowed"] is True
    assert reloaded["feedback_id"] == record["feedback_id"]
