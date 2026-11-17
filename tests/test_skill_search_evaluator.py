"""DE-30 评测回归：真实 Run 执行适配边界、完整性门、触发路由、公平对照与兼容。

覆盖 tasks.json DE-30 六条 acceptance_scenarios（全部离线：合成候选/合成执行器/
合成 verifier，不调真实模型）：

1. 明知有工具缺陷的候选不能仅凭 LLM 重答文本拿到 artifact_verified；
2. 缺题/重复 ID/NaN/旧 rubric/错误候选加载均不能产生有效完整分数向量；
3. 429/环境故障不被伪装技能 0 分；有权限/安全违反直接隔离且证据可追踪；
4. routing 变化另走实际检索/竞争/加载链的正负例，强制注入成绩不能证明触发优化；
5. 旧局部候选与新策略使用同一执行适配器公平对照，关闭新适配仍返回旧报告兼容字段；
6. 出生题成功只作开发证据；实际 used 标注不自动变成任务成功，组合冲突必须重新评测。

另覆盖：缓存键覆盖完整协议 + DE-29 复用门串联、工具/产物验证走 DE-11 隔离通道
（合成执行器注入）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.skill_search import contracts as sc
from agents.skill_search import evaluator as ev
from agents.skill_search import protocol as sp


# ── 合成夹具 ────────────────────────────────────────────────────────────────

CAND_SNAPSHOT = {
    "name": "deploy-rollout",
    "description": "部署发布独立 SOP",
    "when_to_use": "deploy release canary rollout",
    "instructions": "1. 灰度 5% 观察 10 分钟。2. 全量并回填结果。",
}

DEFECT_SNAPSHOT = {
    **CAND_SNAPSHOT,
    "name": "deploy-rollout-defective",
    "known_tool_defects": ["tool:db_query returns stale rows"],
}


def _snapshot_with_defect() -> dict:
    return dict(DEFECT_SNAPSHOT)


def _case(case_id: str, *, origin: str = "score", split: str = "mutate_dev",
          question: str = "please run the deploy rollout for service A",
          verifier=None, **extra) -> dict:
    case = {
        "case_id": case_id,
        "origin_group": origin,
        "origin_split": split,
        "family": "deploy",
        "question": question,
    }
    if verifier is not None:
        case["verifier"] = verifier
    case.update(extra)
    return case


def _pass_verifier(response_text: str, case: dict | None = None) -> dict:
    return {"passed": True, "scores": {"accuracy": 1.0, "format": 1.0}}


def _fail_verifier(response_text: str, case: dict | None = None) -> dict:
    return {"passed": False, "scores": {"accuracy": 0.2}}


def _freeze_all_s(cases: list[dict]) -> "sp.CaseFreeze":
    assigned = sp.assign_protocol_roles([{**c, "protocol_role": "S"} for c in cases])
    assert assigned["ok"] is True, assigned
    frozen = sp.freeze_case_protocol(assigned["cases"])
    assert frozen["ok"] is True
    return frozen["freeze"]


def _cases_with_verifier(verdict_fn) -> list[dict]:
    return [
        _case("case-a", verifier=verdict_fn),
        _case("case-b", verifier=verdict_fn),
    ]


# ── 场景 1：明知有工具缺陷的候选不能仅凭 LLM 重答文本拿到 artifact_verified ──


def test_s1_known_tool_defect_cannot_claim_artifact_verified_from_text_only():
    candidate = ev.normalize_candidate(
        {"variant_id": "v-defect", "snapshot": _snapshot_with_defect(), "mutation_type": "llm"}
    )
    executor = ev.TextOnlySideQueryExecutor(
        side_query=_make_side_query("perfect text re-answer, every rubric satisfied")
    )
    case = _case("case-a", verifier=_pass_verifier)
    outcome = asyncio.run(
        executor.execute(case=case, candidate=candidate, frozen_protocol={"fingerprint": "fp-1"})
    )
    # 文本重答全部通过 rubric，但执行级别仍是 text_only：
    assert outcome.terminal_status == ev.TERMINAL_PASS
    assert outcome.score == 1.0
    assert outcome.execution_level == ev.LEVEL_TEXT_ONLY
    assert outcome.artifact_verified is False
    assert outcome.tool_events == [] and outcome.artifact_refs == []
    assert outcome.task_success is False
    assert not outcome.dev_evidence_only  # score 来源，但分数不可当 artifact_verified 成功


def test_s1_text_only_level_capped_even_if_record_tries_to_claim_more():
    candidate = ev.normalize_candidate({"candidate_id": "cand-x", "snapshot": dict(CAND_SNAPSHOT)})
    case = _case("case-a", verifier=_pass_verifier)
    outcome = asyncio.run(
        ev.TextOnlySideQueryExecutor(side_query=_make_side_query("text")).execute(
            case=case, candidate=candidate
        )
    )
    assert outcome.execution_level == ev.LEVEL_TEXT_ONLY
    assert outcome.artifact_verified is False


def test_s1_isolated_run_artifact_verified_requires_artifact_evidence():
    candidate = ev.normalize_candidate({"candidate_id": "cand-iso", "snapshot": dict(CAND_SNAPSHOT)})

    def run_executor(*, candidate, case, frozen_protocol):
        return {
            "loaded_snapshot": dict(candidate["snapshot"]),
            "response_text": "ran the rollout",
            "tool_events": [{"tool": "deploy", "ok": True, "authorized": True, "event_id": "t-1"}],
            # 声称 artifact_verified 但没有任何 artifact refs / verdict：
            "execution_level": ev.LEVEL_ARTIFACT_VERIFIED,
            "tokens": {"input_tokens": 10, "output_tokens": 5, "wall_ms": 12},
        }

    outcome = asyncio.run(
        ev.IsolatedRunExecutor(run_executor=run_executor).execute(
            case=_case("case-a", verifier=_pass_verifier), candidate=candidate
        )
    )
    assert outcome.execution_level == ev.LEVEL_TOOL_REPLAY
    assert outcome.artifact_verified is False


def test_s1_known_defect_capped_below_artifact_verified_without_fix_evidence():
    candidate = ev.normalize_candidate(
        {"candidate_id": "cand-defect", "snapshot": _snapshot_with_defect()}
    )

    def run_executor(*, candidate, case, frozen_protocol):
        return {
            "loaded_snapshot": dict(candidate["snapshot"]),
            "response_text": "done",
            "tool_events": [{"tool": "db_query", "ok": True, "authorized": True, "event_id": "t-2"}],
            "artifacts": [{"path": "rollout.json", "sha256": "abc"}],
            "artifact_verdict": {"verified": True, "reason": ""},
            "execution_level": ev.LEVEL_ARTIFACT_VERIFIED,
            "tokens": {"input_tokens": 10, "output_tokens": 5},
        }

    outcome = asyncio.run(
        ev.IsolatedRunExecutor(run_executor=run_executor).execute(
            case=_case("case-a", verifier=_pass_verifier), candidate=candidate
        )
    )
    # 产物验证声称通过，但候选明知有工具缺陷且无修复证据：级别封顶 tool_replay
    assert outcome.execution_level == ev.LEVEL_TOOL_REPLAY
    assert outcome.artifact_verified is False
    assert outcome.task_success is False

    def fixed_executor(*, candidate, case, frozen_protocol):
        record = run_executor(candidate=candidate, case=case, frozen_protocol=frozen_protocol)
        record["defect_fixed"] = True
        return record

    outcome_fixed = asyncio.run(
        ev.IsolatedRunExecutor(run_executor=fixed_executor).execute(
            case=_case("case-a", verifier=_pass_verifier), candidate=candidate
        )
    )
    assert outcome_fixed.execution_level == ev.LEVEL_ARTIFACT_VERIFIED
    assert outcome_fixed.artifact_verified is True
    assert outcome_fixed.task_success is True


# ── 场景 2：缺题/重复 ID/NaN/旧 rubric/错误候选加载 → 无有效完整分数向量 ──────


def _frozen_and_candidate():
    cases = _cases_with_verifier(_pass_verifier) + [_case("case-c", verifier=_pass_verifier)]
    freeze = _freeze_all_s(cases)
    candidate = ev.normalize_candidate({"candidate_id": "cand-main", "snapshot": dict(CAND_SNAPSHOT)})
    return freeze, candidate, cases


def _pass_outcomes(freeze, candidate, cases) -> list["ev.ExecutionOutcome"]:
    outcomes = asyncio.run(
        ev.evaluate_cases(
            executor=ev.TextOnlySideQueryExecutor(side_query=_make_side_query("text reply")),
            cases=cases,
            candidate=candidate,
            freeze=freeze,
        )
    )
    for outcome in outcomes:
        assert outcome.terminal_status == ev.TERMINAL_PASS
    return outcomes


def test_s2_complete_vector_baseline():
    freeze, candidate, cases = _frozen_and_candidate()
    outcomes = _pass_outcomes(freeze, candidate, cases)
    final = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=candidate)
    assert final["ok"] is True
    assert final["score_vector"] == {"case-a": 1.0, "case-b": 1.0, "case-c": 1.0}
    assert final["average_score"] == pytest.approx(1.0)
    assert final["cache_key"].startswith("evalcache-")


def test_s2_missing_case_blocks_vector():
    freeze, candidate, cases = _frozen_and_candidate()
    outcomes = _pass_outcomes(freeze, candidate, cases)[1:]  # 少跑 case-a
    final = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=candidate)
    assert final["ok"] is False
    assert final["reason"] == "missing_case_result"
    assert {"case_id": "case-a", "code": "missing_case_result"} in final["blocking"]
    assert final["score_vector"] is None


def test_s2_duplicate_case_result_blocks_vector():
    freeze, candidate, cases = _frozen_and_candidate()
    outcomes = _pass_outcomes(freeze, candidate, cases)
    outcomes.append(outcomes[0])  # case-a 重复执行两次
    final = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=candidate)
    assert final["ok"] is False
    assert final["reason"] == "duplicate_case_result"
    assert final["blocking"][0]["count"] == 2
    assert final["score_vector"] is None


def test_s2_nan_score_blocks_vector():
    freeze, candidate, cases = _frozen_and_candidate()
    outcomes = _pass_outcomes(freeze, candidate, cases)
    outcomes[1].score = float("nan")
    final = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=candidate)
    assert final["ok"] is False
    assert final["reason"] == "invalid_score_value"
    assert final["score_vector"] is None


def test_s2_out_of_range_score_blocks_vector():
    freeze, candidate, cases = _frozen_and_candidate()
    outcomes = _pass_outcomes(freeze, candidate, cases)
    outcomes[2].score = 1.5
    final = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=candidate)
    assert final["ok"] is False and final["reason"] == "invalid_score_value"


def test_s2_stale_rubric_fingerprint_blocks_vector():
    freeze, candidate, cases = _frozen_and_candidate()
    outcomes = _pass_outcomes(freeze, candidate, cases)
    outcomes[0].protocol_fingerprint = "old-rubric-fingerprint"  # 旧 rubric 跑出来的结果
    final = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=candidate)
    assert final["ok"] is False
    assert final["reason"] == "rubric_protocol_mismatch"
    assert final["blocking"][0]["expected"] == freeze.fingerprint
    assert final["score_vector"] is None


def test_s2_wrong_candidate_load_blocks_vector():
    freeze, candidate, cases = _frozen_and_candidate()
    outcomes = _pass_outcomes(freeze, candidate, cases)
    outcomes[1].loaded_hash = "sha-of-a-different-skill"
    final = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=candidate)
    assert final["ok"] is False
    assert final["reason"] == "candidate_load_mismatch"
    assert final["blocking"][0]["loaded"] == "sha-of-a-different-skill"
    assert final["score_vector"] is None


def test_s2_isolated_executor_reports_load_mismatch_from_receipt():
    candidate = ev.normalize_candidate({"candidate_id": "cand-m", "snapshot": dict(CAND_SNAPSHOT)})
    other_snapshot = {**CAND_SNAPSHOT, "instructions": "完全不同的旧版本正文"}

    def run_executor(*, candidate, case, frozen_protocol):
        return {"loaded_snapshot": dict(other_snapshot), "response_text": "x"}

    outcome = asyncio.run(
        ev.IsolatedRunExecutor(run_executor=run_executor).execute(
            case=_case("case-a", verifier=_pass_verifier), candidate=candidate
        )
    )
    assert outcome.terminal_status == ev.TERMINAL_INVALID_LOAD
    assert outcome.valid is False and outcome.score is None


def test_s2_candidate_id_mismatch_blocks_vector():
    freeze, candidate, cases = _frozen_and_candidate()
    outcomes = _pass_outcomes(freeze, candidate, cases)
    outcomes[0].candidate_id = "some-other-candidate"
    final = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=candidate)
    assert final["ok"] is False and final["reason"] == "candidate_id_mismatch"


# ── 场景 3：infra 错不伪装 0 分；越权直接隔离且证据可追踪 ────────────────────


def _make_side_query(text: str, exc: Exception | None = None):
    async def side_query(system: str, user: str) -> str:
        if exc is not None:
            raise exc
        return text

    return side_query


def test_s3_rate_limit_surfaces_as_infra_error_not_zero_score():
    candidate = ev.normalize_candidate({"candidate_id": "cand-429", "snapshot": dict(CAND_SNAPSHOT)})
    executor = ev.TextOnlySideQueryExecutor(
        side_query=_make_side_query("", exc=RuntimeError("429 rate limited: quota exceeded"))
    )
    outcome = asyncio.run(
        executor.execute(case=_case("case-a", verifier=_pass_verifier), candidate=candidate)
    )
    assert outcome.terminal_status == ev.TERMINAL_INFRA_ERROR
    assert outcome.valid is False
    assert outcome.score is None
    assert outcome.notes.startswith("infra failure")
    assert "429" in outcome.evidence[0]

    freeze = _freeze_all_s([_case("case-a", verifier=_pass_verifier), _case("case-b", verifier=_pass_verifier)])
    final = ev.finalize_score_vector(freeze=freeze, outcomes=[outcome], candidate=candidate)
    assert final["ok"] is False and final["reason"] == "infra_error"
    # 不产生可比分数：既没有把 429 记成技能 0 分，也没有用单题分凑一个"完整"向量
    assert final["score_vector"] is None


def test_s3_isolated_timeout_and_env_failure_are_infra():
    candidate = ev.normalize_candidate({"candidate_id": "cand-timeout", "snapshot": dict(CAND_SNAPSHOT)})

    def timeout_executor(*, candidate, case, frozen_protocol):
        return {"error": "watchdog: isolated run timed out after 600s", "loaded_snapshot": dict(candidate["snapshot"])}

    outcome = asyncio.run(
        ev.IsolatedRunExecutor(run_executor=timeout_executor).execute(
            case=_case("case-a", verifier=_pass_verifier), candidate=candidate
        )
    )
    assert outcome.terminal_status == ev.TERMINAL_INFRA_ERROR
    assert outcome.score is None

    def env_executor(*, candidate, case, frozen_protocol):
        return {"error": "env_missing: sandbox runtime unavailable"}

    outcome_env = asyncio.run(
        ev.IsolatedRunExecutor(run_executor=env_executor).execute(
            case=_case("case-a", verifier=_pass_verifier), candidate=candidate
        )
    )
    assert outcome_env.terminal_status == ev.TERMINAL_INFRA_ERROR


def test_s3_security_violation_quarantines_candidate_with_traceable_evidence():
    candidate = ev.normalize_candidate(
        {
            "candidate_id": "cand-rogue",
            "snapshot": {**CAND_SNAPSHOT, "allowed_tools": "read_file,write_file"},
        }
    )

    def rogue_executor(*, candidate, case, frozen_protocol):
        return {
            "loaded_snapshot": dict(candidate["snapshot"]),
            "response_text": "done",
            "tool_events": [
                {"tool": "read_file", "ok": True, "authorized": True, "event_id": "e-1"},
                {"tool": "drop_table", "ok": True, "authorized": False, "event_id": "e-9"},
            ],
            "artifacts": [{"path": "rollout.json", "sha256": "abc"}],
            "artifact_verdict": {"verified": True, "reason": ""},
            "tokens": {"input_tokens": 8, "output_tokens": 4},
        }

    outcome = asyncio.run(
        ev.IsolatedRunExecutor(run_executor=rogue_executor).execute(
            case=_case("case-a", verifier=_pass_verifier), candidate=candidate
        )
    )
    assert outcome.terminal_status == ev.TERMINAL_SECURITY_VIOLATION
    assert outcome.valid is False and outcome.score is None
    assert any("unauthorized_tool=drop_table" in item and "e-9" in item for item in outcome.evidence)

    freeze = _freeze_all_s([_case("case-a", verifier=_pass_verifier), _case("case-b", verifier=_pass_verifier)])
    ok_outcome = asyncio.run(
        ev.TextOnlySideQueryExecutor(side_query=_make_side_query("ok")).execute(
            case=_case("case-b", verifier=_pass_verifier), candidate=candidate
        )
    )
    final = ev.finalize_score_vector(freeze=freeze, outcomes=[outcome, ok_outcome], candidate=candidate)
    assert final["ok"] is False and final["reason"] == "security_violation"
    # 直接隔离：其余题再高分也不能抵消，候选进入 quarantine
    assert final["candidate_quarantined"] is True
    assert final["score_vector"] is None
    assert any(b["code"] == "security_violation" and b["evidence"] for b in final["blocking"])


# ── 场景 4：routing 走实际检索/竞争/加载链；强制注入不能证明触发优化 ──────────


def _baseline_corpus() -> dict[str, list[str]]:
    from agents.skills import skill_terms_from_text

    return {
        "deploy-rollout": skill_terms_from_text(
            "deploy-rollout", "部署发布 SOP", "deploy release canary", "Canary deploy steps."
        ),
        "db-failover": skill_terms_from_text(
            "db-failover", "数据库故障切换", "database failover replica recovery", "Failover steps."
        ),
        "git-tag": skill_terms_from_text(
            "git-tag", "打 tag 约定", "when creating git tags", "Semantic version tags."
        ),
    }


def test_s4_routing_passes_with_positive_negative_and_competition_queries():
    result = ev.evaluate_trigger_routing(
        skill_name="deploy-rollout",
        candidate_snapshot={
            **CAND_SNAPSHOT,
            "when_to_use": "deploy release canary rollout blue green runbook transition",
        },
        baseline_corpus=_baseline_corpus(),
        positive_queries=[
            "plan a canary deploy",
            "prepare the rollout release checklist",
            "run the blue green transition runbook",
        ],
        negative_queries=["database replica failover now", "push a git tag for the hotfix branch"],
        competitor_queries=[{"query": "database replica failover now", "expected": "db-failover"}],
        load_limit=3,
    )
    assert result["evaluated"] is True
    # 正例：经真实 BM25 链，目标 skill 真实进入加载窗口
    assert result["variant"]["positive"]["load_rate"] == pytest.approx(1.0)
    assert all(row["loaded"] for row in result["variant"]["positive"]["queries"])
    # 负例：不应触发的 query 不误触发（database/git-tag 查询不加载 deploy-rollout）
    assert result["variant"]["negative"]["false_trigger_rate"] == 0.0
    # 竞争：其他 skill 的正确触发场景中目标不抢加载，期望技能真实胜出
    assert result["variant"]["competitor"]["misfire_rate"] == 0.0
    assert result["variant"]["competitor"]["expected_won_rate"] == pytest.approx(1.0)
    assert result["routing_pass"] is True
    assert result["trigger_improved"] is True  # baseline 缺 blue/green 词：load_rate 2/3 → 3/3
    assert result["baseline"]["positive"]["load_rate"] == pytest.approx(2 / 3)
    assert result["load_limit"] == 3
    assert "db-failover" in result["variant"]["corpus_skill_names"]


def test_s4_broadened_trigger_fails_negative_and_competition_gates():
    result = ev.evaluate_trigger_routing(
        skill_name="deploy-rollout",
        candidate_snapshot={
            **CAND_SNAPSHOT,
            # 把数据库词写进触发条件：正例仍命中，但负例/竞争全挂
            "when_to_use": "deploy release canary rollout database failover replica",
        },
        baseline_corpus=_baseline_corpus(),
        positive_queries=["plan a canary deploy"],
        negative_queries=["database replica failover now"],
        competitor_queries=[{"query": "database replica failover now", "expected": "db-failover"}],
    )
    assert result["evaluated"] is True
    assert result["variant"]["negative"]["false_trigger_rate"] == pytest.approx(1.0)
    assert result["variant"]["competitor"]["misfire_rate"] == pytest.approx(1.0)
    assert result["routing_pass"] is False
    assert result["trigger_improved"] is False


def test_s4_forced_injection_scores_cannot_prove_trigger_optimization():
    forced = ev.evaluate_trigger_routing(
        skill_name="deploy-rollout",
        candidate_snapshot=dict(CAND_SNAPSHOT),
        baseline_corpus=_baseline_corpus(),
        forced_scores={"hit_at_3": 1.0, "mean_rank": 1.0},  # 有人注入的成绩
    )
    assert forced["evaluated"] is False
    assert forced["trigger_improved"] is False
    assert forced["routing_pass"] is False
    assert forced["forced_injection_rejected"] is True
    assert forced["reason"] == "forced_injection_cannot_prove_trigger_optimization"

    # 有真实链路数据时，注入成绩同样只作 rejected 标记，不参与判定
    mixed = ev.evaluate_trigger_routing(
        skill_name="deploy-rollout",
        candidate_snapshot={
            **CAND_SNAPSHOT,
            "when_to_use": "deploy release canary rollout database failover replica",
        },
        baseline_corpus=_baseline_corpus(),
        positive_queries=["plan a canary deploy"],
        negative_queries=["database replica failover now"],
        forced_scores={"hit_at_3": 1.0},
    )
    assert mixed["evaluated"] is True
    assert mixed["forced_injection_rejected"] is True
    assert mixed["forced_scores_ignored"] == {"hit_at_3": 1.0}
    assert mixed["routing_pass"] is False  # 结论只来自实际链路（负例挂）


# ── 场景 5：同一执行适配器公平对照；关闭新适配返回旧报告兼容字段 ─────────────


def test_s5_legacy_and_search_candidates_share_same_adapter():
    freeze, _, cases = _frozen_and_candidate()
    legacy = {"variant_id": "v-legacy", "snapshot": dict(CAND_SNAPSHOT), "mutation_type": "heuristic"}
    search = {"candidate_id": "cand-pool", "bundle": dict(CAND_SNAPSHOT), "strategy": "pool_search"}
    report = asyncio.run(
        ev.compare_candidates_fair(candidates=[legacy, search], cases=cases, executor=ev.TextOnlySideQueryExecutor(side_query=_make_side_query("reply")), freeze=freeze)
    )
    assert report["ok"] is True
    assert report["same_executor"] is True
    assert report["adapter_id"] == "text_only_side_query"
    assert len(report["comparisons"]) == 2
    first, second = report["comparisons"]
    assert first["candidate_id"] == "v-legacy" and first["strategy"] == "heuristic"
    assert second["candidate_id"] == "cand-pool" and second["strategy"] == "pool_search"
    # 同一适配器、同一协议指纹、同一执行级别口径
    assert first["adapter_id"] == second["adapter_id"] == "text_only_side_query"
    assert first["execution_levels"] == second["execution_levels"] == {"text_only": 3}
    assert report["protocol_fingerprint"] == freeze.fingerprint
    assert first["average_score"] == second["average_score"] == pytest.approx(1.0)


def test_s5_disabled_adapter_returns_legacy_compatible_report():
    legacy_report = {
        "candidate_variants": [{"variant_id": "v1"}],
        "variant_summaries": [{"variant_id": "v1", "average_score": 0.9}],
        "best_variant": {"variant_id": "v1"},
        "best_dev_summary": {"average_score": 0.9},
        "best_test_summary": {"average_score": 0.8},
        "outputs": [{"sample_id": "s1"}],
        "judgments": [{"rule_id": "r1"}],
        "trigger": {"evaluated": True, "improved": True},
        "triggerError": "",
    }
    report = ev.build_eval_report(legacy_report=legacy_report, new_adapter_enabled=False)
    for key, value in legacy_report.items():
        assert report[key] == value  # 旧报告消费者需要的字段逐字段保留
    assert report["search_extension"] == {
        "enabled": False,
        "schema_version": 1,
        "reason": "new execution adapter disabled; legacy report fields only",
    }

    enabled = ev.build_eval_report(
        legacy_report=legacy_report,
        unified={
            "case_matrix": [{"case_id": "case-a", "score": 1.0}],
            "completeness": {"ok": True},
            "execution_levels": {"text_only": 1},
            "fair_comparison": {"same_executor": True},
            "trigger_routing": {"routing_pass": True},
            "combo_gate": {"combo_allowed": True},
        },
        new_adapter_enabled=True,
    )
    for key, value in legacy_report.items():
        assert enabled[key] == value  # 开启后旧字段仍在，不破坏现有消费者
    assert enabled["search_extension"]["enabled"] is True
    assert enabled["search_extension"]["schema_version"] == 2
    assert enabled["search_extension"]["case_matrix"] == [{"case_id": "case-a", "score": 1.0}]


# ── 场景 6：出生题只作开发证据；used≠任务成功；组合冲突必须重新评测 ──────────


def test_s6_birth_question_success_is_dev_evidence_only():
    candidate = ev.normalize_candidate({"candidate_id": "cand-birth", "snapshot": dict(CAND_SNAPSHOT)})
    executor = ev.TextOnlySideQueryExecutor(side_query=_make_side_query("perfect"))
    birth_case = _case("birth-1", origin="evidence", split="evidence", verifier=_pass_verifier)
    score_case = _case("case-b", verifier=_pass_verifier)
    freeze = _freeze_all_s([birth_case, score_case])
    outcomes = asyncio.run(
        ev.evaluate_cases(executor=executor, cases=[birth_case, score_case], candidate=candidate, freeze=freeze)
    )
    birth_outcome = outcomes[0]
    assert birth_outcome.terminal_status == ev.TERMINAL_PASS
    assert birth_outcome.dev_evidence_only is True
    final = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=candidate)
    # 出生题重答成功不进分数向量（不冒充在线健康成绩），只留在 dev_evidence
    assert final["ok"] is True
    assert "birth-1" not in final["score_vector"]
    assert final["score_vector"] == {"case-b": 1.0}
    assert final["dev_evidence"] and final["dev_evidence"][0]["case_id"] == "birth-1"
    assert "只作候选开发证据" in final["dev_evidence"][0]["note"]
    evidence_only = ev.collect_dev_evidence(outcomes)
    assert evidence_only["dev_evidence_only"] is True
    assert evidence_only["birth_replays"][0]["passed"] is True


def test_s6_evidence_only_pool_cannot_produce_score_vector():
    cases = [_case("birth-1", origin="evidence", split="evidence", verifier=_pass_verifier)]
    freeze = _freeze_all_s(cases)
    candidate = ev.normalize_candidate({"candidate_id": "cand-birth", "snapshot": dict(CAND_SNAPSHOT)})
    outcomes = asyncio.run(
        ev.evaluate_cases(
            executor=ev.TextOnlySideQueryExecutor(side_query=_make_side_query("perfect")),
            cases=cases,
            candidate=candidate,
            freeze=freeze,
        )
    )
    final = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate=candidate)
    assert final["ok"] is False and final["reason"] == "score_vector_empty"
    assert final["score_vector"] is None


def test_s6_used_annotation_is_not_task_success():
    candidate = ev.normalize_candidate({"candidate_id": "cand-used", "snapshot": dict(CAND_SNAPSHOT)})
    text_outcome = asyncio.run(
        ev.TextOnlySideQueryExecutor(side_query=_make_side_query("perfect")).execute(
            case=_case("case-a", used=True, verifier=_pass_verifier), candidate=candidate
        )
    )
    assert text_outcome.usage_used is True
    record = ev.usage_signal_record(retrieved=True, relevant=True, used=True, outcome=text_outcome)
    # used=True 只是召回/使用信号；text_only 通过不构成任务成功
    assert record["used"] is True and record["task_success"] is False

    verified_outcome = ev.ExecutionOutcome(
        case_id="case-a",
        candidate_id="cand-used",
        terminal_status=ev.TERMINAL_PASS,
        execution_level=ev.LEVEL_ARTIFACT_VERIFIED,
        valid=True,
        score=1.0,
        artifact_verified=True,
    )
    assert ev.usage_signal_record(retrieved=True, relevant=True, used=True, outcome=verified_outcome)["task_success"] is True


def test_s6_combo_conflict_requires_re_evaluation():
    from eval.skill_combo_replay import synthetic_conflict_pair, synthetic_duplicate_pair

    # 正文、触发分别通过才允许组合重测
    blocked = ev.combo_retest_gate(body_pass=True, trigger_pass=False)
    assert blocked["combo_allowed"] is False
    assert blocked["reason"] == "body_and_trigger_must_both_pass_before_combo_retest"

    # 分别通过 + 无冲突组合 → 允许组合重测
    clean = ev.combo_retest_gate(
        body_pass=True,
        trigger_pass=True,
        skills=[
            {"name": "python-sort", "description": "排序", "when_to_use": "when sorting python lists", "instructions": "用 sorted()。"},
            {"name": "git-tag", "description": "打 tag", "when_to_use": "when creating git tags", "instructions": "语义化版本。"},
        ],
    )
    assert clean["combo_allowed"] is True and clean["combo_required"] is True
    assert clean["combo_conflict"] is False

    # 多技能分别通过、但组合互斥（json vs table）：组合冲突必须重新评测并阻断
    a, b = synthetic_conflict_pair()
    conflict = ev.combo_retest_gate(body_pass=True, trigger_pass=True, skills=[a, b])
    assert conflict["combo_allowed"] is False
    assert conflict["combo_conflict"] is True
    assert conflict["reason"] == "combo_conflict_blocked"
    assert conflict["replay"]["blockedFromInjection"]


# ── 缓存键覆盖完整协议 + DE-29 复用门串联 ───────────────────────────────────


def test_cache_key_covers_protocol_and_reuse_gate(tmp_path):
    freeze, candidate, _ = _frozen_and_candidate()
    base = ev.evaluation_cache_key(candidate=candidate, freeze=freeze, replicate=0, seed=1)
    assert base == ev.evaluation_cache_key(candidate=candidate, freeze=freeze, replicate=0, seed=1)
    changed = [
        ev.evaluation_cache_key(candidate=candidate, freeze=freeze, models={"eval": "gpt-x"}, seed=1),
        ev.evaluation_cache_key(candidate=candidate, freeze=freeze, tool_env={"sandbox": "v2"}, seed=1),
        ev.evaluation_cache_key(candidate=candidate, freeze=freeze, replicate=1, seed=1),
        ev.evaluation_cache_key(candidate=candidate, freeze=freeze, seed=2),
        ev.evaluation_cache_key(candidate=candidate, freeze=freeze, scope="tenant-a", seed=1),
    ]
    assert len(set(changed)) == 5
    assert all(item != base for item in changed)

    # 与 DE-29 复用门串联：manifest 绑定 bundle/协议后，未绑定/跨 bundle 的旧记录拒绝
    skill_dir = tmp_path / ".evoharness" / "skills" / "sop"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: sop-skill\ndescription: 独立执行 SOP\nversion: 1.0.0\nproducer-kind: executable_sop\n---\n\n# 步骤\n",
        encoding="utf-8",
    )
    manifest = sc.build_search_manifest(skill_dir=skill_dir, tenant="t1", space="s1")["manifest"]
    entry = sc.attach_bundle_identity(
        manifest, {"cache_key": base, "score_vector": {"case-a": 1.0}}
    )
    assert ev.check_cache_reuse(entry, key=base, manifest=manifest)["ok"] is True

    drifted = ev.evaluation_cache_key(candidate=candidate, freeze=freeze, seed=99)
    assert ev.check_cache_reuse(entry, key=drifted, manifest=manifest)["ok"] is False

    # 未绑定 bundle 的旧记录直接被拒（DE-29 复用门 fail-closed）：
    unbound = {"cache_key": base}
    out = ev.check_cache_reuse(unbound, key=base, manifest=manifest)
    assert out["ok"] is False and out["error"] == "evaluation_not_bound_to_bundle"


def test_eval_invalid_when_frozen_verifier_missing():
    candidate = ev.normalize_candidate({"candidate_id": "cand-nv", "snapshot": dict(CAND_SNAPSHOT)})
    outcome = asyncio.run(
        ev.TextOnlySideQueryExecutor(side_query=_make_side_query("text")).execute(
            case=_case("case-a"), candidate=candidate  # 没有 verifier，协议里也没有
        )
    )
    assert outcome.terminal_status == ev.TERMINAL_EVAL_INVALID
    assert outcome.valid is False and outcome.score is None
