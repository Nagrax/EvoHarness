"""失败反思沉淀（移植 Reflexion 式 verbal self-reflection 到文件记忆体系）。

捕捉的设计：Reflexion（Shinn et al. 2023）的核心不是"再试一次"，而是把失败
转成**可复用的语言化教训**存进情景记忆，下一次遇到同类任务时召回。
本项目的对应缺口：`skill_evolution` 从成功轨迹里提炼 skill，但**失败教训没有沉淀**——
同一个坑下次还踩。

本层只做三件事（不做自动重试、不改 agent 主循环）：
  R1 证据门槛：同类失败 ≥2 次才沉淀（单次失败可能只是噪声，写进记忆会污染）
  R2 结构化教训：subject 可被 L1 消解复用，正文含"症状/根因/下次动作"三段
  R3 只写不覆盖：撞上同 subject 时交给 memory_lifecycle 的 L1 规则处理

与既有分工一致：确定性（计数、去重、格式）交给程序，语义（根因是否成立）留给模型/人。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# 同类失败的判定键：错误类型 + 任务族（避免把"超时"和"答案错误"混成一类）
FAILURE_KINDS = ("timeout", "no-final-marker", "wrong-answer", "tool-error", "infra")
MIN_OCCURRENCES = 2  # R1 证据门槛


@dataclass
class FailureEvent:
    """一次失败观测（来自评测器的逐题记录）。"""

    task_id: str
    kind: str            # FAILURE_KINDS 之一
    family: str = ""     # 任务族/holdout 分层，用于判定"同类"
    detail: str = ""     # 原始错误信息（截断保存）
    at: float = field(default_factory=time.time)


@dataclass
class Lesson:
    """由同类失败聚合出的教训。"""

    subject: str
    kind: str
    family: str
    occurrences: int
    symptoms: str
    next_action: str


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text.lower()).strip("-")


def lesson_subject(kind: str, family: str) -> str:
    """教训的 subject：kind + family 规范化。同一 subject 会被 L1 冲突消解覆盖。"""
    return _normalize(f"failure-{kind}-{family or 'general'}")


def group_failures(events: list[FailureEvent]) -> dict[str, list[FailureEvent]]:
    """按 (kind, family) 分组；未知 kind 归入 'other' 以防丢观测。"""
    groups: dict[str, list[FailureEvent]] = {}
    for event in events:
        kind = event.kind if event.kind in FAILURE_KINDS else "other"
        key = f"{kind}|{event.family or 'general'}"
        groups.setdefault(key, []).append(event)
    return groups


def derive_lessons(events: list[FailureEvent], *,
                   min_occurrences: int = MIN_OCCURRENCES) -> list[Lesson]:
    """把失败观测聚合为教训；达不到门槛的**不产出教训**（防噪声写进记忆）。"""
    lessons: list[Lesson] = []
    for key, group in group_failures(events).items():
        if len(group) < min_occurrences:
            continue
        kind, _, family = key.partition("|")
        detail_samples = [e.detail.strip()[:160] for e in group if e.detail.strip()]
        symptoms = (f"{len(group)} 次同类失败（{kind}"
                    + (f"/{family}" if family != "general" else "") + "）")
        if detail_samples:
            # 去重后保留前两条样例，别把整段日志写进记忆
            uniq = list(dict.fromkeys(detail_samples))[:2]
            symptoms += "；样例：" + " | ".join(uniq)
        lessons.append(Lesson(
            subject=lesson_subject(kind, family if family != "general" else ""),
            kind=kind,
            family=family if family != "general" else "",
            occurrences=len(group),
            symptoms=symptoms,
            next_action=_next_action_for(kind),
        ))
    return lessons


def _next_action_for(kind: str) -> str:
    """按失败类型给出"下次动作"——这是反思里唯一真正可执行的部分。"""
    return {
        "timeout": "先缩小搜索面再看时间预算；超过一半预算仍无进展就换路径，不要死磕。",
        "no-final-marker": "收尾时必须显式输出最终答案标记；推理结束前自检一次是否已给结论。",
        "wrong-answer": "给出结论前把关键事实回读一遍原文，确认没有把假设当证据。",
        "tool-error": "工具报错先读错误原文再改参数；同一调用连续失败两次就换工具或换思路。",
        "infra": "平台/网关错误不是能力问题：记录并跳过，不要为它改策略。",
    }.get(kind, "记录具体症状，下次遇到同类任务先回看这条教训。")


def render_lesson(lesson: Lesson) -> str:
    """渲染成可写入记忆文件的正文（三段式，便于模型快速消费）。"""
    return (
        "## 症状\n"
        f"{lesson.symptoms}\n\n"
        "## 根因\n"
        f"（待确认）同类失败 {lesson.occurrences} 次，"
        f"类型 {lesson.kind}"
        + (f"，任务族 {lesson.family}" if lesson.family else "") + "。\n\n"
        "## 下次动作\n"
        f"{lesson.next_action}\n"
    )


def lesson_to_memory(lesson: Lesson) -> dict:
    """产出可直接交给 write_file 的记忆文件内容（frontmatter 与既有格式一致）。"""
    name = lesson.subject
    return {
        "filename": f"feedback_{name}.md",
        "content": (
            "---\n"
            f"name: {name}\n"
            f"subject: {lesson.subject}\n"
            "type: feedback\n"
            f"description: 失败教训（{lesson.kind}"
            + (f"/{lesson.family}" if lesson.family else "") + f"），{lesson.occurrences} 次观测\n"
            "---\n\n"
            + render_lesson(lesson)
        ),
        "subject": lesson.subject,
        "occurrences": lesson.occurrences,
    }


def extract_failures_from_predictions(records: list[dict], *,
                                      classify=None) -> list[FailureEvent]:
    """从评测逐题记录里提取失败事件（接入真实评测数据的唯一入口）。

    classify: 可注入的判分函数（默认用 eval.run_benchmark.classify_record），
    保持本层不依赖评测器的具体实现。
    """
    if classify is None:
        from eval.run_benchmark import classify_record  # 延迟导入，避免循环依赖
        classify = classify_record

    events: list[FailureEvent] = []
    for record in records:
        task_id = str(record.get("task_id") or "")
        family = str(record.get("family") or record.get("level") or "")
        if record.get("timed_out"):
            events.append(FailureEvent(task_id, "timeout", family, "timed out"))
            continue
        error = str(record.get("error") or "")
        if error:
            # 平台/网关错误单列，不能算能力问题（验收纪律）
            infra_markers = ("aggregate:", "502", "503", "429", "no available candidates")
            is_infra = any(marker in error.lower() for marker in infra_markers)
            kind = "infra" if is_infra else "tool-error"
            events.append(FailureEvent(task_id, kind, family, error[:200]))
            continue
        if not record.get("has_final_marker"):
            events.append(FailureEvent(task_id, "no-final-marker", family,
                                       "no final answer marker emitted"))
            continue
        try:
            verdict = classify(record, str(record.get("dataset") or "gaia"))
        except Exception:  # noqa: BLE001
            continue
        if verdict == "correct":
            continue
        events.append(FailureEvent(task_id, "wrong-answer", family,
                                   f"prediction={str(record.get('prediction'))[:80]}"))
    return events
