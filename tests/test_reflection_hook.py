"""反思钩子接线回归：从真实 checkpoint 的失败计数到教训文件落盘。

真实字段契约（踩过一次坑）：checkpoint 里是 `stream_failures` / `holdout_failures`，
形如 {"timeout": 0, "error": 3, "infra": 0}——不是 `*_failure_kinds`。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agents.reflection import FailureEvent, derive_lessons, lesson_to_memory

# 与 evolution_experiment 里钩子完全一致的映射
KIND_MAP = {"timeout": "timeout", "error": "tool-error", "infra": "infra"}


def _events_from_checkpoint(entry: dict, arm: str, rnd: int) -> list[FailureEvent]:
    events: list[FailureEvent] = []
    for bucket in ("stream_failures", "holdout_failures"):
        for raw_kind, count in (entry.get(bucket) or {}).items():
            kind = KIND_MAP.get(str(raw_kind), "other")
            for index in range(int(count or 0)):
                events.append(FailureEvent(f"{arm}-r{rnd}-{bucket}-{raw_kind}-{index}", kind, arm))
    return events


def test_checkpoint_failure_counts_become_lessons():
    entry = {"stream_failures": {"timeout": 0, "error": 3, "infra": 0},
             "holdout_failures": {"timeout": 2, "error": 0, "infra": 0}}
    lessons = derive_lessons(_events_from_checkpoint(entry, "no_evolution", 3))
    subjects = {l.subject: l.occurrences for l in lessons}
    assert subjects == {"failure-tool-error-no-evolution": 3,
                        "failure-timeout-no-evolution": 2}, subjects


def test_zero_failures_produce_no_lessons():
    entry = {"stream_failures": {"timeout": 0}, "holdout_failures": {k: 0 for k in KIND_MAP}}
    assert derive_lessons(_events_from_checkpoint(entry, "arm", 1)) == []


def test_single_failure_kind_not_sedimented():
    """单次失败不够门槛——反思不能把偶发当规律。"""
    entry = {"holdout_failures": {"timeout": 1}}
    assert derive_lessons(_events_from_checkpoint(entry, "arm", 1)) == []


def test_infra_counted_but_marked_not_capability():
    entry = {"holdout_failures": {"infra": 2}}
    lessons = derive_lessons(_events_from_checkpoint(entry, "arm", 2))
    assert len(lessons) == 1
    payload = lesson_to_memory(lessons[0])
    assert "不是能力问题" in payload["content"], "infra 教训必须声明不是能力问题"


def test_lesson_file_lands_in_arm_home_memories(tmp_path):
    """落盘路径契约：<ws>/<arm>/home/.evoharness/memories/frontmatter 合法。"""
    mem_dir = tmp_path / "arm" / "home" / ".evoharness" / "memories"
    mem_dir.mkdir(parents=True)
    entry = {"holdout_failures": {"timeout": 2}}
    for lesson in derive_lessons(_events_from_checkpoint(entry, "arm", 1)):
        payload = lesson_to_memory(lesson)
        (mem_dir / payload["filename"]).write_text(payload["content"], encoding="utf-8")

    written = list(mem_dir.glob("feedback_*.md"))
    assert written, "教训必须落进该 arm 的 HOME 记忆目录"
    from agents.memory import parse_frontmatter
    meta = parse_frontmatter(written[0].read_text(encoding="utf-8")).meta
    assert meta.get("type") == "feedback"
    assert meta.get("subject", "").startswith("failure-")


def test_reflection_switch_defaults_off():
    """默认关闭：轮次行为与既有实验一致，打开才做对照。"""
    source = (REPO / "eval" / "evolution_experiment.py").read_text(encoding="utf-8")
    assert 'EVOHARNESS_REFLECTION", "0"' in source, "反思开关默认必须是关"
