"""记忆生命周期层：移植 mem0/Letta 类系统的核心机制到本项目的文件记忆体系。

捕捉的前沿设计（依据：docs/interview/Harness项目面经.md 引用的 mem0/TencentDB Agent Memory
与 Letta/MemGPT 分页记忆；未读原文，按公开常识抽象其机制）：
  L1 冲突消解：同 subject 新记忆覆盖旧记忆（保留 provenance 与版本历史）
  L2 合并重复：相似度超阈值时合并而非新增
  L3 过期降权：stale 记忆召回权重衰减 + 注入时警告强化

本模块只新增生命周期操作，不改变既有 frontmatter 格式与召回链路
（M1-M7 既有断言必须继续通过——这是移植不破坏本体的验证方式）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MemoryRecord:
    name: str
    subject: str          # L1 冲突消解的主键（同一 subject 只保留一份有效记忆）
    mtype: str
    description: str
    content: str
    updated_at: float     # epoch 秒
    version: int = 1
    superseded_by: str = ""   # L1：被谁覆盖
    supersedes: str = ""      # L1：覆盖了谁


def parse_memory_file(path: Path) -> MemoryRecord | None:
    """从既有 frontmatter 解析 MemoryRecord；无 frontmatter 返回 None。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    if not m:
        return None
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip("'\"")
    name = meta.get("name") or path.stem
    # subject 缺省用 name 规范化（兼容既有记忆文件——没有 subject 就用自身）
    subject = meta.get("subject") or re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", name.lower()).strip("-")
    try:
        updated = path.stat().st_mtime
    except OSError:
        updated = time.time()
    return MemoryRecord(
        name=name, subject=subject, mtype=meta.get("type") or "project",
        description=meta.get("description") or "", content=m.group(2),
        updated_at=updated,
        version=int(meta.get("version") or 1),
    )


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower()))


def similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    ta, tb = _tokens(a.subject + " " + a.description), _tokens(b.subject + " " + b.description)
    union = ta | tb
    return (len(ta & tb) / len(union)) if union else 0.0


# L2 合并阈值：与 skill 去重一致的可调常量（中文分词 Jaccard 偏低的教训已吸取，取 0.4）
MERGE_THRESHOLD = 0.4


def resolve_conflicts(records: list[MemoryRecord], *, now: float | None = None) -> dict:
    """对候选记忆做生命周期消解，返回四路决策与保留集。

    决策语义（对齐 skill 进化的 add/merge/revise/discard）：
    - 同 subject：新覆盖旧（revise/supersede），旧版本记入 superseded
    - 不同 subject 但相似度 >= MERGE_THRESHOLD：建议 merge（返回建议，不自动合并——
      语义判断留给模型/人，这是"确定性交给程序、语义交给模型"的既有分工）
    - 孤立记录：add
    过期判定单独返回（L3），不做删除——只降权。
    """
    now = now if now is not None else time.time()
    by_subject: dict[str, MemoryRecord] = {}
    decisions: list[dict] = []
    superseded: list[dict] = []
    merge_suggestions: list[dict] = []
    kept: list[MemoryRecord] = []

    ordered = sorted(records, key=lambda r: r.updated_at, reverse=True)
    for record in ordered:
        winner = by_subject.get(record.subject)
        if winner is None:
            by_subject[record.subject] = record
            kept.append(record)
            decisions.append({"name": record.name, "action": "add", "why": "first record for subject"})
            continue
        if record.updated_at > winner.updated_at:
            by_subject[record.subject] = record
            kept = [r for r in kept if r.subject != record.subject]
            kept.append(record)
            superseded.append({
                "superseded": winner.name, "by": record.name,
                "oldVersion": winner.version, "newVersion": record.version,
            })
            decisions.append({"name": record.name, "action": "revise",
                              "why": f"newer than {winner.name} for same subject"})
        else:
            superseded.append({"superseded": record.name, "by": winner.name,
                               "oldVersion": record.version, "newVersion": winner.version})
            decisions.append({"name": record.name, "action": "discard",
                              "why": f"older than {winner.name} for same subject"})

    # L2 相似但不冲突的孤立对：只建议，不自动合并
    for i in range(len(kept)):
        for j in range(i + 1, len(kept)):
            sim = similarity(kept[i], kept[j])
            if sim >= MERGE_THRESHOLD:
                merge_suggestions.append({
                    "pair": [kept[i].name, kept[j].name],
                    "similarity": round(sim, 2),
                    "suggestedAction": "merge",
                })

    stale_days = {r.name: max(0.0, (now - r.updated_at) / 86400) for r in records}
    return {
        "kept": kept,
        "superseded": superseded,
        "decisions": decisions,
        "mergeSuggestions": merge_suggestions,
        "staleDays": stale_days,
    }


def recall_weight(record: MemoryRecord, *, now: float | None = None,
                   base: float = 1.0, decay_per_day: float = 0.02,
                   floor: float = 0.5) -> float:
    """L3 过期降权：每天衰减 2%，下限 0.5（记忆是有损便签，不是档案——既有定位）。"""
    now = now if now is not None else time.time()
    days = max(0.0, (now - record.updated_at) / 86400)
    return max(floor, base - days * decay_per_day)


def scan_memory_health(memory_dir: Path | str, *, now: float | None = None,
                       stale_days: int = 30) -> dict:
    """写入后健康扫描：把 L1 冲突与 L2 重复变成**可见提示**，不静默改文件。

    设计约束（重要）：这里只报告，不删除、不改写任何记忆文件——
    模型刚写下的内容不能被程序悄悄吞掉；语义判断（是否真该合并/替换）
    留给人或模型。程序只负责把"你写了一条会覆盖旧记忆的内容"摆到台面上。

    返回 {records, unparsed, conflicts, duplicates, stale, summary}。
    """
    now = now if now is not None else time.time()
    directory = Path(memory_dir)
    records: list[MemoryRecord] = []
    unparsed: list[str] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            record = parse_memory_file(path)
            if record is None:
                unparsed.append(path.name)
            else:
                records.append(record)

    resolved = resolve_conflicts(records, now=now)
    stale = [name for name, days in resolved["staleDays"].items() if days >= stale_days]
    return {
        "records": [r.name for r in records],
        "unparsed": unparsed,
        "conflicts": resolved["superseded"],
        "duplicates": resolved["mergeSuggestions"],
        "stale": stale,
        "summary": {
            "total": len(records),
            "conflicts": len(resolved["superseded"]),
            "duplicates": len(resolved["mergeSuggestions"]),
            "stale": len(stale),
            "unparsed": len(unparsed),
        },
    }
