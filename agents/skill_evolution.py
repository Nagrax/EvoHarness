from __future__ import annotations

import json
import os
import re
import shutil
import time
import hashlib
from pathlib import Path
from typing import Any

from .frontmatter import format_frontmatter, parse_frontmatter


USAGE_LOG = "usage.jsonl"
USAGE_SAMPLES = "skill-usage-samples.jsonl"
_usage_sample_day_counts: dict[str, int] = {}
ONLINE_PROVENANCE_LOG = "online_provenance.jsonl"
ONLINE_PROVENANCE_INDEX = "online_skill_provenance.json"
SKILL_USAGE_STATS = "skill_usage_stats.json"
SKILL_LOAD_ERRORS_LOG = "skill_load_errors.jsonl"
HISTORY_DIR = "history"
HOLDOUT_READONLY_ENV = "EVOHARNESS_HOLDOUT_READONLY"


def skill_state_read_only() -> bool:
    """holdout 只读模式：禁止一切 Skill/usage/provenance/status/tier 写入。

    只由评测进程显式设置（`EVOHARNESS_HOLDOUT_READONLY=1`），默认关闭，不影响生产路径。
    """
    raw = os.environ.get(HOLDOUT_READONLY_ENV, "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_evolution_dir() -> Path:
    return Path.cwd() / ".evoharness" / "skill-evolution"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    if skill_state_read_only():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def record_skill_load_issue(
    *,
    event: str,
    file: str,
    error_type: str = "",
    error: str = "",
    reason: str = "",
) -> None:
    """记录 SKILL.md 加载层问题（load_failed=解析抛异常 / load_warning=静默行为改变）。

    解析失败发生在自进化上游：文件进不了 SkillDefinition，进化体系看不见它，
    信号也不在对话层输入里——所以溯源必须由加载层用确定性代码落盘，不走 LLM。
    """
    _append_jsonl(get_evolution_dir() / SKILL_LOAD_ERRORS_LOG, {
        "event": event,
        "time": _utc_now(),
        "file": file,
        "error_type": error_type,
        "error": str(error)[:500],
        "reason": reason,
    })


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, value: Any) -> None:
    if skill_state_read_only():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_skill_slug(name: str) -> str:
    raw = str(name or "").strip()
    slug = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "-", raw)
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    if slug:
        return slug[:120].rstrip("-.") or slug[:120]
    if raw:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        return f"skill-{digest}"
    return "unknown"


