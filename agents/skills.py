from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .frontmatter import parse_frontmatter
from .skill_evolution import (
    create_skill_file,
    evolve_skill_file,
    format_skill_stats,
    record_online_skill_provenance,
    record_skill_feedback,
    record_skill_invocation,
    record_skill_usage_judgments,
)
from .skill_status import (
    PROVISIONAL_STATUSES,
    STATUS_WEIGHTS,
    get_effective_status_map,
    get_trusted_status_map,
)


@dataclass
class SkillDefinition:
    # 一个 skill 在程序内的统一表示，由 SKILL.md 的 frontmatter 和正文解析得到。
    name: str
    description: str
    when_to_use: str | None = None
    allowed_tools: list[str] | None = None
    user_invocable: bool = True
    context: str = "inline"  # "inline" or "fork"
    prompt_template: str = ""
    source: str = "project"  # "project" or "user"
    skill_dir: str = ""


# skills 只在首次读取时扫描磁盘，后续复用缓存；修改 skill 后需要重启或 reset。
_cached_skills: list[SkillDefinition] | None = None


def execute_skill(skill_name:str, args:object)-> dict | None:
    # skill 工具的执行入口：按名字找到 skill，并返回解析后的 prompt 和执行配置。
    skill = get_skill_by_name(skill_name)
    if not skill:
        return None

    record_skill_invocation(
        skill_name=skill.name,
        source=skill.source,
        context=skill.context,
        args=args,
    )

    return {
        "prompt": resolve_skill_prompt(skill, args),
        "allowed_tools": skill.allowed_tools,
        "context": skill.context,
        "source": skill.source,
        "skill_dir": skill.skill_dir,
    }



def resolve_skill_prompt(skill: SkillDefinition, args: object) -> str:
    import re
    prompt = skill.prompt_template
    # 支持在 SKILL.md 正文中使用 $ARGUMENTS 或 ${ARGUMENTS} 引用用户参数。
    prompt = re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", str(args or ""), prompt)
    # 支持 skill 引用自己的目录，例如读取同目录下的 references/scripts。
    prompt = prompt.replace("${CLAUDE_SKILL_DIR}", skill.skill_dir)
    return prompt

def get_skill_by_name(skill_name:str)->SkillDefinition | None:
    # 通过 name 查找 skill；name 来自 frontmatter，没有写时使用目录名。
    for s in discover_skills():
        if s.name == skill_name:
            return s
    return None

def discover_skills() -> list[SkillDefinition]:
    global _cached_skills
    if _cached_skills is not None:
        return _cached_skills

    skills: dict[str,SkillDefinition] = {}
    # 用户级 skills 优先级最高：~/.evoharness/skills/<name>/SKILL.md
    user_dir = Path.home() / ".evoharness" / "skills"
    _load_skills_from_dir(user_dir, "user", skills)
    # 项目级 skills 优先级较低：<cwd>/.evoharness/skills/<name>/SKILL.md
    project_dir = Path.cwd() / ".evoharness" / "skills"
    _load_skills_from_dir(project_dir, "project", skills, overwrite=False)

    _cached_skills = list(skills.values())
    return _cached_skills

def _load_skills_from_dir( base_dir: Path, source: str, skills:dict[str, SkillDefinition], overwrite: bool = True) -> None:
    # 只加载目录形式的 skill，不加载 .evoharness/skills/foo.md 这种单文件形式。
    if not base_dir.is_dir():
        return
    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        # 文件名必须是 SKILL.md，大小写要一致。
        skill_file = entry/  "SKILL.md"
        if not skill_file.exists():
            continue
        skill = _parse_skill_file(skill_file, source, str(entry))
        if skill:
            # 项目级加载时 overwrite=False，避免覆盖同名用户级 skill。
            if not overwrite and skill.name in skills:
                continue
            skills[skill.name] = skill

