"""失败反思沉淀回归（Reflexion 式移植）。

关键纪律（每条都对应一个真实的失败模式）：
- 单次失败不沉淀（噪声写进记忆会污染后续召回）
- 平台错误不算能力问题（infra 单列，不许写"教训"去改策略）
- 教训 subject 可被 L1 冲突消解复用（同 kind+family 只有一个有效教训）
- 记忆文件格式与既有 frontmatter 一致（否则索引与召回都读不到）
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agents.reflection import (
    MIN_OCCURRENCES,
    FailureEvent,
    derive_lessons,
    extract_failures_from_predictions,
    group_failures,
    lesson_subject,
    lesson_to_memory,
    render_lesson,
)


# ── R1 证据门槛 ────────────────────────────────────────────


def test_single_failure_produces_no_lesson():
    events = [FailureEvent("t1", "timeout", "gaia")]
    assert derive_lessons(events) == [], "单次失败不得沉淀（噪声门槛）"


def test_two_same_kind_failures_produce_lesson():
    events = [FailureEvent("t1", "timeout", "gaia"), FailureEvent("t2", "timeout", "gaia")]
    lessons = derive_lessons(events)
    assert len(lessons) == 1
    assert lessons[0].occurrences == 2
    assert lessons[0].kind == "timeout"


def test_threshold_is_configurable():
    events = [FailureEvent("t1", "tool-error", "x")]
    assert derive_lessons(events, min_occurrences=1), "门槛可配到 1（对照实验用）"
    assert MIN_OCCURRENCES == 2, "默认门槛必须保持 2"


def test_different_kinds_not_mixed():
    """超时与答案错误混成一类会把教训写歪。"""
    events = [
        FailureEvent("t1", "timeout", "gaia"),
        FailureEvent("t2", "wrong-answer", "gaia"),
    ]
    assert derive_lessons(events) == [], "不同 kind 不得聚合"


def test_different_families_not_mixed():
    events = [
        FailureEvent("t1", "timeout", "gaia"),
        FailureEvent("t2", "timeout", "hle"),
    ]
    assert derive_lessons(events) == [], "不同任务族不得聚合"


def test_unknown_kind_bucketed_not_dropped():
    events = [FailureEvent("t1", "weird", "x"), FailureEvent("t2", "weird", "x")]
    groups = group_failures(events)
    assert "weird|x" not in groups, "未知 kind 不得原样成组"
    assert "other|x" in groups, "未知 kind 必须被归入 other（family 保留）"
    assert len(derive_lessons(events)) == 1, "未知类型不能被静默丢弃"


# ── R2 结构化教训 ─────────────────────────────────────────


def test_lesson_has_three_sections_and_action():
    events = [FailureEvent("t1", "timeout", "gaia", "step 12 exceeded"),
              FailureEvent("t2", "timeout", "gaia", "step 9 exceeded")]
    lesson = derive_lessons(events)[0]
    body = render_lesson(lesson)
    for section in ("## 症状", "## 根因", "## 下次动作"):
        assert section in body
    assert "换路径" in body, "超时类必须给出可执行的下次动作"
    assert "（待确认）" in body, "根因不得由程序臆断——必须标明待确认"


def test_detail_samples_deduped_and_truncated():
    long_detail = "x" * 500
    events = [FailureEvent("t1", "tool-error", "x", long_detail),
              FailureEvent("t2", "tool-error", "x", long_detail)]
    lesson = derive_lessons(events)[0]
    assert lesson.symptoms.count("x" * 500) <= 1, "同样例不得重复"
    assert len(lesson.symptoms) < 600, "教训不能把整段日志写进记忆"


# ── R3 与记忆体系一致 ─────────────────────────────────────


def test_lesson_subject_normalized_for_l1_dedup():
    assert lesson_subject("timeout", "gaia") == "failure-timeout-gaia"
    assert lesson_subject("tool-error", "") == "failure-tool-error-general"
    # 同 kind+family 必得同 subject（L1 才会覆盖旧教训而不是越积越多）
    assert lesson_subject("timeout", "gaia") == lesson_subject("timeout", "gaia")


def test_lesson_to_memory_matches_frontmatter_contract():
    events = [FailureEvent("t1", "timeout", "gaia"), FailureEvent("t2", "timeout", "gaia")]
    payload = lesson_to_memory(derive_lessons(events)[0])
    content = payload["content"]
    assert content.startswith("---\n")
    assert "\ntype: feedback\n" in content
    assert f"subject: {payload['subject']}" in content
    assert payload["filename"].startswith("feedback_")
    assert payload["filename"].endswith(".md")
    # 能被既有 frontmatter 解析器读懂
    from agents.memory import parse_frontmatter
    meta = parse_frontmatter(content).meta
    assert meta.get("type") == "feedback"
    assert meta.get("subject") == payload["subject"]


# ── 从真实评测记录提取失败 ────────────────────────────────


def test_extract_classifies_failure_kinds():
    records = [
        {"task_id": "a", "timed_out": True, "family": "gaia"},
        {"task_id": "b", "error": "aggregate: no available candidates", "family": "gaia"},
        {"task_id": "c", "has_final_marker": False, "family": "gaia"},
        {"task_id": "d", "has_final_marker": True, "prediction": "x", "family": "gaia"},
    ]
    events = extract_failures_from_predictions(records, classify=lambda r, d: "incorrect")
    kinds = {e.task_id: e.kind for e in events}
    assert kinds["a"] == "timeout"
    assert kinds["b"] == "infra", "网关错误必须归 infra，不能算能力问题"
    assert kinds["c"] == "no-final-marker"
    assert kinds["d"] == "wrong-answer"


def test_infra_failures_do_not_become_strategy_lessons():
    """平台错误累计也不该产出"改策略"的教训——它的 next_action 明确说不是能力问题。"""
    events = [FailureEvent("t1", "infra", "gaia"), FailureEvent("t2", "infra", "gaia")]
    lesson = derive_lessons(events)[0]
    body = render_lesson(lesson)
    assert "不是能力问题" in body
    assert "跳过" in body


def test_correct_records_produce_no_failure():
    records = [{"task_id": "a", "has_final_marker": True, "prediction": "p"}]
    events = extract_failures_from_predictions(records, classify=lambda r, d: "correct")
    assert events == []