def _preview(value: object, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _compact_messages(messages: list[dict[str, Any]], *, max_messages: int = 12, max_chars: int = 4000) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in list(messages or [])[-max_messages:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        out.append({"role": role, "content": _preview(content, max_chars)})
    return out


def record_skill_invocation(
    *,
    skill_name: str,
    source: str,
    context: str,
    args: object = "",
) -> None:
    row = {
        "event": "invoke",
        "time": _utc_now(),
        "skill": skill_name,
        "source": source,
        "context": context,
        "args_preview": _preview(args),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, row)


def record_skill_feedback(
    *,
    skill_name: str,
    rating: str,
    note: str = "",
) -> None:
    row = {
        "event": "feedback",
        "time": _utc_now(),
        "skill": skill_name,
        "rating": str(rating or "").strip(),
        "note": _preview(note, 1200),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, row)


def record_online_skill_provenance(
    *,
    action: str,
    skill_name: str = "",
    result: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    retrieved_reference: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    row = {
        "event": "online_ingest",
        "time": _utc_now(),
        "action": str(action or "none").strip() or "none",
        "skill": str(skill_name or "").strip(),
        "ok": bool((result or {}).get("ok")) if result is not None else not bool(error),
        "result": result or {},
        "messages": _compact_messages(list(messages or [])),
        "retrieved_reference": retrieved_reference or {},
        "decision": decision or {},
        "error": _preview(error, 1200),
    }
    _append_jsonl(get_evolution_dir() / ONLINE_PROVENANCE_LOG, row)
    _update_online_provenance_index(row)


def _update_online_provenance_index(row: dict[str, Any]) -> None:
    skill = str(row.get("skill") or "").strip()
    if not skill:
        return
    root = get_evolution_dir()
    path = root / ONLINE_PROVENANCE_INDEX
    index = _read_json(path, {})
    item = index.setdefault(
        skill,
        {
            "skill": skill,
            "source_count": 0,
            "history_count": 0,
            "sources": [],
            "version_timeline": [],
            "usage": {},
        },
    )
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    item["current_version"] = result.get("version") or item.get("current_version", "")
    item["last_action"] = row.get("action")
    item["last_time"] = row.get("time")
    item["last_ok"] = row.get("ok")
    item["last_error"] = row.get("error", "")
    item["source_count"] = int(item.get("source_count", 0)) + 1
    source = {
        "time": row.get("time"),
        "action": row.get("action"),
        "ok": row.get("ok"),
        "messages": row.get("messages", []),
        "retrieved_reference": row.get("retrieved_reference", {}),
        "decision": row.get("decision", {}),
        "error": row.get("error", ""),
    }
    sources = list(item.get("sources") or [])
    sources.append(source)
    item["sources"] = sources[-20:]
    item["history_count"] = len(sources)
    if result.get("version"):
        timeline = list(item.get("version_timeline") or [])
        timeline.append({"time": row.get("time"), "version": result.get("version"), "action": row.get("action")})
        item["version_timeline"] = timeline[-50:]
    index[skill] = item
    _write_json(path, index)


def _parse_int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


def _bump_patch(version: str | None) -> str:
    raw = str(version or "0.1.0").strip()
    parts = raw.split(".")
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        return "0.1.1"
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts[:3])


def _find_skill_file_by_name(base_dir: Path, skill_name: str) -> Path | None:
    if not base_dir.is_dir():
        return None
    wanted = str(skill_name or "").strip()
    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            parsed = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = parsed.meta.get("name") or entry.name
        if name == wanted:
            return skill_file
    return None


def resolve_skill_file(skill_name: str, *, target: str = "active", active_dir: str = "") -> Path | None:
    target = (target or "active").strip().lower()
    if target == "active" and active_dir:
        skill_file = Path(active_dir) / "SKILL.md"
        if skill_file.is_file():
            return skill_file

    if target in ("project", "active"):
        found = _find_skill_file_by_name(Path.cwd() / ".evoharness" / "skills", skill_name)
        if found:
            return found

    if target in ("user", "active"):
        found = _find_skill_file_by_name(Path.home() / ".evoharness" / "skills", skill_name)
        if found:
            return found

    return None


def _skills_root(target: str) -> Path:
    target = (target or "project").strip().lower()
    if target == "user":
        return Path.home() / ".evoharness" / "skills"
    return Path.cwd() / ".evoharness" / "skills"


def _normalize_context(context: str | None) -> str:
    return "fork" if str(context or "").strip().lower() == "fork" else "inline"


def _normalize_allowed_tools(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return ",".join(part.strip() for part in str(value).split(",") if part.strip())


def _skill_body(instructions: str, evidence: str = "") -> str:
    body = str(instructions or "").strip()
    if not body:
        body = (
            "# Goal\n\n"
            "Apply this reusable skill when the user's request matches the trigger.\n\n"
            "# Workflow\n\n"
            "1. Identify the user's concrete task and constraints.\n"
            "2. Follow the reusable rules captured in this skill.\n"
            "3. Produce the requested output directly.\n"
        )
    if not body.lstrip().startswith("#"):
        body = "# Skill Instructions\n\n" + body
    if evidence:
        body = body.rstrip() + "\n\n## Creation Evidence\n\n" + str(evidence).strip() + "\n"
    return body.rstrip() + "\n"


def create_skill_file(
    *,
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
) -> dict[str, Any]:
    if skill_state_read_only():
        return {"ok": False, "error": "holdout read-only mode: skill creation disabled"}
    resolved_name = str(name or "").strip()
    if not resolved_name:
        return {"ok": False, "error": "skill name is required"}
    if not str(description or "").strip():
        return {"ok": False, "error": "description is required"}

    existing = resolve_skill_file(resolved_name, target="active")
    if existing:
        return {"ok": False, "error": f"Skill already exists: {resolved_name}", "file": str(existing)}

    root = _skills_root(target)
    skill_dir = root / _safe_skill_slug(resolved_name)
    skill_file = skill_dir / "SKILL.md"
    if skill_file.exists():
        return {"ok": False, "error": f"Skill file already exists: {skill_file}"}

    meta = {
        "name": resolved_name,
        "description": re.sub(r"\s+", " ", str(description).strip()),
        "version": "0.1.0",
        "created-at": _utc_now(),
        "user-invocable": "true" if user_invocable else "false",
        "context": _normalize_context(context),
    }
    if when_to_use:
        meta["when-to-use"] = re.sub(r"\s+", " ", str(when_to_use).strip())
    if tags:
        meta["tags"] = ",".join(re.sub(r"\s+", "-", str(tag).strip()) for tag in tags if str(tag).strip())
    tools = _normalize_allowed_tools(allowed_tools)
    if tools:
        meta["allowed-tools"] = tools

    skill_dir.mkdir(parents=True, exist_ok=False)
    body = _skill_body(instructions, evidence)
    skill_file.write_text(format_frontmatter(meta, body), encoding="utf-8")

    event = {
        "event": "create",
        "time": _utc_now(),
        "actor": actor,
        "skill": resolved_name,
        "file": str(skill_file),
        "target": "user" if (target or "").strip().lower() == "user" else "project",
        "description": _preview(description, 1200),
        "when_to_use": _preview(when_to_use, 1200),
        "evidence": _preview(evidence, 1200),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, event)
    try:
        # 部署分区：自进化写出的新 skill 一律先进实验区（注入带标注+禁 fork）。
        # 函数内延迟导入防循环（先例：本文件被 skill_tiers 模块级依赖）；
        # fail-open：注册失败不影响落盘主流程，缺口由 tier_audit.jsonl 缺行暴露。
        from .skill_tiers import register_evolution_write

        register_evolution_write(
            resolved_name,
            producer="create_skill_file",
            evidence={"file": str(skill_file), "actor": actor, "target": event["target"]},
        )
    except Exception:
        pass
    return {"ok": True, **event}


def _append_evolution_note(body: str, lesson: str, rationale: str = "") -> str:
    lesson = re.sub(r"\s+", " ", str(lesson or "").strip())
    rationale = re.sub(r"\s+", " ", str(rationale or "").strip())
    if not lesson:
        raise ValueError("lesson is required")

    bullet = f"- {_today()}: {lesson}"
    if rationale:
        bullet += f" Reason: {rationale}"

    body = str(body or "").rstrip()
    marker = "## Evolution Notes"
    if marker in body:
        if lesson in body:
            return body + "\n"
        return body + "\n" + bullet + "\n"
    return body + "\n\n" + marker + "\n\n" + bullet + "\n"


def evolve_skill_file(
    *,
    skill_name: str,
    lesson: str,
    rationale: str = "",
    target: str = "active",
    active_dir: str = "",
    actor: str = "agent",
    instructions: str = "",
    description: str = "",
    when_to_use: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    if skill_state_read_only():
        return {"ok": False, "error": "holdout read-only mode: skill evolution disabled"}
    skill_file = resolve_skill_file(skill_name, target=target, active_dir=active_dir)
    if not skill_file:
        return {"ok": False, "error": f"Skill not found: {skill_name}"}

    raw = skill_file.read_text(encoding="utf-8")
    parsed = parse_frontmatter(raw)
    meta = dict(parsed.meta)
    resolved_name = meta.get("name") or skill_file.parent.name

    snapshot = {
        "time": _utc_now(),
        "event": "snapshot",
        "actor": actor,
        "skill": resolved_name,
        "file": str(skill_file),
        "version": meta.get("version", "0.1.0"),
        "lesson": _preview(lesson, 1200),
        "rationale": _preview(rationale, 1200),
        "content": raw,
    }
    history_path = get_evolution_dir() / HISTORY_DIR / f"{_safe_skill_slug(resolved_name)}.jsonl"
    _append_jsonl(history_path, snapshot)

    meta["name"] = resolved_name
    meta["version"] = _bump_patch(meta.get("version"))
    meta["last-evolved"] = _utc_now()
    meta["evolution-count"] = str(_parse_int(meta.get("evolution-count"), 0) + 1)
    if description.strip():
        meta["description"] = re.sub(r"\s+", " ", description.strip())
    if when_to_use.strip():
        meta["when-to-use"] = re.sub(r"\s+", " ", when_to_use.strip())
    if tags:
        existing_tags = [part.strip() for part in str(meta.get("tags") or "").split(",") if part.strip()]
        merged_tags = existing_tags[:]
        for tag in tags:
            normalized = re.sub(r"\s+", "-", str(tag).strip())
            if normalized and normalized not in merged_tags:
                merged_tags.append(normalized)
        if merged_tags:
            meta["tags"] = ",".join(merged_tags[:12])

    if instructions.strip():
        new_body = _skill_body(instructions.strip())
        note = _append_evolution_note("", lesson, rationale).strip()
        if note:
            new_body = new_body.rstrip() + "\n\n" + note + "\n"
    else:
        new_body = _append_evolution_note(parsed.body, lesson, rationale)
    skill_file.write_text(format_frontmatter(meta, new_body), encoding="utf-8")

    event = {
        "event": "evolve",
        "time": _utc_now(),
        "actor": actor,
        "skill": resolved_name,
        "file": str(skill_file),
        "version": meta["version"],
        "target": target,
        "lesson": _preview(lesson, 1200),
        "rationale": _preview(rationale, 1200),
        "history": str(history_path),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, event)
    try:
        # 部署分区：修改过的 skill（re）进实验区（设计契约，偏离已在评审存档）；
        # 审计行记录 from_tier 变迁，修复类写入（repair）不改 tier 是另一条路径。
        from .skill_tiers import register_evolution_write

        register_evolution_write(
            resolved_name,
            producer="evolve_skill_file",
            evidence={"file": str(skill_file), "version": meta["version"], "actor": actor},
        )
    except Exception:
        pass
    return {"ok": True, **event}


def record_skill_usage_judgments(judgments: list[dict[str, Any]]) -> dict[str, Any]:
    stats_path = get_evolution_dir() / SKILL_USAGE_STATS
    stats = _read_json(stats_path, {})
    pruned: list[str] = []
    # usage-prune 的真实链路（judge_retrieved_skill_usage → 本函数）产出的 judgments 不带
    # invocations/self_evolved；若原样入 stats，_maybe_prune_stale_skill 的两个守卫（手动
    # /<skill> 调用过、手写 skill）会因字段缺失恒不触发，实为纵深防御、不改变现网行为。
    # 此处从 usage 统计（usage.jsonl 的 invoke/create 事件 → invocations/created_at）与
    # provenance index 补齐这两个字段，让守卫在真实归档路径上实效；口径与僵尸门
    # maybe_archive_never_retrieved_skill 的输入构造一致。
    lifecycle = load_skill_stats()
    provenance_index = _read_json(get_evolution_dir() / ONLINE_PROVENANCE_INDEX, {})
    for judgment in judgments:
        skill = str(judgment.get("name") or judgment.get("skill") or "").strip()
        if not skill:
            continue
        item = stats.setdefault(
            skill,
            {
                "retrieved": 0,
                "relevant": 0,
                "used": 0,
                "last_retrieved": "",
                "last_used": "",
                "source": judgment.get("source", ""),
                "skill_dir": judgment.get("skill_dir", ""),
            },
        )
        item["retrieved"] = int(item.get("retrieved", 0)) + 1
        item["last_retrieved"] = _utc_now()
        item["source"] = judgment.get("source", item.get("source", ""))
        item["skill_dir"] = judgment.get("skill_dir", item.get("skill_dir", ""))
        if judgment.get("relevant"):
            item["relevant"] = int(item.get("relevant", 0)) + 1
        if judgment.get("used"):
            item["used"] = int(item.get("used", 0)) + 1
            item["last_used"] = _utc_now()
        item["last_reason"] = _preview(judgment.get("reason", ""), 500)
        item["last_score"] = judgment.get("score", 0)
        live = lifecycle.get(skill) if isinstance(lifecycle, dict) else None
        live = live if isinstance(live, dict) else {}
        if "invocations" in judgment:
            item["invocations"] = int(judgment.get("invocations", 0) or 0)
        else:
            item["invocations"] = int(live.get("invocations", 0) or 0)
        if "self_evolved" in judgment:
            item["self_evolved"] = bool(judgment.get("self_evolved"))
        else:
            item["self_evolved"] = bool(
                (isinstance(provenance_index, dict) and provenance_index.get(skill))
                or str(live.get("created_at") or "")
            )
        if _maybe_prune_stale_skill(skill, item):
            pruned.append(skill)
    _write_json(stats_path, stats)
    _sync_usage_into_provenance(stats)
    return {"ok": True, "judgments": len(judgments), "pruned": pruned}


def _parse_iso_time(value: str) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        from datetime import datetime, timezone

        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def archive_skill_dir(skill_name: str, stats: dict[str, Any], *, reason: str = "unused") -> bool:
    """把 skill 目录搬进归档区；usage prune 与僵尸孵化期清理共用。"""
    if skill_state_read_only():
        return False
    skill_dir = Path(str(stats.get("skill_dir") or ""))
    if not skill_dir.is_dir():
        return False
    if skill_dir.name.startswith("."):
        return False
    archive_root = get_evolution_dir() / "pruned"
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / f"{_safe_skill_slug(skill_name)}-{int(time.time())}"
    try:
        shutil.move(str(skill_dir), str(destination))
    except Exception:
        return False
    event = {
        "event": "prune",
        "reason": reason,
        "time": _utc_now(),
        "skill": skill_name,
        "from": str(skill_dir),
        "to": str(destination),
        "retrieved": stats.get("retrieved", 0),
        "used": stats.get("used", 0),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, event)
    stats["pruned"] = True
    stats["pruned_to"] = str(destination)
    return True


def _persist_pruned_flag(skill_name: str, stats: dict[str, Any]) -> None:
    """把僵尸归档的 pruned 标志粘滞回 skill_usage_stats.json（与 usage prune 同构）。

    usage prune 在 record_skill_usage_judgments 里整体回写该文件；僵尸归档走一次性
    临时 dict，若不回写，下次评测 load_skill_stats 重算会回落 unobserved、反复归档。
    """
    path = get_evolution_dir() / SKILL_USAGE_STATS
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    item = data.get(skill_name)
    if not isinstance(item, dict):
        item = {}
        data[skill_name] = item
    for key in ("retrieved", "relevant", "used", "source", "skill_dir"):
        if key in stats and key not in item:
            item[key] = stats[key]
    item["pruned"] = True
    if stats.get("pruned_to"):
        item["pruned_to"] = stats["pruned_to"]
    item["prune_reason"] = str(stats.get("prune_reason") or "never-retrieved")
    _write_json(path, data)
    _sync_usage_into_provenance(data)


def _prune_source_allowed(source: object) -> bool:
    """归档来源口径（usage prune 与僵尸门共用）：user 级与 project 级默认纳入判定。

    统一口径前两门漂移：僵尸门已纳 project、usage prune 只纳 user，导致 project
    自进化 skill 的 retrieved∈[3,39] 且 used==0 落在两门之间永不清理。其余来源仍
    仅在显式 env 开关（EVOHARNESS_SKILL_PRUNE_OTHER_SOURCES；旧名
    EVOHARNESS_SKILL_PRUNE_PROJECT 作兼容别名识别，实际 gate 的正是"非默认来源"）
    下纳入——project 早已默认纳入，旧名易误读为"只 gate project"。
    """
    normalized = str(source or "").strip().lower()
    if normalized in {"user", "project"}:
        return True
    for key in ("EVOHARNESS_SKILL_PRUNE_OTHER_SOURCES", "EVOHARNESS_SKILL_PRUNE_PROJECT"):
        if os.environ.get(key, "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def maybe_archive_never_retrieved_skill(skill_name: str, stats: dict[str, Any], *, first_seen: str = "") -> bool:
    """对称清尸：沉淀后长期从未被真正使用的 skill 也归档。

    与 usage prune 互补：那边清"被检索很多次但从没用过"的，这边兜住"检索很少
    （0/1/2 次）且从未被使用（used==0 且 relevant==0）"的新滞留僵尸——两种死法
    都清，僵尸孵化期不再无限挂起。仅 retrieved==0 不足以覆盖：retrieved 为 1/2
    但从未被真正使用的 skill 因 retrieved>0 恒 incubating，原先永不被清理。
    """
    zombie_days = _parse_int(os.environ.get("EVOHARNESS_SKILL_ZOMBIE_DAYS"), 14)
    # 检索次数上限默认 2：更大量检索的"未使用"skill 由 usage prune 负责兜底，
    # 此处只兜少量检索却从未被使用的情形；确需放宽仍可用环境变量覆盖。
    max_retrieved = _parse_int(os.environ.get("EVOHARNESS_SKILL_ZOMBIE_MAX_RETRIEVED"), 2)
    source = str(stats.get("source") or "").strip().lower()
    if not _prune_source_allowed(source):
        return False
    # 人工 /<skill> 手动调用过（invocations>0）即视为被真实使用过，绝不归档——只看
    # retrieved/used/relevant 会误归档"从未被检索但被手动调用"的 user-invocable skill。
    if int(stats.get("invocations", 0) or 0) > 0:
        return False
    # 仅归档自进化产出：调用方以 self_evolved=False 标记手写 skill（project 级与
    # user 级手写同理），避免僵尸门把"无 provenance / 无 create 事件"的手写 skill
    # 误当僵尸清理——原实现仅保护 project 源，user 级手写仍会被误归档。
    if stats.get("self_evolved") is False:
        return False
    if int(stats.get("retrieved", 0) or 0) > max_retrieved:
        return False
    # 归档条件 = "从未被使用"：used/relevant 均为 0（retrieved 0/1/2 都算僵尸）。
    if int(stats.get("used", 0) or 0) > 0 or int(stats.get("relevant", 0) or 0) > 0:
        return False
    first_ts = _parse_iso_time(first_seen)
    if first_ts <= 0 or (time.time() - first_ts) < zombie_days * 86400:
        return False
    moved = archive_skill_dir(skill_name, stats, reason="never-retrieved")
    if moved:
        stats["prune_reason"] = "never-retrieved"
        _persist_pruned_flag(skill_name, stats)
    return moved


def record_usage_samples(
    judgments: list[dict[str, Any]],
    *,
    messages: list[dict[str, Any]],
    latest_user: str,
    latest_assistant: str,
) -> dict[str, Any]:
    """按检索落使用窗口样本（成绩组考卷）：relevant/used 是标注，不是入组门槛。

    被检索到就记一条当轮对话——包括"被检索但模型没用"的轮次，那类样本是
    真实的低分答卷，让通过率惩罚"检索得到却不被采纳"的 skill。
    每进程每天每 skill 最多 EVOHARNESS_USAGE_SAMPLE_DAY_LIMIT 条（默认 4），
    防高频 skill 灌爆样本池；文件超 20MB 停写兜底。
    """
    limit = _parse_int(os.environ.get("EVOHARNESS_USAGE_SAMPLE_DAY_LIMIT"), 4)
    day = _today()
    path = get_evolution_dir() / USAGE_SAMPLES
    try:
        if path.is_file() and path.stat().st_size > 20 * 1024 * 1024:
            return {"ok": False, "error": "usage sample log too large"}
        recorded = 0
        for judgment in judgments:
            name = str(judgment.get("name") or judgment.get("skill") or "").strip()
            if not name:
                continue
            key = f"{day}|{name}"
            if _usage_sample_day_counts.get(key, 0) >= max(1, limit):
                continue
            _usage_sample_day_counts[key] = _usage_sample_day_counts.get(key, 0) + 1
            _append_jsonl(
                path,
                {
                    "skill": name,
                    "time": _utc_now(),
                    "source_type": "usage",
                    "latest_user": str(latest_user or ""),
                    "latest_assistant": str(latest_assistant or ""),
                    "messages": _compact_messages(messages),
                    "relevant": bool(judgment.get("relevant")),
                    "used": bool(judgment.get("used")),
                    "score": float(judgment.get("score", 0.0) or 0.0),
                },
            )
            recorded += 1
        return {"ok": True, "recorded": recorded}
    except Exception:
        return {"ok": False, "error": "usage sample write failed"}


def _maybe_prune_stale_skill(skill_name: str, stats: dict[str, Any]) -> bool:
    min_retrieved = _parse_int(os.environ.get("EVOHARNESS_SKILL_USAGE_PRUNE_MIN_RETRIEVED"), 40)
    max_used = _parse_int(os.environ.get("EVOHARNESS_SKILL_USAGE_PRUNE_MAX_USED"), 0)
    # 来源口径与僵尸门统一：project 级自进化 skill 同样纳入（原先只纳 user，project
    # 僵尸落在两门之间永不清理）。
    if not _prune_source_allowed(stats.get("source")):
        return False
    # 与僵尸门对称：手动 /<skill> 调用过（invocations>0）即算被真实使用，绝不归档。
    if int(stats.get("invocations", 0) or 0) > 0:
        return False
    # 与僵尸门对称：仅归档自进化产出，手写 skill（self_evolved=False）不归档。
    if stats.get("self_evolved") is False:
        return False
    if int(stats.get("retrieved", 0)) < min_retrieved:
        return False
    if int(stats.get("used", 0)) > max_used:
        return False
    return archive_skill_dir(skill_name, stats, reason="unused")


def _sync_usage_into_provenance(stats: dict[str, Any]) -> None:
    path = get_evolution_dir() / ONLINE_PROVENANCE_INDEX
    index = _read_json(path, {})
    if not isinstance(index, dict):
        return
    changed = False
    for skill, usage in stats.items():
        if skill in index:
            index[skill]["usage"] = usage
            changed = True
    if changed:
        _write_json(path, index)


def _file_signature(path: Path) -> tuple[int, int, str]:
    try:
        st = path.stat()
        raw = path.read_bytes()
    except OSError:
        return (0, 0, "")
    return (st.st_mtime_ns, st.st_size, hashlib.sha1(raw).hexdigest())


# usage.jsonl + history/*.jsonl 的聚合（load_skill_stats 的昂贵部分）按源文件
# mtime+size+content-hash 做进程内缓存；skill_usage_stats.json 小文件不参与签名（本模块每轮都会
# 重写它，纳入会使缓存轮轮失效），每次调用仍新鲜叠加。
_usage_index_cache_key: tuple[Any, ...] | None = None
_usage_index_cache: dict[str, dict[str, Any]] | None = None


def _usage_index_key() -> tuple[Any, ...]:
    root = get_evolution_dir()
    history_root = root / HISTORY_DIR
    history_sig: list[tuple[str, int, int, str]] = []
    if history_root.is_dir():
        for path in sorted(history_root.glob("*.jsonl")):
            history_sig.append((path.name, *_file_signature(path)))
    return (str(root), _file_signature(root / USAGE_LOG), tuple(history_sig))


def _load_usage_index() -> dict[str, dict[str, Any]]:
    """usage.jsonl 事件聚合 + history/ 快照计数（昂贵部分，按 mtime+size+content-hash 缓存）。

    usage-tracking 热路径（record_skill_usage_judgments）每轮都会取一次本视图；大日志
    下全量重读代价明显。任一源文件被追加/重写（含本模块的 evolve 快照与 invoke 记录）
    都会改变签名使缓存失效，故聚合字段（invocations/created_at/snapshots）始终反映最新
    落盘；返回的是副本，调用方 mutate 不会污染缓存。
    """
    global _usage_index_cache_key, _usage_index_cache
    key = _usage_index_key()
    if _usage_index_cache_key == key and _usage_index_cache is not None:
        return {name: dict(item) for name, item in _usage_index_cache.items()}
    stats: dict[str, dict[str, Any]] = {}
    usage_path = get_evolution_dir() / USAGE_LOG
    if usage_path.is_file():
        for line in usage_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            skill = str(row.get("skill") or "").strip()
            if not skill:
                continue
            item = stats.setdefault(skill, {"created": 0, "invocations": 0, "feedback": 0, "evolutions": 0})
            event = row.get("event")
            if event == "create":
                item["created"] = int(item.get("created", 0)) + 1
                item["created_at"] = row.get("time")
                item["file"] = row.get("file")
            elif event == "invoke":
                item["invocations"] = int(item.get("invocations", 0)) + 1
                item["last_invoked"] = row.get("time")
            elif event == "feedback":
                item["feedback"] = int(item.get("feedback", 0)) + 1
                item["last_feedback"] = row.get("time")
            elif event == "evolve":
                item["evolutions"] = int(item.get("evolutions", 0)) + 1
                item["last_evolved"] = row.get("time")
                item["version"] = row.get("version")
                item["file"] = row.get("file")

    history_root = get_evolution_dir() / HISTORY_DIR
    if history_root.is_dir():
        for path in history_root.glob("*.jsonl"):
            count = len([line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()])
            skill = path.stem
            item = stats.setdefault(skill, {"created": 0, "invocations": 0, "feedback": 0, "evolutions": 0})
            item["snapshots"] = count
    _usage_index_cache_key = key
    _usage_index_cache = {name: dict(item) for name, item in stats.items()}
    return stats


def load_skill_stats() -> dict[str, dict[str, Any]]:
    stats = _load_usage_index()
    usage_stats = _read_json(get_evolution_dir() / SKILL_USAGE_STATS, {})
    if isinstance(usage_stats, dict):
        for skill, usage in usage_stats.items():
            if not isinstance(usage, dict):
                continue
            item = stats.setdefault(str(skill), {"created": 0, "invocations": 0, "feedback": 0, "evolutions": 0})
            item["retrieved"] = usage.get("retrieved", 0)
            item["relevant"] = usage.get("relevant", 0)
            item["used"] = usage.get("used", 0)
            item["last_retrieved"] = usage.get("last_retrieved", "")
            item["last_used"] = usage.get("last_used", "")
            item["pruned"] = usage.get("pruned", False)
    return stats


def format_skill_stats() -> str:
    stats = load_skill_stats()
    if not stats:
        return "No skill evolution events recorded yet."

    lines = ["Skill evolution stats:"]
    for name in sorted(stats):
        item = stats[name]
        parts = [
            f"created={item.get('created', 0)}",
            f"invoked={item.get('invocations', 0)}",
            f"feedback={item.get('feedback', 0)}",
            f"evolved={item.get('evolutions', 0)}",
            f"snapshots={item.get('snapshots', 0)}",
            f"retrieved={item.get('retrieved', 0)}",
            f"used={item.get('used', 0)}",
        ]
        if item.get("created_at"):
            parts.append(f"created_at={item['created_at']}")
        if item.get("version"):
            parts.append(f"version={item['version']}")
        if item.get("last_invoked"):
            parts.append(f"last_invoked={item['last_invoked']}")
        if item.get("last_used"):
            parts.append(f"last_used={item['last_used']}")
        if item.get("pruned"):
            parts.append("pruned=true")
        lines.append(f"  {name}: " + ", ".join(parts))
    return "\n".join(lines)