def _parse_skill_file(file_path: Path, source: str, skill_dir: str) -> SkillDefinition:
    try:
        # SKILL.md = frontmatter 配置 + markdown 正文。
        raw = file_path.read_text()
        result = parse_frontmatter(raw)
        meta = result.meta

        # name 没写时用目录名；user-invocable 默认 true；context 默认 inline。
        name = meta.get("name") or file_path.parent.name or "unknown"
        user_invocable = meta.get("user-invocable", "true") != "false"
        context = "fork" if meta.get("context") == "fork" else "inline"

        allowed_tools: list[str] | None = None
        if "allowed-tools" in meta:
            raw_tools = meta["allowed-tools"]
            # allowed-tools 支持 JSON 数组字符串，也支持逗号分隔。
            if raw_tools.startswith("["):
                try:
                    allowed_tools = json.loads(raw_tools)
                except Exception:
                    allowed_tools = [s.strip() for s in raw_tools.strip("[]").split(",")]
            else:
                allowed_tools = [s.strip() for s in raw_tools.split(",")]

        return SkillDefinition(
            name=name,
            description=meta.get("description", ""),
            when_to_use=meta.get("when_to_use") or meta.get("when-to-use"),
            allowed_tools=allowed_tools,
            user_invocable=user_invocable,
            context=context,
            prompt_template=result.body,
            source=source,
            skill_dir=skill_dir,
        )

    except Exception:
        return None


def build_skill_descriptions() -> str:
    # 把已加载的 skills 写进 system prompt，让模型知道哪些 skill 可用。
    skills = discover_skills()

    lines = ["# Available Skills", ""]
    if not skills:
        lines.append("(No skills are currently registered.)")
        lines.append("")
    # user_invocable=True 的 skill 主要给用户通过 /<name> 手动调用。
    invocable = [s for s in skills if s.user_invocable]
    # user_invocable=False 的 skill 作为自动调用候选，模型根据 when_to_use 决定是否调用 skill 工具。
    auto_only = [s for s in skills if not s.user_invocable]

    if invocable:
        lines.append("User-invocable skills (user types /<name> to invoke):")
        for s in invocable:
            lines.append(f"- **/{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  When to use: {s.when_to_use}")
        lines.append("")

    if auto_only:
        lines.append("Auto-invocable skills:")
        lines.append("When the user's request matches a skill's When to use, call the `skill` tool with that skill name before continuing. Do not ask the user to invoke it manually.")
        for s in auto_only:
            lines.append(f"- **{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  When to use: {s.when_to_use}")
        lines.append("")

    lines.append("To invoke a skill programmatically, use the `skill` tool with the skill name and optional arguments.")
    lines.append("")
    lines.append("# Skill Evolution")
    lines.append("EvoHarness has an online skill evolution loop after each assistant response. Do not create or evolve skills during normal task execution unless the user explicitly asks for manual skill maintenance.")
    lines.append("If manual maintenance is explicitly requested, call `skill_evolve` only for durable reusable feedback on an existing skill, and call `skill_create` only when no suitable existing skill exists.")
    lines.append("Never create or evolve skills from one-off task content, private secrets, temporary project facts, or assistant-only guesses.")
    return "\n".join(lines)


_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{1,2}")
_STOP_TOKENS = {
    "请帮",
    "帮我",
    "我做",
    "做一",
    "一次",
    "一下",
    "这个",
    "那个",
    "一个",
    "用户",
    "问题",
    "回答",
    "生成",
    "使用",
    "需要",
}


def _tokens(text: str) -> set[str]:
    raw = str(text or "").lower().replace("_", " ").replace("-", " ")
    found = {m.group(0) for m in _TOKEN_RE.finditer(raw)}
    expanded = set(found)
    for token in found:
        if len(token) > 3 and token.endswith("s"):
            expanded.add(token[:-1])
    found = expanded
    cjk = re.findall(r"[\u4e00-\u9fff]+", raw)
    for chunk in cjk:
        if len(chunk) >= 2:
            found.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return {x for x in found if x.strip() and x not in _STOP_TOKENS}


def _token_list(text: str) -> list[str]:
    raw = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens = [m.group(0) for m in _TOKEN_RE.finditer(raw)]
    for chunk in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(chunk) >= 2:
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    expanded: list[str] = []
    for token in tokens:
        if not token.strip() or token in _STOP_TOKENS:
            continue
        expanded.append(token)
        if len(token) > 3 and token.endswith("s"):
            expanded.append(token[:-1])
    return expanded


def _skill_search_text(skill: SkillDefinition) -> str:
    return "\n".join(
        [
            skill.name,
            skill.description,
            skill.when_to_use or "",
            skill.prompt_template[:4000],
        ]
    )


def skill_terms_from_text(name: str, description: str, when_to_use: str, body: str) -> list[str]:
    # 检索语料 = 元信息（名字+描述+触发条件）权重 x3 + 正文前 2500 字。
    meta_terms = _token_list("\n".join([str(name or ""), str(description or ""), str(when_to_use or "")]))
    return (meta_terms * 3) + _token_list(str(body or "")[:2500])


