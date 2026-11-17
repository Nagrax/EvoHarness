"""DE-33 候选池回归：硬门、父子逐 Case 入池判定与 Attempt 落账（S2 场景）。

对应 tasks.json DE-33 acceptance_scenarios 第 2 条与
SKILL-EVOLUTION-SEARCH §10.2，输入直接取
``docs/digital-employee/search-design-fixtures.json`` 的 admission_examples：

1. parent_dominates → rejected_dominated；child_dominates → admit；
   complementary_lower_mean → admit（互不支配，池保留父与子）；
2. equal_scores / epsilon_tie → no_effect（同分不改进，不入池）；
3. safety_before_mean → 安全硬门先于分数比较（支配父代也拒绝）；
   missing_case → invalid_evaluation（fail-closed）；
4. NaN/Inf/越界/多题/重复向量 → rejected_invalid_evaluation，不填零不部分入池；
5. 相同制品相同协议 → rejected_duplicate（复用已验证评测）；
6. 直接父代必须在池中且可用：未知父代/不可用父代拒绝；
7. mark_unusable 保留记录并使下游不可用；Attempt 逐次落账（未入池尝试也保留）；
8. 与 DE-30 finalize_score_vector 串联：完整向量入池，缺题向量按门拒绝；
9. 池判定与输入 dict 键序无关（Case 完成顺序不改变结果），save/load 往返一致。

全部离线（fixtures + 合成评分向量），不依赖真实模型。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.skill_search import evaluator as ev
from agents.skill_search import pool as pl
from agents.skill_search import protocol as sp

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads(
    (REPO_ROOT / "docs" / "digital-employee" / "search-design-fixtures.json").read_text(encoding="utf-8")
)
assert FIXTURES["epsilon"] == pl.SCORE_EPSILON


def _make_pool(case_ids: list[str], *, search_id: str = "s-de33-pool") -> pl.CandidatePool:
    return pl.CandidatePool(search_id=search_id, case_ids=case_ids, epsilon=FIXTURES["epsilon"])


def _seed(pool: pl.CandidatePool, scores: list[float], *, cid: str = "seed", content: str = "hash-seed") -> dict:
    vector = {case_id: score for case_id, score in zip(pool.case_ids, scores)}
    result = pool.register_seed(
        candidate_id=cid,
        score_vector=vector,
        content_hash=content,
        protocol_hash="proto-1",
    )
    assert result["ok"] is True, result
    return result


# ── fixtures admission_examples 逐例驱动 ─────────────────────────────────────


def test_s2_fixture_parent_dominates_child_rejected():
    spec = next(x for x in FIXTURES["admission_examples"] if x["id"] == "parent_dominates")
    pool = _make_pool(["case-1", "case-2"])
    _seed(pool, spec["parent"])
    result = pool.admit(
        candidate_id="child",
        parent_id="seed",
        score_vector={"case-2": spec["child"][1], "case-1": spec["child"][0]},  # 键序故意与 Case 序不同
        content_hash="hash-child",
        protocol_hash="proto-1",
    )
    assert result["ok"] is False
    assert result["decision"] == pl.DECISION_REJECTED_DOMINATED
    assert result["verdict"] == pl.VERDICT_PARENT_DOMINATES
    assert len(pool.candidates) == 1  # 只追加种子，拒绝不入池
    # Attempt 保留（未入池尝试不丢弃）
    assert [a["decision"] for a in pool.attempts] == [
        pl.DECISION_ADMITTED,
        pl.DECISION_REJECTED_DOMINATED,
    ]


def test_s2_fixture_child_dominates_parent_admitted():
    spec = next(x for x in FIXTURES["admission_examples"] if x["id"] == "child_dominates")
    pool = _make_pool(["case-1", "case-2"])
    _seed(pool, spec["parent"])
    result = pool.admit(
        candidate_id="child",
        parent_id="seed",
        score_vector=dict(zip(["case-1", "case-2"], spec["child"])),
        content_hash="hash-child",
        protocol_hash="proto-1",
    )
    assert result["ok"] is True
    assert result["decision"] == pl.DECISION_ADMITTED
    assert result["verdict"] == pl.VERDICT_CHILD_DOMINATES
    assert [c["candidate_id"] for c in pool.candidates] == ["seed", "child"]
    assert pool.candidates[1]["created_sequence"] == 1
    assert pool.candidates[1]["iteration"] == 1


def test_s2_fixture_complementary_lower_mean_admitted_and_both_retained():
    spec = next(x for x in FIXTURES["admission_examples"] if x["id"] == "complementary_lower_mean")
    pool = _make_pool(FIXTURES["frontier_example"]["case_ids"])
    _seed(pool, spec["parent"], cid="parent-c")
    result = pool.admit(
        candidate_id="child-d",
        parent_id="parent-c",
        score_vector=dict(zip(pool.case_ids, spec["child"])),
        content_hash="hash-child-d",
        protocol_hash="proto-1",
    )
    assert result["ok"] is True
    assert result["verdict"] == pl.VERDICT_INCOMPARABLE
    # 池保留父代与互补低均分子代（父不被删除）
    assert [c["candidate_id"] for c in pool.candidates] == ["parent-c", "child-d"]
    child = pool.get_candidate("child-d")
    assert child["average_score"] < pool.get_candidate("parent-c")["average_score"]


def test_s2_fixture_equal_scores_and_epsilon_tie_are_no_effect():
    for example_id in ("equal_scores", "epsilon_tie"):
        spec = next(x for x in FIXTURES["admission_examples"] if x["id"] == example_id)
        pool = _make_pool(["case-1", "case-2"])
        _seed(pool, spec["parent"], content=f"hash-{example_id}")
        result = pool.admit(
            candidate_id=f"child-{example_id}",
            parent_id="seed",
            # 键序打乱（case-2 在前），值仍按 fixture 对应 case-1/case-2
            score_vector={"case-2": spec["child"][1], "case-1": spec["child"][0]},
            content_hash=f"hash-child-{example_id}",  # 不同制品：走分数比较而非去重
            protocol_hash="proto-1",
        )
        assert result["ok"] is False, example_id
        assert result["decision"] == pl.DECISION_NO_EFFECT, example_id
        assert result["verdict"] == pl.VERDICT_EQUAL_WITHIN_EPSILON, example_id
        assert len(pool.candidates) == 1, example_id


def test_s2_fixture_safety_gate_precedes_mean_and_dominance():
    spec = next(x for x in FIXTURES["admission_examples"] if x["id"] == "safety_before_mean")
    pool = _make_pool(["case-1", "case-2"])
    _seed(pool, spec["parent"])
    result = pool.admit(
        candidate_id="child",
        parent_id="seed",
        score_vector=dict(zip(["case-1", "case-2"], spec["child"])),
        content_hash="hash-child",
        protocol_hash="proto-1",
        safety_passed=bool(spec.get("safety_passed", True)),
    )
    assert result["ok"] is False
    assert result["decision"] == pl.DECISION_REJECTED_SAFETY
    assert len(pool.candidates) == 1
    assert pool.attempts[-1]["decision"] == pl.DECISION_REJECTED_SAFETY


def test_s2_fixture_missing_case_is_invalid_evaluation():
    spec = next(x for x in FIXTURES["admission_examples"] if x["id"] == "missing_case")
    pool = _make_pool(["case-1", "case-2"])
    _seed(pool, spec["parent"])
    result = pool.admit(
        candidate_id="child",
        parent_id="seed",
        score_vector={"case-1": spec["child"][0]},  # 缺 case-2
        content_hash="hash-child",
        protocol_hash="proto-1",
    )
    assert result["ok"] is False
    assert result["decision"] == pl.DECISION_REJECTED_INVALID
    assert any(e["code"] == "missing_case" for e in result["evidence"])
    assert len(pool.candidates) == 1


# ── 评分向量 fail-closed：NaN/Inf/越界/多题 ──────────────────────────────────


@pytest.mark.parametrize(
    "bad_vector,code",
    [
        ({"case-1": float("nan"), "case-2": 0.9}, "score_not_finite"),
        ({"case-1": float("inf"), "case-2": 0.9}, "score_not_finite"),
        ({"case-1": 1.5, "case-2": 0.9}, "score_out_of_range"),
        ({"case-1": -0.1, "case-2": 0.9}, "score_out_of_range"),
        ({"case-1": None, "case-2": 0.9}, "score_not_numeric"),
        ({"case-1": 0.9, "case-2": 0.9, "case-3": 0.9}, "unknown_case"),
    ],
)
def test_s2_invalid_score_vectors_never_enter_pool(bad_vector, code):
    pool = _make_pool(["case-1", "case-2"])
    _seed(pool, [0.8, 0.9])
    result = pool.admit(
        candidate_id="child",
        parent_id="seed",
        score_vector=bad_vector,
        content_hash="hash-child",
        protocol_hash="proto-1",
    )
    assert result["ok"] is False
    assert result["decision"] == pl.DECISION_REJECTED_INVALID
    assert any(e["code"] == code for e in result["evidence"])
    assert len(pool.candidates) == 1
    # 池内种子向量未被污染
    assert pool.get_candidate("seed")["score_vector"] == {"case-1": 0.8, "case-2": 0.9}


def test_s2_duplicate_content_reuses_existing_evaluation():
    pool = _make_pool(["case-1", "case-2"])
    _seed(pool, [0.8, 0.9])
    first = pool.admit(
        candidate_id="child-a",
        parent_id="seed",
        score_vector={"case-1": 0.9, "case-2": 0.9},
        content_hash="hash-same-artifact",
        protocol_hash="proto-1",
    )
    assert first["ok"] is True
    duplicate = pool.admit(
        candidate_id="child-b",
        parent_id="seed",
        score_vector={"case-1": 0.9, "case-2": 0.9},
        content_hash="hash-same-artifact",  # 相同制品
        protocol_hash="proto-1",            # 相同协议
    )
    assert duplicate["ok"] is False
    assert duplicate["decision"] == pl.DECISION_REJECTED_DUPLICATE
    assert duplicate["evidence"][0]["existing_candidate_id"] == "child-a"
    assert [c["candidate_id"] for c in pool.candidates] == ["seed", "child-a"]
    # 不同协议 → 不算重复（新协议须重新评测）
    other_protocol = pool.admit(
        candidate_id="child-c",
        parent_id="seed",
        score_vector={"case-1": 0.9, "case-2": 0.9},
        content_hash="hash-same-artifact",
        protocol_hash="proto-2",
    )
    assert other_protocol["decision"] == pl.DECISION_ADMITTED


def test_s2_direct_parent_must_be_known_and_usable():
    pool = _make_pool(["case-1", "case-2"])
    _seed(pool, [0.8, 0.9])
    unknown = pool.admit(
        candidate_id="child",
        parent_id="ghost",
        score_vector={"case-1": 0.9, "case-2": 0.9},
        content_hash="hash-child",
        protocol_hash="proto-1",
    )
    assert unknown["decision"] == pl.DECISION_REJECTED_PARENT_UNKNOWN

    pool.mark_unusable("seed", reason="bundle invalidated", evidence=[{"code": "protocol_drift"}])
    orphan = pool.admit(
        candidate_id="child-2",
        parent_id="seed",
        score_vector={"case-1": 0.95, "case-2": 0.95},
        content_hash="hash-child-2",
        protocol_hash="proto-1",
    )
    assert orphan["decision"] == pl.DECISION_REJECTED_PARENT_UNUSABLE
    assert len(pool.candidates) == 1


def test_s2_mark_unusable_keeps_record_and_removes_from_usable_set():
    pool = _make_pool(["case-1", "case-2"])
    _seed(pool, [0.8, 0.9])
    pool.admit(
        candidate_id="child",
        parent_id="seed",
        score_vector={"case-1": 0.9, "case-2": 0.9},
        content_hash="hash-child",
        protocol_hash="proto-1",
    )
    marked = pool.mark_unusable("child", reason="artifact invalid", evidence=[{"code": "bad_bundle"}])
    assert marked["ok"] is True and marked["already_unusable"] is False
    # 记录保留（只追加语义），仅可用性标记变化
    assert len(pool.candidates) == 2
    record = pool.get_candidate("child")
    assert record["admitted"] is True
    assert record["unusable"] is True
    assert record["unusable_reason"] == "artifact invalid"
    assert [c["candidate_id"] for c in pool.usable_candidates()] == ["seed"]
    again = pool.mark_unusable("child", reason="second call")
    assert again["already_unusable"] is True
    missing = pool.mark_unusable("ghost", reason="x")
    assert missing["ok"] is False


def test_s2_failed_attempts_retained_for_infra_and_budget():
    pool = _make_pool(["case-1", "case-2"])
    _seed(pool, [0.8, 0.9])
    pool.record_failed_attempt(
        candidate_id="cand-infra",
        parent_id="seed",
        reason="provider 429 rate limit",
        evidence=[{"marker": "429"}],
        completed_cases=1,
    )
    pool.record_failed_attempt(
        candidate_id="cand-budget",
        parent_id="seed",
        reason="budget exhausted",
        status=pl.ATTEMPT_BLOCKED,
    )
    assert len(pool.attempts) == 3
    assert pool.attempts[1]["status"] == pl.ATTEMPT_FAILED
    assert pool.attempts[2]["status"] == pl.ATTEMPT_BLOCKED
    assert pool.attempts[1]["attempt_id"] == "attempt-0002"
    assert len(pool.candidates) == 1  # 未入池尝试不产生池条目


# ── 与 DE-30 完整性门串联 ────────────────────────────────────────────────────


def _freeze_two_cases() -> sp.CaseFreeze:
    cases = [
        {"case_id": "case-1", "origin_group": "score", "family": "f", "question": "q1"},
        {"case_id": "case-2", "origin_group": "score", "family": "f", "question": "q2"},
    ]
    assigned = sp.assign_protocol_roles([{**c, "protocol_role": "S"} for c in cases])
    assert assigned["ok"] is True
    frozen = sp.freeze_case_protocol(assigned["cases"])
    assert frozen["ok"] is True
    return frozen["freeze"]


def _outcome(case_id: str, candidate_id: str, score) -> ev.ExecutionOutcome:
    return ev.ExecutionOutcome(
        case_id=case_id,
        candidate_id=candidate_id,
        terminal_status=ev.TERMINAL_PASS,
        execution_level=ev.LEVEL_TEXT_ONLY,
        valid=True,
        score=score,
    )


def test_s2_finalize_score_vector_wiring_admit_and_reject():
    freeze = _freeze_two_cases()
    pool = pl.CandidatePool(search_id="s-wiring", case_ids=freeze.ordered_case_ids)
    seed_vector = {"case-1": 0.8, "case-2": 0.9}
    seed_finalize = ev.finalize_score_vector(
        freeze=freeze,
        outcomes=[
            _outcome("case-1", "seed", 0.8),
            _outcome("case-2", "seed", 0.9),
        ],
        candidate={"candidate_id": "seed"},
    )
    assert seed_finalize["ok"] is True
    seeded = pool.register_seed(
        candidate_id="seed",
        score_vector=seed_vector,
        content_hash="hash-seed",
        protocol_hash=freeze.fingerprint,
        finalize_result=seed_finalize,
    )
    assert seeded["ok"] is True

    # 子候选完整向量 → 入池
    child_outcomes = [_outcome("case-1", "child", 0.9), _outcome("case-2", "child", 0.9)]
    child_finalize = ev.finalize_score_vector(
        freeze=freeze, outcomes=child_outcomes, candidate={"candidate_id": "child"}
    )
    admitted = pool.admit(
        candidate_id="child",
        parent_id="seed",
        content_hash="hash-child",
        protocol_hash=freeze.fingerprint,
        finalize_result=child_finalize,
    )
    assert admitted["ok"] is True and admitted["decision"] == pl.DECISION_ADMITTED

    # 缺题向量（finalize ok=False）→ 按门拒绝，不入池
    broken_finalize = ev.finalize_score_vector(
        freeze=freeze,
        outcomes=[_outcome("case-1", "broken", 1.0)],  # 缺 case-2
        candidate={"candidate_id": "broken"},
    )
    assert broken_finalize["ok"] is False
    rejected = pool.admit(
        candidate_id="broken",
        parent_id="child",
        content_hash="hash-broken",
        protocol_hash=freeze.fingerprint,
        finalize_result=broken_finalize,
    )
    assert rejected["decision"] == pl.DECISION_REJECTED_INVALID
    assert len(pool.candidates) == 2

    # 安全隔离（security_violation）→ 安全拒绝
    quarantine_finalize = ev.finalize_score_vector(
        freeze=freeze,
        outcomes=[
            _outcome("case-1", "rogue", 1.0),
            _outcome("case-2", "rogue", 1.0),
        ],
        candidate={"candidate_id": "rogue"},
    )
    quarantine_finalize["ok"] = False
    quarantine_finalize["candidate_quarantined"] = True
    quarantine_finalize["reason"] = "security_violation"
    safety = pool.admit(
        candidate_id="rogue",
        parent_id="child",
        content_hash="hash-rogue",
        protocol_hash=freeze.fingerprint,
        finalize_result=quarantine_finalize,
    )
    assert safety["decision"] == pl.DECISION_REJECTED_SAFETY


# ── 确定性与序列化 ───────────────────────────────────────────────────────────


def test_s2_admission_decisions_independent_of_vector_key_order():
    pool_a = _make_pool(["case-1", "case-2"], search_id="s-order-a")
    pool_b = _make_pool(["case-1", "case-2"], search_id="s-order-b")
    seed_forward = {"case-1": 0.8, "case-2": 0.9}
    seed_reversed = {"case-2": 0.9, "case-1": 0.8}
    assert pool_a.register_seed(
        candidate_id="seed", score_vector=seed_forward, content_hash="h", protocol_hash="p"
    )["ok"] is True
    assert pool_b.register_seed(
        candidate_id="seed", score_vector=seed_reversed, content_hash="h", protocol_hash="p"
    )["ok"] is True
    child_scores = [0.7, 1.0]
    ra = pool_a.admit(
        candidate_id="child", parent_id="seed",
        score_vector={"case-1": child_scores[0], "case-2": child_scores[1]},
        content_hash="hc", protocol_hash="p",
    )
    rb = pool_b.admit(
        candidate_id="child", parent_id="seed",
        score_vector={"case-2": child_scores[1], "case-1": child_scores[0]},
        content_hash="hc", protocol_hash="p",
    )
    assert ra["decision"] == rb["decision"]
    a, b = pool_a.to_dict(), pool_b.to_dict()
    for payload in (a, b):
        payload.pop("search_id")
        payload.pop("content_digest")  # digest 绑定 search_id，比较池内容时排除
    assert a == b  # 池状态与向量键序完全无关


def test_s2_pool_roundtrip_and_digest():
    pool = _make_pool(["case-1", "case-2"])
    _seed(pool, [0.8, 0.9])
    pool.admit(
        candidate_id="child", parent_id="seed",
        score_vector={"case-1": 0.9, "case-2": 0.9},
        content_hash="hash-child", protocol_hash="proto-1",
        cumulative_seed_diff={"added_bytes": 120, "ops": 2, "generations": 1},
    )
    pool.record_failed_attempt(candidate_id="cand-x", parent_id="seed", reason="infra")
    payload = pool.to_dict()
    restored = pl.CandidatePool.from_dict(copy.deepcopy(payload))
    assert restored.to_dict() == payload
    assert restored.content_digest() == pool.content_digest()
    # 追加语义在恢复后的池上继续成立
    result = restored.admit(
        candidate_id="child-2", parent_id="child",
        score_vector={"case-1": 0.95, "case-2": 0.85},
        content_hash="hash-child-2", protocol_hash="proto-1",
    )
    assert result["decision"] == pl.DECISION_ADMITTED
    assert restored.get_candidate("child-2")["created_sequence"] == 2
