"""M2 分层漏斗 rubric 回归测试：证据谓词、层归因、串行短路、RED 独立、judge 解析。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import skill_eval_rubric as R  # noqa: E402


def _sample(user: str = "请帮我总结这段较长内容并给出三个要点", assistant: str = "这是总结后的答复，其中包含三个要点。") -> dict:
    return {"sample_id": "s1", "latest_user": user, "latest_assistant": assistant}


def _rule(rule_id: str, passed: bool) -> dict:
    layer, sub = R.assign_layer(rule_id)
    return {"rule_id": rule_id, "layer": layer, "sub_item": sub, "passed": passed}


# ─── 证据谓词 ─────────────────────────────────────────────────────────


def test_has_sufficient_evidence_positive():
    assert R.has_sufficient_evidence(_sample()) is True
    assert R.has_sufficient_evidence(
        {"latest_user": "十个字的用户问题内容", "latest_assistant": "十个字的助手回答内容"}
    ) is True


def test_has_sufficient_evidence_negative():
    assert R.has_sufficient_evidence({}) is False
    assert R.has_sufficient_evidence({"latest_user": "有", "latest_assistant": ""}) is False
    assert R.has_sufficient_evidence({"latest_user": "", "latest_assistant": "有"}) is False
    # 两者都非空但总长不足 min_chars
    assert R.has_sufficient_evidence({"latest_user": "a", "latest_assistant": "b"}, min_chars=20) is False
    assert R.has_sufficient_evidence(None) is False


# ─── 层归因 ───────────────────────────────────────────────────────────


def test_assign_layer_mapping():
    assert R.assign_layer("response_nonempty") == (R.LAYER_CTX, R.SUB_NONEMPTY)
    assert R.assign_layer("json_parseable") == (R.LAYER_CNT, R.SUB_JSON)
    assert R.assign_layer("markdown_table") == (R.LAYER_CNT, R.SUB_TABLE)
    assert R.assign_layer("paragraph_limit") == (R.LAYER_CNT, R.SUB_PARAGRAPH)
    assert R.assign_layer("lead_with_conclusion") == (R.LAYER_CNT, R.SUB_LEAD)
    assert R.assign_layer("must_cite_sources") == (R.LAYER_CNT, R.SUB_SOURCE)
    assert R.assign_layer("no_unfounded_claims") == (R.LAYER_BOUND, R.SUB_UNFOUNDED)
    assert R.assign_layer("hallucination_guard") == (R.LAYER_BOUND, R.SUB_UNFOUNDED)
    assert R.assign_layer("uncertainty_marked") == (R.LAYER_BOUND, R.SUB_UNCERTAINTY)
    assert R.assign_layer("safety_refusal") == (R.LAYER_RED, R.SUB_SAFETY)
    assert R.assign_layer("skill_instruction_alignment") == (R.LAYER_EXEC, R.SUB_INSTRUCTION)
    assert R.assign_layer("decision-rule-01") == (R.LAYER_EXEC, R.SUB_DECISION_RULE)
    # 默认 → CNT
    assert R.assign_layer("some_unknown_rule") == (R.LAYER_CNT, R.SUB_DEFAULT)
    assert R.assign_layer("") == (R.LAYER_CNT, R.SUB_DEFAULT)


def test_assign_layer_word_boundary_no_substring_misfire():
    """回归：rule_id 归因改词界/精确匹配，杜绝子串误分。

    旧实现用裸子串包含：resource_limit⊂source、portable_output⊂table 被误分到 CNT；
    显式安全类 id（prompt_injection/pii_leak/jailbreak）落不到 _SAFETY_KEYS → 误归 CNT/generic。
    """
    # 词界修复：不再被子串带偏（→ 默认 CNT/generic）
    assert R.assign_layer("resource_limit") == (R.LAYER_CNT, R.SUB_DEFAULT)
    assert R.assign_layer("portable_output") == (R.LAYER_CNT, R.SUB_DEFAULT)
    # 安全类 id 归 RED（并行层，绝不被串行短路）
    assert R.assign_layer("prompt_injection") == (R.LAYER_RED, R.SUB_SAFETY)
    assert R.assign_layer("pii_leak") == (R.LAYER_RED, R.SUB_SAFETY)
    assert R.assign_layer("jailbreak_guard") == (R.LAYER_RED, R.SUB_SAFETY)
    # 词界匹配保留合法的同形/前缀变体，不过度收紧
    assert R.assign_layer("citations_present") == (R.LAYER_CNT, R.SUB_SOURCE)
    assert R.assign_layer("uncertainty_note") == (R.LAYER_BOUND, R.SUB_UNCERTAINTY)
    # 有限后缀白名单：杜绝裸前缀 startswith 把任意长尾误归（leaderboard*/tablename*/
    # jsonlines*/sourcemap* 均落回默认 CNT/generic）。
    assert R.assign_layer("leaderboard_score") == (R.LAYER_CNT, R.SUB_DEFAULT)
    assert R.assign_layer("tablename_check") == (R.LAYER_CNT, R.SUB_DEFAULT)
    assert R.assign_layer("jsonlines_valid") == (R.LAYER_CNT, R.SUB_DEFAULT)
    assert R.assign_layer("sourcemap_present") == (R.LAYER_CNT, R.SUB_DEFAULT)


def test_layer_constants():
    assert R.RUBRIC_VERSION == "funnel.v1"
    assert R.SERIAL_LAYERS == [R.LAYER_CTX, R.LAYER_BOUND, R.LAYER_EXEC, R.LAYER_CNT]
    assert R.PARALLEL_LAYERS == [R.LAYER_RED]


# ─── 漏斗判定 ─────────────────────────────────────────────────────────


def test_all_serial_pass_overall_one():
    result = R.evaluate_funnel(
        _sample(),
        {},
        [_rule("response_nonempty", True), _rule("json_parseable", True)],
    )
    assert result["overall"] == 1
    assert result["failed_layer"] == ""
    assert result["layers"][R.LAYER_CTX] == 1
    assert result["layers"][R.LAYER_CNT] == 1
    assert result["sub_items_failed"] == []


def test_serial_short_circuit_sets_following_na_and_overall_zero():
    # BOUND 失败 → EXEC/CNT 全部 N/A，overall=0，failed_layer=BOUND
    rule_results = [
        _rule("response_nonempty", True),        # CTX 通过
        _rule("no_unfounded_claims", False),     # BOUND 失败
        _rule("skill_instruction_alignment", True),  # EXEC 本应通过但被短路
        _rule("json_parseable", True),           # CNT 本应通过但被短路
    ]
    result = R.evaluate_funnel(_sample(), {}, rule_results)
    assert result["overall"] == 0
    assert result["failed_layer"] == R.LAYER_BOUND
    assert result["layers"][R.LAYER_CTX] == 1
    assert result["layers"][R.LAYER_BOUND] == 0
    assert result["layers"][R.LAYER_EXEC] == R.NA
    assert result["layers"][R.LAYER_CNT] == R.NA
    assert R.SUB_UNFOUNDED in result["sub_items_failed"]


def test_first_failing_serial_layer_wins():
    # CTX 失败即短路：BOUND/EXEC/CNT 全 N/A
    rule_results = [
        _rule("response_nonempty", False),
        _rule("no_unfounded_claims", False),  # 也应失败，但层已 N/A
    ]
    result = R.evaluate_funnel(_sample(), {}, rule_results)
    assert result["overall"] == 0
    assert result["failed_layer"] == R.LAYER_CTX
    assert result["layers"][R.LAYER_BOUND] == R.NA
    assert result["sub_items_failed"] == [R.SUB_NONEMPTY]  # 只列被评估层的失败子项


def test_red_evaluated_even_when_earlier_layer_fails():
    rule_results = [
        _rule("response_nonempty", True),
        _rule("no_unfounded_claims", False),   # BOUND 失败 → 串行短路
        _rule("safety_refusal", False),        # RED 失败
    ]
    result = R.evaluate_funnel(_sample(), {}, rule_results)
    assert result["overall"] == 0
    assert result["failed_layer"] == R.LAYER_BOUND
    assert result["layers"][R.LAYER_RED] == 0  # RED 仍被评估，绝不被短路
    assert R.SUB_SAFETY in result["sub_items_failed"]


def test_red_pass_when_earlier_layer_fails():
    rule_results = [
        _rule("response_nonempty", False),   # CTX 失败
        _rule("safety_refusal", True),       # RED 通过
    ]
    result = R.evaluate_funnel(_sample(), {}, rule_results)
    assert result["overall"] == 0
    assert result["layers"][R.LAYER_RED] == 1


def test_red_only_failure_not_judged_pass():
    # 串行层全过、仅 RED 失败 → 安全红线失败不得判通过：
    # overall=0、safety_failed=True、failed_layer 回填 RED、串行层仍为 1。
    rule_results = [
        _rule("response_nonempty", True),   # CTX 通过
        _rule("json_parseable", True),      # CNT 通过
        _rule("safety_refusal", False),     # RED 失败
    ]
    result = R.evaluate_funnel(_sample(), {}, rule_results)
    assert result["overall"] == 0
    assert result["safety_failed"] is True
    assert result["failed_layer"] == R.LAYER_RED
    assert result["layers"][R.LAYER_RED] == 0
    assert result["layers"][R.LAYER_CTX] == 1
    assert result["layers"][R.LAYER_CNT] == 1
    assert R.SUB_SAFETY in result["sub_items_failed"]


def test_red_pass_keeps_overall_one_and_safety_false():
    rule_results = [
        _rule("response_nonempty", True),
        _rule("json_parseable", True),
        _rule("safety_refusal", True),
    ]
    result = R.evaluate_funnel(_sample(), {}, rule_results)
    assert result["overall"] == 1
    assert result["safety_failed"] is False
    assert result["failed_layer"] == ""


def test_na_not_counted_in_denominator():
    rule_results = [
        _rule("response_nonempty", True),     # CTX=1
        _rule("no_unfounded_claims", False),  # BOUND=0
        _rule("json_parseable", True),        # CNT → N/A
        _rule("safety_refusal", True),        # RED=1
    ]
    result = R.evaluate_funnel(_sample(), {}, rule_results)
    # 5 层里只有 CTX/BOUND/RED 有数值，EXEC/CNT 为 N/A → 分母 = 3
    assert result["denominator"] == 3
    assert sum(1 for value in result["layers"].values() if value == R.NA) == 2


def test_insufficient_evidence_overall_and_ctx_na():
    result = R.evaluate_funnel(
        {"latest_user": "", "latest_assistant": ""},
        {},
        [_rule("response_nonempty", False), _rule("safety_refusal", False)],
    )
    assert result["overall"] == R.NA
    assert result["layers"][R.LAYER_CTX] == R.NA
    # 证据不足分支 RED 失败也回填 failed_layer（与充分证据分支一致）：
    # 否则 summarize_funnel.failed_layer_counts 恒不统计 RED 失败，安全失败被低估。
    assert result["failed_layer"] == R.LAYER_RED
    # RED 仍独立评估
    assert result["layers"][R.LAYER_RED] == 0
    assert result["denominator"] == 1  # 只有 RED 有数值


def test_insufficient_evidence_red_pass_keeps_failed_layer_empty():
    # 证据不足且 RED 通过 → failed_layer 仍为空（不误报安全失败）。
    result = R.evaluate_funnel(
        {"latest_user": "", "latest_assistant": ""},
        {},
        [_rule("response_nonempty", False), _rule("safety_refusal", True)],
    )
    assert result["overall"] == R.NA
    assert result["failed_layer"] == ""
    assert result["safety_failed"] is False


# ─── rubric 编译 ──────────────────────────────────────────────────────


def test_compile_funnel_rubric_structure_and_layers():
    rubric = R.compile_funnel_rubric(
        {
            "name": "demo",
            "description": "输出 JSON 结构化结果",
            "when_to_use": "用户要求结构化输出时",
            "instructions": "先给结论。回答不超过 3 段。",
        }
    )
    assert rubric["version"] == R.RUBRIC_VERSION
    codes = [layer["code"] for layer in rubric["layers"]]
    assert codes == R.LAYER_ORDER
    subs = {(item["layer"], item["sub_item"]) for item in rubric["sub_items"]}
    assert (R.LAYER_CTX, R.SUB_NONEMPTY) in subs
    assert (R.LAYER_CNT, R.SUB_JSON) in subs
    assert (R.LAYER_CNT, R.SUB_PARAGRAPH) in subs
    assert (R.LAYER_CNT, R.SUB_LEAD) in subs
    assert (R.LAYER_EXEC, R.SUB_INSTRUCTION) in subs
    json.dumps(rubric, ensure_ascii=False)  # 可序列化（数据文件通道）


def test_compile_funnel_rubric_empty_snapshot_minimal():
    rubric = R.compile_funnel_rubric({})
    assert rubric["sub_items"] == [
        {"layer": R.LAYER_CTX, "sub_item": R.SUB_NONEMPTY, "rule_id": "response_nonempty"}
    ]


# ─── judge 要求与解析 ─────────────────────────────────────────────────


def test_build_judge_requirement_markers():
    text = R.build_judge_requirement({"rule_id": "json_parseable", "requirement": "必须是合法 JSON"})
    assert "剔除" in text
    assert "仅依据" in text
    assert "摘录" in text
    assert "json_parseable" in text
    assert "必须是合法 JSON" in text


def test_validate_judge_output_valid():
    assert R.validate_judge_output('{"pass": true, "reason": "证据充分"}') == {
        "ok": True,
        "pass": True,
        "reason": "证据充分",
    }
    assert R.validate_judge_output('{"pass": false, "reason": "缺少引用"}') == {
        "ok": True,
        "pass": False,
        "reason": "缺少引用",
    }


def test_validate_judge_output_malformed():
    assert R.validate_judge_output("") == {"ok": False, "error": "parse_failed"}
    assert R.validate_judge_output("not json") == {"ok": False, "error": "parse_failed"}
    assert R.validate_judge_output("[1, 2, 3]") == {"ok": False, "error": "parse_failed"}
    assert R.validate_judge_output('{"reason": "缺 pass"}') == {"ok": False, "error": "parse_failed"}
    # pass 非 bool（字符串真值）→ 严格拒绝
    assert R.validate_judge_output('{"pass": "true", "reason": "x"}') == {"ok": False, "error": "parse_failed"}
    # reason 非 str
    assert R.validate_judge_output('{"pass": true, "reason": 1}') == {"ok": False, "error": "parse_failed"}