def build_retrieval_corpus(skills: list[SkillDefinition] | None = None) -> dict[str, list[str]]:
    """把 skill 列表转成检索语料；检索与触发变体的重放评测共用。"""
    skills = skills if skills is not None else discover_skills()
    corpus: dict[str, list[str]] = {}
    for skill in skills:
        terms = skill_terms_from_text(skill.name, skill.description, skill.when_to_use or "", skill.prompt_template)
        if terms:
            corpus[skill.name] = terms
    return corpus


def rank_corpus_for_query(query: str, corpus: dict[str, list[str]]) -> list[tuple[str, float]]:
    """BM25-lite 打分：返回 (skill 名, 归一化分数) 降序列表。不含名字加分与状态权重。"""
    query_tokens = set(_token_list(query))
    if not query_tokens or not corpus:
        return []
    document_frequency: Counter[str] = Counter()
    total_len = 0
    for terms in corpus.values():
        document_frequency.update(set(terms))
        total_len += len(terms)
    avg_doc_len = total_len / max(1, len(corpus))
    doc_count = len(corpus)
    k1 = 1.4
    b = 0.75
    scored: list[tuple[str, float]] = []
    for name, terms in corpus.items():
        term_counts = Counter(terms)
        overlap = query_tokens & set(terms)
        if not overlap:
            continue
        raw_score = 0.0
        doc_len = max(1, len(terms))
        for token in overlap:
            tf = term_counts[token]
            idf = math.log(1 + (doc_count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            denom = tf + k1 * (1 - b + b * doc_len / max(1.0, avg_doc_len))
            raw_score += idf * (tf * (k1 + 1)) / max(denom, 0.0001)
        scored.append((name, raw_score / max(3.0, len(query_tokens))))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def retrieve_relevant_skills(
    query: str,
    *,
    limit: int = 3,
    min_score: float = 0.08,
) -> list[dict[str, Any]]:
    skills = discover_skills()
    if not skills:
        return []
    by_name = {skill.name: skill for skill in skills}
    corpus = build_retrieval_corpus(skills)
    # 状态联动：healthy 加权 / watch 降权只对评测认证过的状态生效；新 skill 不惩罚，
    # 否则检索量本来就少的孵化期 skill 会被饿死在起跑线。
    trusted = get_trusted_status_map()
    effective = get_effective_status_map()
    hits: list[dict[str, Any]] = []
    for name, base_score in rank_corpus_for_query(query, corpus):
        skill = by_name.get(name)
        if skill is None:
            continue
        name_bonus = 0.15 if skill.name.lower() in str(query or "").lower() else 0.0
        weight = STATUS_WEIGHTS.get(trusted.get(skill.name, ""), 1.0)
        score = min(1.0, base_score + name_bonus) * weight
        if score < float(min_score):
            continue
        hits.append(
            {
                "score": float(score),
                "name": skill.name,
                "description": skill.description,
                "when_to_use": skill.when_to_use or "",
                "source": skill.source,
                "context": skill.context,
                "user_invocable": bool(skill.user_invocable),
                "skill_dir": skill.skill_dir,
                "status": effective.get(skill.name, "incubating"),
            }
        )

    hits.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    return hits[: max(1, int(limit or 1))]


# ─── 组合冲突静态检测：输出要求互斥对 + 触发条件重叠度 ──────────────

_MUTUALLY_EXCLUSIVE_FLAGS = (("json", "table"),)
_CONFLICT_OVERLAP_THRESHOLD = 0.6
_conflict_cache: dict[str, list[str]] | None = None


def _output_flags(skill: SkillDefinition) -> set[str]:
    text = " ".join(
        [skill.name, skill.description, skill.when_to_use or "", (skill.prompt_template or "")[:1500]]
    ).lower()
    flags: set[str] = set()
    if "json" in text or "结构化输出" in text:
        flags.add("json")
    if "markdown table" in text or "表格" in text:
        flags.add("table")
    return flags


def compute_skill_conflicts(skills: list[SkillDefinition] | None = None) -> dict[str, list[str]]:
    """两两检测 skill 之间的静态冲突：输出要求互斥（一个要纯 JSON 一个要表格）、
    或 when_to_use 分词重叠度过高（触发条件撞车）。结果随 skill 缓存。"""
    global _conflict_cache
    if _conflict_cache is not None and skills is None:
        return _conflict_cache
    skills = skills if skills is not None else discover_skills()
    when_tokens = {s.name: _tokens((s.when_to_use or "") + " " + s.description) for s in skills}
    flags = {s.name: _output_flags(s) for s in skills}
    conflicts = {s.name: [] for s in skills}
    for i in range(len(skills)):
        for j in range(i + 1, len(skills)):
            a, b = skills[i].name, skills[j].name
            ta, tb = when_tokens[a], when_tokens[b]
            union = ta | tb
            overlap = (len(ta & tb) / len(union)) if union else 0.0
            flag_clash = any(
                (fa in flags[a] and fb in flags[b]) or (fb in flags[a] and fa in flags[b])
                for fa, fb in _MUTUALLY_EXCLUSIVE_FLAGS
            )
            if overlap >= _CONFLICT_OVERLAP_THRESHOLD or flag_clash:
                conflicts[a].append(b)
                conflicts[b].append(a)
    _conflict_cache = conflicts
    return conflicts


def format_retrieved_skill_context(query: str, *, limit: int = 3) -> tuple[str, dict[str, Any] | None]:
    hits = retrieve_relevant_skills(query, limit=limit)
    if not hits:
        return "", None
    conflicts = compute_skill_conflicts()
    retrieved_names = {hit["name"] for hit in hits}
    lines = [
        "<retrieved_skills>",
        "These skills were retrieved for the current user request. Use a skill only if it directly matches the user's intent; otherwise ignore this block.",
    ]
    for idx, hit in enumerate(hits, start=1):
        notes = ""
        if str(hit.get("status", "")) in PROVISIONAL_STATUSES:
            notes += " [provisional: not yet validated]"
        clash = [n for n in conflicts.get(hit["name"], []) if n in retrieved_names]
        if clash:
            notes += f" [conflicts with {', '.join(sorted(clash))}; choose by current intent]"
        lines.append(
            f"{idx}. {hit['name']} (score={float(hit['score']):.3f}, source={hit['source']}){notes}: {hit['description']}"
        )
        if hit.get("when_to_use"):
            lines.append(f"   When to use: {hit['when_to_use']}")
    lines.append("</retrieved_skills>")
    top = dict(hits[0])
    top["all_hits"] = hits
    return "\n".join(lines), top


def reset_skill_cache() -> None:
    # 测试或运行中刷新 skills 时使用；普通用户通常重启程序即可。
    global _cached_skills, _conflict_cache
    _cached_skills = None
    _conflict_cache = None


def evolve_skill(
    skill_name: str,
    lesson: str,
    rationale: str = "",
    target: str = "active",
    instructions: str = "",
    description: str = "",
    when_to_use: str = "",
    tags: list[str] | None = None,
) -> dict:
    skill = get_skill_by_name(skill_name)
    result = evolve_skill_file(
        skill_name=skill_name,
        lesson=lesson,
        rationale=rationale,
        target=target,
        active_dir=skill.skill_dir if skill else "",
        instructions=instructions,
        description=description,
        when_to_use=when_to_use,
        tags=tags,
    )
    if result.get("ok"):
        reset_skill_cache()
    return result


def create_skill(
    name: str,
    description: str,
    instructions: str,
    when_to_use: str = "",
    target: str = "project",
    context: str = "inline",
    user_invocable: bool = False,
    allowed_tools: object = None,
    evidence: str = "",
    actor: str = "agent",
    tags: list[str] | None = None,
) -> dict:
    result = create_skill_file(
        name=name,
        description=description,
        instructions=instructions,
        when_to_use=when_to_use,
        target=target,
        context=context,
        user_invocable=user_invocable,
        allowed_tools=allowed_tools,
        evidence=evidence,
        actor=actor,
        tags=tags,
    )
    if result.get("ok"):
        reset_skill_cache()
    return result


def record_online_provenance(
    *,
    action: str,
    skill_name: str = "",
    result: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    retrieved_reference: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    record_online_skill_provenance(
        action=action,
        skill_name=skill_name,
        result=result,
        messages=messages,
        retrieved_reference=retrieved_reference,
        decision=decision,
        error=error,
    )


def record_feedback(skill_name: str, rating: str, note: str = "") -> None:
    record_skill_feedback(skill_name=skill_name, rating=rating, note=note)


def skill_stats() -> str:
    return format_skill_stats()


def record_usage_judgments(judgments: list[dict[str, Any]]) -> dict[str, Any]:
    result = record_skill_usage_judgments(judgments)
    if result.get("pruned"):
        reset_skill_cache()
    return result
