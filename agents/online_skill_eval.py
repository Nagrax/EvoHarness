from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .skill_evolution import (
    ONLINE_PROVENANCE_INDEX,
    ONLINE_PROVENANCE_LOG,
    SKILL_USAGE_STATS,
    get_evolution_dir,
    load_skill_stats,
    maybe_archive_never_retrieved_skill,
)


DEFAULT_MIN_REPLAY_SAMPLES = 2
DEFAULT_MIN_PROMOTION_TESTS = 1
DEFAULT_MIN_RETRIEVED = 5
DEFAULT_MIN_USED_RATE = 0.2
DEFAULT_MIN_RELEVANCE_RATE = 0.35
DEFAULT_MIN_RULE_PASS_RATE = 0.8
DEFAULT_DEV_SPLIT_RATIO = 0.75
DEFAULT_MIN_SCORE_DELTA = 0.01
ONLINE_EVAL_DIR = "online-eval"
SideQuery = Callable[[str, str], Awaitable[str]]

_RE_URL = re.compile(r"https?://\S+")
_RE_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\((https?://[^)]+)\)")
_RE_SOURCE_LABEL = re.compile(r"(?im)^\s*(source|sources|reference|references|来源|参考)\s*[:：]")
_RE_JSON_PREFIX = re.compile(r"^\s*[\{\[]")
_RE_CONCLUSION = re.compile(r"(?i)\b(tl;dr|answer|conclusion|bottom line)\b|结论|先说结论")
_RE_PARAGRAPH_LIMIT = re.compile(r"(不超过|少于|最多|within|less than|at most)\s*(\d+)\s*(段|paragraph)")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _ratio(numerator: int | float, denominator: int | float) -> float:
    denominator = float(denominator or 0)
    if denominator <= 0:
        return 0.0
    return round(float(numerator or 0) / denominator, 4)


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _stable_hash(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _lineage_id_for_skill(skill_name: str) -> str:
    return "skill-" + _stable_hash({"name": str(skill_name or "").strip()})[:16]


def _online_eval_root() -> Path:
    return get_evolution_dir() / ONLINE_EVAL_DIR


def _lineage_dataset_dir(lineage_id: str) -> Path:
    return _online_eval_root() / "datasets" / lineage_id


def _lineage_eval_dir(lineage_id: str) -> Path:
    return _online_eval_root() / "evals" / lineage_id


def _lineage_run_dir(lineage_id: str, run_id: str) -> Path:
    return _online_eval_root() / "runs" / lineage_id / run_id


def _champions_registry_path() -> Path:
    return _online_eval_root() / "champions.json"


def _lineage_champion_dir(lineage_id: str) -> Path:
    return _online_eval_root() / "champions" / lineage_id


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _action_bucket(action: str) -> str:
    raw = str(action or "none").strip().lower() or "none"
    if raw.endswith("_denied"):
        return "denied"
    if raw in {"add", "merge", "revise", "discard", "none", "failed"}:
        return raw
    return "other"


def _latest_message(messages: list[dict[str, Any]], role: str) -> str:
    wanted = str(role or "").strip().lower()
    for item in reversed(list(messages or [])):
        if str(item.get("role") or "").strip().lower() == wanted:
            return str(item.get("content") or "").strip()
    return ""


def _normalized_messages(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            out.append({"role": role, "content": content})
    return out


def _compile_eval_rules(skill: dict[str, Any], *, include_llm_rules: bool = False) -> list[dict[str, Any]]:
    corpus = "\n".join(
        [
            str(skill.get("name") or ""),
            str(skill.get("description") or ""),
            str(skill.get("when_to_use") or ""),
            str(skill.get("instructions") or ""),
            " ".join(str(tag) for tag in skill.get("tags", []) if str(tag).strip()),
        ]
    )
    low = _normalize_text(corpus)
    rules: list[dict[str, Any]] = [
        {
            "rule_id": "response_nonempty",
            "label": "Non-empty response",
            "kind": "programmatic",
            "hard": True,
            "params": {"mode": "nonempty"},
            "provenance": {"source": "baseline"},
        }
    ]

    skill_requirement = _skill_alignment_requirement(skill)
    if include_llm_rules and skill_requirement:
        rules.append(
            {
                "rule_id": "skill_instruction_alignment",
                "label": "Follows skill instructions",
                "kind": "llm_binary",
                "hard": False,
                "params": {
                    "mode": "requirement",
                    "requirement_text": skill_requirement,
                },
                "provenance": {"source": "skill_text"},
            }
        )

    if any(key in low for key in ("引用来源", "标注来源", "注明来源", "cite sources", "with sources", "provide sources", "source-backed")):
        rules.append(
            {
                "rule_id": "must_cite_sources",
                "label": "Cite sources",
                "kind": "programmatic",
                "hard": True,
                "params": {"mode": "mentions_sources"},
                "provenance": {"source": "skill_text"},
            }
        )

    para_limit = _paragraph_limit(corpus)
    if para_limit:
        rules.append(
            {
                "rule_id": "paragraph_limit",
                "label": f"At most {para_limit} paragraphs",
                "kind": "programmatic",
                "hard": True,
                "params": {"mode": "max_paragraphs", "max_paragraphs": para_limit},
                "provenance": {"source": "skill_text"},
            }
        )

    if any(key in low for key in ("先给结论", "结论在前", "先说结论", "answer first", "lead with the conclusion", "bottom line first")):
        rules.append(
            {
                "rule_id": "lead_with_conclusion",
                "label": "Lead with conclusion",
                "kind": "programmatic",
                "hard": False,
                "params": {"mode": "lead_with_conclusion"},
                "provenance": {"source": "skill_text"},
            }
        )

    if "json" in low or "结构化输出" in low:
        rules.append(
            {
                "rule_id": "json_parseable",
                "label": "Valid JSON output",
                "kind": "programmatic",
                "hard": True,
                "params": {"mode": "json_parseable"},
                "provenance": {"source": "skill_text"},
            }
        )

    if "markdown table" in low or "表格" in low:
        rules.append(
            {
                "rule_id": "markdown_table",
                "label": "Markdown table present",
                "kind": "programmatic",
                "hard": False,
                "params": {"mode": "markdown_table"},
                "provenance": {"source": "skill_text"},
            }
        )

    if include_llm_rules and any(
        key in low
        for key in (
            "不要幻觉",
            "不要编造",
            "不确定就说",
            "don't hallucinate",
            "do not hallucinate",
            "avoid hallucination",
            "if unsure",
        )
    ):
        rules.append(
            {
                "rule_id": "no_unfounded_claims",
                "label": "Avoid unfounded claims",
                "kind": "llm_binary",
                "hard": True,
                "params": {
                    "mode": "requirement",
                    "requirement_text": "Avoid unfounded claims and state uncertainty when needed.",
                },
                "provenance": {"source": "skill_text"},
            }
        )
    elif any(key in low for key in ("不要幻觉", "不要编造", "不确定就说", "do not hallucinate", "avoid hallucination", "if unsure")):
        rules.append(
            {
                "rule_id": "uncertainty_marked",
                "label": "Mark uncertainty",
                "kind": "programmatic",
                "hard": False,
                "params": {"mode": "uncertainty_marked"},
                "provenance": {"source": "skill_text"},
            }
        )

    return rules[:8]


def _skill_alignment_requirement(skill: dict[str, Any], *, max_chars: int = 3200) -> str:
    parts: list[str] = []
    name = str(skill.get("name") or "").strip()
    description = str(skill.get("description") or "").strip()
    when_to_use = str(skill.get("when_to_use") or "").strip()
    instructions = str(skill.get("instructions") or "").strip()
    tags = [str(tag).strip() for tag in skill.get("tags", []) if str(tag).strip()]

    if name:
        parts.append(f"Skill name: {name}")
    if description:
        parts.append(f"Description: {description}")
    if when_to_use:
        parts.append(f"When to use: {when_to_use}")
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    if instructions:
        parts.append("Instructions:\n" + instructions)

    body = "\n\n".join(parts).strip()
    if not body:
        return ""
    if len(body) > max_chars:
        body = body[: max_chars - 80].rstrip() + "\n\n[Truncated to fit judge context.]"
    return (
        "Evaluate whether the assistant response follows this Skill's observable requirements. "
        "Judge only the response quality for the given user message; do not require hidden tool calls or information that is not visible in the response. "
        "Return false when the response clearly violates important instructions, misses the requested style/format, or ignores the Skill's intended behavior.\n\n"
        + body
    )


def _paragraph_limit(text: str) -> int:
    for match in _RE_PARAGRAPH_LIMIT.finditer(str(text or "")):
        try:
            return max(1, int(match.group(2)))
        except Exception:
            continue
    low = _normalize_text(text)
    if "少于 3 段" in low or "不超过 3 段" in low or "3 paragraphs" in low:
        return 3
    return 0


def _evaluate_rule(rule: dict[str, Any], response_text: str) -> dict[str, Any]:
    params = rule.get("params") if isinstance(rule.get("params"), dict) else {}
    mode = str(params.get("mode") or "").strip()
    text = str(response_text or "")
    stripped = text.strip()
    passed = False
    details: dict[str, Any] = {}

    if mode == "nonempty":
        passed = bool(stripped)
        details = {"length": len(stripped)}
    elif mode == "json_parseable":
        if not _RE_JSON_PREFIX.search(stripped):
            details = {"reason": "missing_json_prefix"}
        else:
            try:
                json.loads(stripped)
                passed = True
            except Exception as exc:
                details = {"reason": "json_parse_failed", "error": str(exc)}
    elif mode == "mentions_sources":
        passed = bool(_RE_URL.search(text) or _RE_MARKDOWN_LINK.search(text) or _RE_SOURCE_LABEL.search(text))
        details = {"has_url": bool(_RE_URL.search(text))}
    elif mode == "lead_with_conclusion":
        first_block = stripped.split("\n\n", 1)[0].strip()
        passed = bool(_RE_CONCLUSION.search(first_block)) or len(first_block) <= 100
        details = {"first_block": first_block[:160]}
    elif mode == "max_paragraphs":
        paras = [part.strip() for part in re.split(r"\n\s*\n", stripped) if part.strip()]
        limit = max(1, int(params.get("max_paragraphs", 3) or 3))
        passed = len(paras) <= limit
        details = {"paragraph_count": len(paras), "limit": limit}
    elif mode == "markdown_table":
        lines = [line for line in text.splitlines() if "|" in line]
        passed = len(lines) >= 2 and any("---" in line for line in lines)
        details = {"table_line_count": len(lines)}
    elif mode == "uncertainty_marked":
        uncertainty_terms = ("不确定", "无法确认", "需要验证", "uncertain", "not sure", "cannot verify")
        fabrication_terms = ("可能", "假设", "if", "assuming", "needs verification")
        passed = any(term in stripped.lower() for term in uncertainty_terms + fabrication_terms)
        details = {"heuristic": "uncertainty_marker"}
    else:
        details = {"error": f"unsupported programmatic mode: {mode}"}

    return {
        "rule_id": rule.get("rule_id", ""),
        "label": rule.get("label", ""),
        "hard": bool(rule.get("hard")),
        "passed": bool(passed),
        "details": details,
    }


async def _evaluate_rule_async(
    rule: dict[str, Any],
    response_text: str,
    *,
    sample: dict[str, Any],
    skill_name: str,
    side_query: SideQuery | None,
) -> dict[str, Any]:
    kind = str(rule.get("kind") or "programmatic").strip()
    if kind == "programmatic":
        return _evaluate_rule(rule, response_text)

    if kind != "llm_binary":
        return {
            "rule_id": rule.get("rule_id", ""),
            "label": rule.get("label", ""),
            "hard": bool(rule.get("hard")),
            "passed": False,
            "details": {"error": f"unsupported rule kind: {kind}"},
        }

    requirement = str((rule.get("params") or {}).get("requirement_text") or rule.get("description") or "").strip()
    if not requirement:
        return {
            "rule_id": rule.get("rule_id", ""),
            "label": rule.get("label", ""),
            "hard": bool(rule.get("hard")),
            "passed": False,
            "details": {"error": "missing_requirement_text"},
        }
    if side_query is None:
        return {
            "rule_id": rule.get("rule_id", ""),
            "label": rule.get("label", ""),
            "hard": bool(rule.get("hard")),
            "passed": False,
            "skipped": True,
            "details": {"reason": "judge_llm_not_configured"},
        }

    system = (
        "You are a strict binary evaluator for skill replay results.\n"
        "Output ONLY strict JSON parseable by json.loads.\n"
        'Schema: {"pass": true|false, "reason": "short reason"}\n'
        "Judge only against the requirement provided.\n"
        "Do not write analysis, chain-of-thought, markdown, or any text outside the JSON object.\n"
        "Prefer false if the requirement is not clearly satisfied.\n"
    )
    payload = {
        "requirement": requirement,
        "skill_name": skill_name,
        "latest_user_message": sample.get("latest_user", ""),
        "response": str(response_text or ""),
    }
    raw_judge_response = ""
    try:
        raw_judge_response = await side_query(system, json.dumps(payload, ensure_ascii=False))
        parsed = _parse_json_object(raw_judge_response)
    except Exception as exc:
        parsed = {"pass": False, "reason": f"judge failed: {exc}"}
    reason = str(parsed.get("reason") or "").strip()
    if not reason:
        if not str(raw_judge_response or "").strip():
            reason = "judge returned an empty response"
        elif not parsed:
            reason = "judge returned non-JSON response"
        else:
            reason = "judge returned no reason"
    details = {"reason": reason[:500]}
    if raw_judge_response and not parsed:
        details["raw_response_preview"] = str(raw_judge_response).strip()[:500]
    return {
        "rule_id": rule.get("rule_id", ""),
        "label": rule.get("label", ""),
        "hard": bool(rule.get("hard")),
        "passed": bool(parsed.get("pass", False)),
        "details": details,
    }


def _active_skill_snapshots() -> dict[str, dict[str, Any]]:
    try:
        from .skills import discover_skills
    except Exception:
        return {}

    snapshots: dict[str, dict[str, Any]] = {}
    for skill in discover_skills():
        name = str(getattr(skill, "name", "") or "").strip()
        if not name:
            continue
        snapshots[name] = {
            "name": name,
            "description": str(getattr(skill, "description", "") or ""),
            "when_to_use": str(getattr(skill, "when_to_use", "") or ""),
            "instructions": str(getattr(skill, "prompt_template", "") or ""),
            "source": str(getattr(skill, "source", "") or ""),
            "skill_dir": str(getattr(skill, "skill_dir", "") or ""),
            "context": str(getattr(skill, "context", "") or ""),
        }
    return snapshots


def _rows_by_skill(provenance_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in provenance_rows:
        skill = str(row.get("skill") or "").strip()
        if not skill:
            result = row.get("result") if isinstance(row.get("result"), dict) else {}
            skill = str(result.get("skill") or "").strip()
        if skill:
            grouped.setdefault(skill, []).append(row)
    return grouped


def _build_replay_pool(
    skill_name: str,
    rows: list[dict[str, Any]],
    lineage: dict[str, Any],
    *,
    freeze: bool = True,
) -> list[dict[str, Any]]:
    samples: dict[str, dict[str, Any]] = {}

    def add_sample(source: dict[str, Any], source_kind: str) -> None:
        messages = _normalized_messages(source.get("messages"))
        if not messages or not _latest_message(messages, "user"):
            return
        sample_id = _stable_hash(
            {
                "skill": skill_name,
                "messages": messages,
                "latest_user": _latest_message(messages, "user"),
            }
        )
        samples[sample_id] = {
            "sample_id": sample_id,
            "source_type": source_kind,
            "split": "mutate_dev",
            "time": source.get("time", ""),
            "action": source.get("action", ""),
            "ok": bool(source.get("ok", True)),
            "latest_user": _latest_message(messages, "user"),
            "latest_assistant": _latest_message(messages, "assistant"),
            "messages": messages,
        }

    for row in rows:
        add_sample(row, "online_log")
    for source in lineage.get("sources", []) if isinstance(lineage.get("sources"), list) else []:
        if isinstance(source, dict):
            add_sample(source, "online_index")

    ordered = sorted(samples.values(), key=lambda item: (str(item.get("time") or ""), item["sample_id"]), reverse=True)
    if not freeze:
        return _assign_replay_splits(ordered)
    return _freeze_replay_pool(skill_name=skill_name, samples=ordered)


def _split_score(sample_id: str) -> float:
    raw = str(sample_id or "")
    seed = raw[:8] if len(raw) >= 8 else _stable_hash(raw)[:8]
    try:
        return int(seed, 16) / float(16**8 - 1)
    except Exception:
        return 0.0


def _assign_replay_splits(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(item) for item in samples]
    if len(out) < 2:
        for item in out:
            item["split"] = "mutate_dev"
        return out

    for item in out:
        score = _split_score(str(item.get("sample_id") or ""))
        item["split"] = "mutate_dev" if score < DEFAULT_DEV_SPLIT_RATIO else "promotion_test"

    if not any(item.get("split") == "promotion_test" for item in out):
        out[-1]["split"] = "promotion_test"
    if not any(item.get("split") == "mutate_dev" for item in out):
        out[0]["split"] = "mutate_dev"
    return out


def _freeze_replay_pool(*, skill_name: str, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lineage_id = _lineage_id_for_skill(skill_name)
    path = _lineage_dataset_dir(lineage_id) / "replay_pool.jsonl"
    existing = _read_jsonl(path)
    by_id: dict[str, dict[str, Any]] = {}

    for item in existing:
        sample_id = str(item.get("sample_id") or "").strip()
        if sample_id:
            by_id[sample_id] = dict(item)

    for item in samples:
        sample_id = str(item.get("sample_id") or "").strip()
        if not sample_id:
            continue
        previous = by_id.get(sample_id)
        if previous:
            merged = dict(previous)
            merged.update({key: value for key, value in item.items() if key != "split"})
            by_id[sample_id] = merged
        else:
            by_id[sample_id] = dict(item)

    frozen = sorted(by_id.values(), key=lambda item: (str(item.get("time") or ""), str(item.get("sample_id") or "")), reverse=True)
    frozen = _assign_replay_splits(frozen)
    _write_jsonl(path, frozen)
    return frozen


def _summarize_rule_outcomes(rules: list[dict[str, Any]], samples: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    for sample in samples:
        response = str(sample.get("latest_assistant") or "")
        for rule in rules:
            outcome = _evaluate_rule(rule, response)
            outcome["sample_id"] = sample.get("sample_id", "")
            outcome["split"] = sample.get("split", "")
            outcome["kind"] = rule.get("kind", "programmatic")
            outcome["score"] = (2.0 if outcome.get("hard") else 1.0) if outcome.get("passed") else 0.0
            outcomes.append(outcome)

    return _summarize_outcomes_from_rows(rules=rules, samples=samples, outcomes=outcomes)


async def _summarize_rule_outcomes_async(
    rules: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    *,
    skill_name: str,
    side_query: SideQuery | None,
) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    for sample in samples:
        response = str(sample.get("latest_assistant") or "")
        for rule in rules:
            outcome = await _evaluate_rule_async(
                rule,
                response,
                sample=sample,
                skill_name=skill_name,
                side_query=side_query,
            )
            outcome["sample_id"] = sample.get("sample_id", "")
            outcome["split"] = sample.get("split", "")
            outcome["kind"] = rule.get("kind", "programmatic")
            outcome["score"] = (2.0 if outcome.get("hard") else 1.0) if outcome.get("passed") else 0.0
            outcomes.append(outcome)
    return _summarize_outcomes_from_rows(rules=rules, samples=samples, outcomes=outcomes)


def _summarize_outcomes_from_rows(
    *,
    rules: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    total = len(outcomes)
    passed = sum(1 for item in outcomes if item.get("passed"))
    llm_rules = [rule for rule in rules if str(rule.get("kind") or "programmatic") == "llm_binary"]
    llm_outcomes = [item for item in outcomes if str(item.get("kind") or "programmatic") == "llm_binary"]
    llm_passed = sum(1 for item in llm_outcomes if item.get("passed"))
    total_score = float(sum(float(item.get("score", 0.0) or 0.0) for item in outcomes))
    hard = [item for item in outcomes if item.get("hard")]
    hard_failed = [item for item in hard if not item.get("passed")]
    test = [item for item in outcomes if item.get("split") == "promotion_test"]
    test_passed = sum(1 for item in test if item.get("passed"))
    test_hard_failed = [item for item in test if item.get("hard") and not item.get("passed")]
    by_rule: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        rid = str(outcome.get("rule_id") or "unknown")
        item = by_rule.setdefault(rid, {"rule_id": rid, "passed": 0, "total": 0, "hard": bool(outcome.get("hard"))})
        item["total"] = int(item.get("total", 0)) + 1
        if outcome.get("passed"):
            item["passed"] = int(item.get("passed", 0)) + 1
    for item in by_rule.values():
        item["pass_rate"] = _ratio(int(item.get("passed", 0)), int(item.get("total", 0)))

    return {
        "rules": rules,
        "rule_count": len(rules),
        "programmatic_rule_count": len(rules) - len(llm_rules),
        "llm_rule_count": len(llm_rules),
        "outcome_count": total,
        "llm_outcome_count": len(llm_outcomes),
        "llm_pass_rate": _ratio(llm_passed, len(llm_outcomes)),
        "skipped_outcome_count": sum(1 for item in outcomes if item.get("skipped")),
        "passed_rules": passed,
        "total_score": total_score,
        "average_score": _ratio(total_score, max(1, len(samples))),
        "pass_rate": _ratio(passed, total),
        "hard_failures": len(hard_failed),
        "promotion_test_pass_rate": _ratio(test_passed, len(test)),
        "promotion_test_hard_failures": len(test_hard_failed),
        "by_rule": sorted(by_rule.values(), key=lambda item: str(item.get("rule_id") or "")),
        "failures": [
            {
                "sample_id": item.get("sample_id", ""),
                "split": item.get("split", ""),
                "rule_id": item.get("rule_id", ""),
                "label": item.get("label", ""),
                "hard": item.get("hard", False),
                "details": item.get("details", {}),
            }
            for item in outcomes
            if not item.get("passed") and not item.get("skipped")
        ][:20],
        "outcomes": outcomes,
    }


def _snapshot_with_instructions(snapshot: dict[str, Any], *, instructions: str, label: str) -> dict[str, Any]:
    out = dict(snapshot or {})
    out["instructions"] = str(instructions or "")
    out["mutation_label"] = str(label or "")
    return out


def _append_eval_guards(instructions: str, additions: list[str]) -> str:
    body = str(instructions or "").rstrip()
    clean = [str(item or "").strip() for item in additions if str(item or "").strip()]
    if not clean:
        return body
    marker = "## Online Eval Improvement Guards"
    if marker not in body:
        body = (body + "\n\n" if body else "") + marker + "\n"
    for item in clean:
        line = item if item.startswith("- ") else f"- {item}"
        if line not in body:
            body += line + "\n"
    return body


def _candidate_variant_id(*, lineage_id: str, label: str, snapshot: dict[str, Any]) -> str:
    return f"candidate-{_stable_hash({'lineage_id': lineage_id, 'label': label, 'snapshot': snapshot})[:12]}"


def _rule_by_id(rules: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(rule.get("rule_id") or ""): rule for rule in rules if str(rule.get("rule_id") or "")}


def _build_heuristic_candidate_variants(
    *,
    lineage_id: str,
    snapshot: dict[str, Any],
    rules: list[dict[str, Any]],
    rule_summary: dict[str, Any],
    max_variants: int = 4,
) -> list[dict[str, Any]]:
    rule_lookup = _rule_by_id(rules)
    failures = rule_summary.get("failures") if isinstance(rule_summary.get("failures"), list) else []
    if not failures:
        failures = [{"rule_id": str(rule.get("rule_id") or "")} for rule in rules if str(rule.get("kind") or "") != "programmatic"][:2]
    variants: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    base_instructions = str(snapshot.get("instructions") or "")
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        rid = str(failure.get("rule_id") or "").strip()
        if not rid or rid in seen_labels:
            continue
        rule = rule_lookup.get(rid, {})
        details = failure.get("details") if isinstance(failure.get("details"), dict) else {}
        reason = str(details.get("reason") or details.get("error") or "").strip()
        additions: list[str] = []
        if rid == "must_cite_sources":
            additions.append("Always cite concrete sources or links for factual claims.")
        elif rid == "paragraph_limit":
            limit = int((rule.get("params") or {}).get("max_paragraphs", 3) or 3)
            additions.append(f"Keep the final answer within {limit} short paragraphs.")
        elif rid == "lead_with_conclusion":
            additions.append("Open with a direct conclusion or answer before elaboration.")
        elif rid == "json_parseable":
            additions.append("Output valid JSON only, with no markdown fences or extra commentary.")
        elif rid == "markdown_table":
            additions.append("Include a markdown table when presenting structured comparisons or plans.")
        elif rid == "no_unfounded_claims":
            additions.append("Do not invent facts; if evidence is missing, state uncertainty explicitly.")
        elif rid == "skill_instruction_alignment":
            additions.append("Satisfy the Skill's observable requirements before adding extra clarifications or commentary.")
        else:
            requirement = str((rule.get("params") or {}).get("requirement_text") or "").strip()
            if requirement:
                additions.append(f"Requirement to preserve: {requirement[:500]}")
        if reason:
            additions.append(f"Address prior evaluation failure: {reason[:500]}")
        if not additions:
            continue
        label = f"heuristic:{rid}"
        new_snapshot = _snapshot_with_instructions(
            snapshot,
            instructions=_append_eval_guards(base_instructions, additions),
            label=label,
        )
        if str(new_snapshot.get("instructions") or "").strip() == base_instructions.strip():
            continue
        variants.append(
            {
                "variant_id": _candidate_variant_id(lineage_id=lineage_id, label=label, snapshot=new_snapshot),
                "label": label,
                "mutation_type": "heuristic",
                "parent_variant_id": "current_active",
                "snapshot": new_snapshot,
                "notes": "; ".join(additions)[:1000],
            }
        )
        seen_labels.add(rid)
        if len(variants) >= max(1, int(max_variants)):
            break
    return variants


async def _build_llm_candidate_variant_async(
    *,
    lineage_id: str,
    snapshot: dict[str, Any],
    rules: list[dict[str, Any]],
    rule_summary: dict[str, Any],
    side_query: SideQuery | None,
) -> dict[str, Any] | None:
    if side_query is None:
        return None
    failures = rule_summary.get("failures") if isinstance(rule_summary.get("failures"), list) else []
    if not failures:
        return None
    system = (
        "You improve a local agent Skill for replay evaluation.\n"
        "Output ONLY strict JSON parseable by json.loads.\n"
        'Schema: {"description": "...", "instructions": "...", "notes": "..."}\n'
        "Make a small durable improvement. Do not invent new capabilities. Preserve the same Skill identity.\n"
    )
    payload = {
        "skill": {
            "name": snapshot.get("name", ""),
            "description": snapshot.get("description", ""),
            "when_to_use": snapshot.get("when_to_use", ""),
            "instructions": snapshot.get("instructions", ""),
        },
        "rules": rules[:6],
        "failures": failures[:4],
    }
    try:
        parsed = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    except Exception:
        return None
    instructions = str(parsed.get("instructions") or "").strip()
    if not instructions:
        return None
    label = "llm_mutation"
    new_snapshot = dict(snapshot or {})
    new_snapshot["description"] = str(parsed.get("description") or snapshot.get("description") or "")
    new_snapshot["instructions"] = instructions
    new_snapshot["mutation_label"] = label
    return {
        "variant_id": _candidate_variant_id(lineage_id=lineage_id, label=label, snapshot=new_snapshot),
        "label": label,
        "mutation_type": "llm",
        "parent_variant_id": "current_active",
        "snapshot": new_snapshot,
        "notes": str(parsed.get("notes") or "LLM-guided mutation").strip()[:1000],
    }


async def _generate_variant_response_async(
    *,
    variant: dict[str, Any],
    sample: dict[str, Any],
    side_query: SideQuery | None,
) -> str:
    if side_query is None:
        return ""
    snapshot = variant.get("snapshot") if isinstance(variant.get("snapshot"), dict) else {}
    system = str(snapshot.get("instructions") or snapshot.get("description") or "").strip()
    messages = sample.get("messages") if isinstance(sample.get("messages"), list) else []
    history = "\n\n".join(
        f"[{str(item.get('role') or '').strip()}] {str(item.get('content') or '').strip()}"
        for item in messages
        if isinstance(item, dict) and str(item.get("content") or "").strip()
    )
    user = (
        "Replay the following conversation with the candidate Skill instructions already injected.\n\n"
        f"Conversation:\n{history}\n\n"
        "Respond to the latest user message. Return only the assistant response."
    )
    try:
        return str(await side_query(system, user) or "").strip()
    except Exception as exc:
        return f"[candidate_response_failed: {exc}]"


async def _evaluate_generated_variant_async(
    *,
    variant: dict[str, Any],
    samples: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    side_query: SideQuery | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    snapshot = variant.get("snapshot") if isinstance(variant.get("snapshot"), dict) else {}
    skill_name = str(snapshot.get("name") or variant.get("label") or "")
    for sample in samples:
        response = await _generate_variant_response_async(variant=variant, sample=sample, side_query=side_query)
        outputs.append(
            {
                "sample_id": sample.get("sample_id", ""),
                "variant_id": variant.get("variant_id", ""),
                "split": sample.get("split", ""),
                "source_type": sample.get("source_type", ""),
                "latest_user": sample.get("latest_user", ""),
                "response_source": "candidate_generated",
                "response_text": response,
            }
        )
        for rule in rules:
            outcome = await _evaluate_rule_async(
                rule,
                response,
                sample=sample,
                skill_name=skill_name,
                side_query=side_query,
            )
            outcome["sample_id"] = sample.get("sample_id", "")
            outcome["split"] = sample.get("split", "")
            outcome["kind"] = rule.get("kind", "programmatic")
            outcome["variant_id"] = variant.get("variant_id", "")
            outcome["score"] = (2.0 if outcome.get("hard") else 1.0) if outcome.get("passed") else 0.0
            outcomes.append(outcome)
    summary = _summarize_outcomes_from_rows(rules=rules, samples=samples, outcomes=outcomes)
    return outputs, outcomes, summary


def _variant_summary_from_eval(variant: dict[str, Any], summary: dict[str, Any], sample_count: int) -> dict[str, Any]:
    return {
        "variant_id": variant.get("variant_id", ""),
        "label": variant.get("label", ""),
        "mutation_type": variant.get("mutation_type", ""),
        "parent_variant_id": variant.get("parent_variant_id", ""),
        "sample_count": int(sample_count or 0),
        "total_score": float(summary.get("total_score", 0.0) or 0.0),
        "average_score": float(summary.get("average_score", 0.0) or 0.0),
        "hard_failures": int(summary.get("hard_failures", 0) or 0),
        "passed_rules": int(summary.get("passed_rules", 0) or 0),
        "total_rules": int(summary.get("outcome_count", 0) or 0),
        "rule_pass_rate": float(summary.get("pass_rate", 0.0) or 0.0),
        "notes": str(variant.get("notes") or ""),
    }


async def _build_candidate_eval_bundle_async(
    *,
    lineage_id: str,
    snapshot: dict[str, Any],
    replay_pool: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    rule_summary: dict[str, Any],
    side_query: SideQuery | None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if side_query is None or not replay_pool:
        return {}
    # 触发变体：与指令变体并行的另一条轨——改 when_to_use，评测靠检索重放，零模型成本。
    trigger: dict[str, Any] = {}
    try:
        trigger_variant = await _build_trigger_variant_async(
            snapshot=snapshot,
            usage=usage or {},
            sample_count=len(replay_pool),
            side_query=side_query,
        )
    except Exception:
        trigger_variant = None
    if trigger_variant is not None:
        try:
            from .skills import build_retrieval_corpus

            baseline_corpus = build_retrieval_corpus()
            if str(snapshot.get("name") or "") in baseline_corpus:
                metrics = _evaluate_trigger_variant(
                    skill_name=str(snapshot.get("name") or ""),
                    variant=trigger_variant,
                    samples=replay_pool,
                    baseline_corpus=baseline_corpus,
                )
                if metrics.get("evaluated"):
                    trigger = {"variant": trigger_variant, "metrics": metrics}
        except Exception:
            pass
    variants = _build_heuristic_candidate_variants(
        lineage_id=lineage_id,
        snapshot=snapshot,
        rules=rules,
        rule_summary=rule_summary,
    )
    llm_variant = await _build_llm_candidate_variant_async(
        lineage_id=lineage_id,
        snapshot=snapshot,
        rules=rules,
        rule_summary=rule_summary,
        side_query=side_query,
    )
    if llm_variant is not None:
        variants.append(llm_variant)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for variant in variants:
        key = _stable_hash((variant.get("snapshot") or {}).get("instructions", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(variant)
    dev_samples = [sample for sample in replay_pool if sample.get("split") == "mutate_dev"] or list(replay_pool)
    test_samples = [sample for sample in replay_pool if sample.get("split") == "promotion_test"]
    outputs: list[dict[str, Any]] = []
    judgments: list[dict[str, Any]] = []
    scored: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for variant in deduped[:4]:
        variant_outputs, variant_outcomes, variant_summary = await _evaluate_generated_variant_async(
            variant=variant,
            samples=dev_samples,
            rules=rules,
            side_query=side_query,
        )
        outputs.extend(variant_outputs)
        judgments.extend(variant_outcomes)
        scored.append((variant, variant_summary, _variant_summary_from_eval(variant, variant_summary, len(dev_samples))))
    if not scored:
        if not trigger:
            return {}
        return {
            "candidate_variants": [],
            "variant_summaries": [],
            "best_variant": {},
            "best_dev_summary": {},
            "best_test_summary": {},
            "outputs": outputs,
            "judgments": judgments,
            "trigger": trigger,
        }
    scored.sort(
        key=lambda item: (
            float(item[1].get("average_score", 0.0) or 0.0),
            -int(item[1].get("hard_failures", 0) or 0),
        ),
        reverse=True,
    )
    best_variant, best_dev_eval, best_dev_summary = scored[0]
    best_test_summary: dict[str, Any] = {}
    if test_samples:
        test_outputs, test_outcomes, test_eval = await _evaluate_generated_variant_async(
            variant=best_variant,
            samples=test_samples,
            rules=rules,
            side_query=side_query,
        )
        outputs.extend(test_outputs)
        judgments.extend(test_outcomes)
        best_test_summary = _variant_summary_from_eval(best_variant, test_eval, len(test_samples))
    return {
        "candidate_variants": deduped[:4],
        "variant_summaries": [item[2] for item in scored],
        "best_variant": best_variant,
        "best_dev_summary": best_dev_summary,
        "best_test_summary": best_test_summary,
        "outputs": outputs,
        "judgments": judgments,
        "trigger": trigger,
    }


async def _build_trigger_variant_async(
    *,
    snapshot: dict[str, Any],
    usage: dict[str, Any],
    sample_count: int,
    side_query: SideQuery | None,
) -> dict[str, Any] | None:
    """根据使用信号改写 when_to_use，生成触发变体。

    信号判读：被检索不少但相关率低 = 触发条件写太宽；样本不少但几乎没被检索
    = 写太窄。只有出现明确偏向时才生成，避免无信号瞎改。
    """
    if side_query is None:
        return None
    retrieved = int(usage.get("retrieved", 0) or 0)
    relevant = int(usage.get("relevant", 0) or 0)
    direction = ""
    if retrieved >= 3 and relevant / max(1, retrieved) < 0.5:
        direction = "too_broad"
    elif sample_count >= 3 and retrieved < 3:
        direction = "too_narrow"
    if not direction:
        return None
    system = (
        "You improve a Skill's retrieval trigger text (when-to-use).\n"
        "Output ONLY strict JSON parseable by json.loads.\n"
        'Schema: {"when_to_use": "...", "notes": "..."}\n'
        "Rewrite the when-to-use so retrieval matches the right requests. Keep the same language as the original. "
        "Do not change the skill's capability.\n"
    )
    payload = {
        "direction": direction,
        "hint": (
            "Retrieved often but rarely relevant: make the trigger narrower and more specific."
            if direction == "too_broad"
            else "Rarely retrieved despite relevant history: make the trigger broader, add synonyms and concrete cues."
        ),
        "skill": {
            "name": snapshot.get("name", ""),
            "description": snapshot.get("description", ""),
            "when_to_use": snapshot.get("when_to_use", ""),
        },
        "usage": {"retrieved": retrieved, "relevant": relevant, "used": int(usage.get("used", 0) or 0)},
    }
    try:
        parsed = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    except Exception:
        return None
    when_to_use = str(parsed.get("when_to_use") or "").strip()
    if not when_to_use or when_to_use == str(snapshot.get("when_to_use") or "").strip():
        return None
    new_snapshot = dict(snapshot or {})
    new_snapshot["when_to_use"] = when_to_use
    new_snapshot["mutation_label"] = "trigger_rewrite"
    return {
        "variant_id": f"trigger-{_stable_hash({'name': snapshot.get('name', ''), 'when_to_use': when_to_use})[:12]}",
        "label": f"trigger:{direction}",
        "mutation_type": "trigger",
        "parent_variant_id": "current_active",
        "snapshot": new_snapshot,
        "notes": str(parsed.get("notes") or direction)[:1000],
    }


def _evaluate_trigger_variant(
    *,
    skill_name: str,
    variant: dict[str, Any],
    samples: list[dict[str, Any]],
    baseline_corpus: dict[str, list[str]],
) -> dict[str, Any]:
    """纯程序化的检索重放：同一批历史 query 重跑 BM25，对比目标 skill 的命中与排名。

    与指令变体的 LLM 评测不同，这一轨零模型成本——检索质量的考卷就是检索本身。
    """
    from .skills import rank_corpus_for_query, skill_terms_from_text

    queries = [str(s.get("latest_user") or "") for s in samples if str(s.get("latest_user") or "").strip()][:50]
    if not queries:
        return {"evaluated": False}

    def _metrics(corpus: dict[str, list[str]]) -> dict[str, float]:
        ranks: list[float] = []
        hits_at_3 = 0
        for query in queries:
            ranking = [name for name, _ in rank_corpus_for_query(query, corpus)]
            if skill_name in ranking:
                rank = ranking.index(skill_name) + 1
                ranks.append(float(rank))
                if rank <= 3:
                    hits_at_3 += 1
            else:
                ranks.append(float(len(corpus) + 1))
        return {"hit_at_3": _ratio(hits_at_3, len(queries)), "mean_rank": round(sum(ranks) / max(1, len(ranks)), 4)}

    snapshot = variant.get("snapshot") if isinstance(variant.get("snapshot"), dict) else {}
    variant_corpus = dict(baseline_corpus)
    variant_terms = skill_terms_from_text(
        snapshot.get("name", skill_name),
        snapshot.get("description", ""),
        snapshot.get("when_to_use", ""),
        snapshot.get("instructions", ""),
    )
    if skill_name in variant_corpus:
        variant_corpus[skill_name] = variant_terms
    baseline = _metrics(baseline_corpus)
    variant_metrics = _metrics(variant_corpus)
    improved = bool(
        variant_metrics.get("hit_at_3", 0.0) > baseline.get("hit_at_3", 0.0)
        or (
            variant_metrics.get("hit_at_3", 0.0) == baseline.get("hit_at_3", 0.0)
            and variant_metrics.get("mean_rank", 1e9) < baseline.get("mean_rank", 1e9)
        )
    )
    return {
        "evaluated": True,
        "query_count": len(queries),
        "baseline": baseline,
        "variant": variant_metrics,
        "improved": improved,
    }


def _skill_status(
    *,
    replay_count: int,
    promotion_test_count: int,
    retrieved: int,
    relevant: int,
    used: int,
    pruned: bool,
    rule_summary: dict[str, Any],
    min_replay_samples: int,
    min_promotion_tests: int,
    min_retrieved: int,
    min_used_rate: float,
    min_relevance_rate: float,
    min_rule_pass_rate: float,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if pruned:
        return "pruned", ["skill has been archived by usage pruning"]
    if replay_count <= 0 and retrieved <= 0:
        return "unobserved", ["no online replay or usage signal yet"]
    if replay_count < min_replay_samples:
        reasons.append(f"only {replay_count} replay sample(s)")
    if promotion_test_count < min_promotion_tests:
        reasons.append(f"only {promotion_test_count} promotion-test sample(s)")
    if retrieved < min_retrieved:
        reasons.append(f"only {retrieved} retrieval judgment(s)")
    if reasons:
        return "incubating", reasons

    relevance_rate = _ratio(relevant, retrieved)
    used_rate = _ratio(used, retrieved)
    pass_rate = float(rule_summary.get("pass_rate", 0.0) or 0.0)
    test_hard_failures = int(rule_summary.get("promotion_test_hard_failures", 0) or 0)
    hard_failures = int(rule_summary.get("hard_failures", 0) or 0)

    if test_hard_failures > 0:
        reasons.append(f"{test_hard_failures} promotion-test hard rule failure(s)")
    elif hard_failures > 0:
        reasons.append(f"{hard_failures} hard rule failure(s)")
    if pass_rate < min_rule_pass_rate:
        reasons.append(f"low replay rule pass rate {_pct(pass_rate)}")
    if relevance_rate < min_relevance_rate:
        reasons.append(f"low relevance rate {_pct(relevance_rate)}")
    if used_rate < min_used_rate:
        reasons.append(f"low used rate {_pct(used_rate)}")
    if reasons:
        return "watch", reasons
    return "healthy", ["passes replay rules and usage gates"]


def _run_id(lineage_id: str) -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{str(lineage_id or '')[-6:]}"


def _variant_summary(
    *,
    skill_name: str,
    snapshot: dict[str, Any],
    rule_summary: dict[str, Any],
    replay_pool: list[dict[str, Any]],
) -> dict[str, Any]:
    version = str(snapshot.get("version") or snapshot.get("current_version") or "active").strip() or "active"
    return {
        "variant_id": f"current-{_stable_hash({'skill': skill_name, 'version': version})[:10]}",
        "label": "current_active",
        "mutation_type": "active",
        "skill": skill_name,
        "version": version,
        "sample_count": len(replay_pool),
        "total_score": float(rule_summary.get("total_score", 0.0) or 0.0),
        "average_score": float(rule_summary.get("average_score", 0.0) or 0.0),
        "hard_failures": int(rule_summary.get("hard_failures", 0) or 0),
        "passed_rules": int(rule_summary.get("passed_rules", 0) or 0),
        "total_rules": int(rule_summary.get("outcome_count", 0) or 0),
        "rule_pass_rate": float(rule_summary.get("pass_rate", 0.0) or 0.0),
    }


def _load_champion(lineage_id: str) -> dict[str, Any]:
    registry = _read_json(_champions_registry_path(), {"champions": {}})
    if not isinstance(registry, dict):
        return {}
    champions = registry.get("champions") if isinstance(registry.get("champions"), dict) else {}
    item = champions.get(lineage_id)
    return dict(item or {}) if isinstance(item, dict) else {}


def _set_champion(lineage_id: str, payload: dict[str, Any]) -> None:
    path = _champions_registry_path()
    registry = _read_json(path, {"version": 1, "champions": {}})
    if not isinstance(registry, dict):
        registry = {"version": 1, "champions": {}}
    champions = registry.setdefault("champions", {})
    if not isinstance(champions, dict):
        champions = {}
        registry["champions"] = champions
    champions[lineage_id] = payload
    _write_json(path, registry)
    champion_dir = _lineage_champion_dir(lineage_id)
    _write_json(champion_dir / "champion.json", payload)
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    if snapshot:
        _write_champion_skill_file(champion_dir / "SKILL.md", snapshot)


def _write_champion_skill_file(path: Path, snapshot: dict[str, Any]) -> None:
    name = str(snapshot.get("name") or "unnamed-skill").strip()
    description = str(snapshot.get("description") or "").strip()
    when_to_use = str(snapshot.get("when_to_use") or "").strip()
    instructions = str(snapshot.get("instructions") or "").strip()
    lines = ["---", f"name: {name}"]
    if description:
        lines.append(f"description: {description}")
    if when_to_use:
        lines.append(f"when-to-use: {when_to_use}")
    lines.extend(["---", "", instructions])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _promotion_decision(
    *,
    status: str,
    candidate: dict[str, Any],
    champion: dict[str, Any],
    min_score_delta: float = DEFAULT_MIN_SCORE_DELTA,
) -> dict[str, Any]:
    if status in {"unobserved", "incubating", "pruned"}:
        return {
            "promoted": False,
            "status": status,
            "reason": "not enough usable replay and usage signal for champion promotion",
            "champion_before": champion,
            "candidate": candidate,
            "min_score_delta": min_score_delta,
        }
    if status == "watch":
        return {
            "promoted": False,
            "status": "rejected",
            "reason": "candidate is under watch",
            "champion_before": champion,
            "candidate": candidate,
            "min_score_delta": min_score_delta,
        }

    previous_summary = champion.get("summary") if isinstance(champion.get("summary"), dict) else {}
    if not previous_summary:
        return {
            "promoted": True,
            "status": "active_champion",
            "reason": "first healthy candidate for this lineage",
            "champion_before": {},
            "candidate": candidate,
            "min_score_delta": min_score_delta,
        }

    candidate_score = float(candidate.get("average_score", 0.0) or 0.0)
    champion_score = float(previous_summary.get("average_score", 0.0) or 0.0)
    candidate_hard = int(candidate.get("hard_failures", 0) or 0)
    champion_hard = int(previous_summary.get("hard_failures", 0) or 0)
    promoted = bool(
        candidate_score >= champion_score + float(min_score_delta)
        and candidate_hard <= champion_hard
    )
    return {
        "promoted": promoted,
        "status": "active_champion" if promoted else "rejected",
        "reason": (
            "candidate beats current champion on average score without more hard failures"
            if promoted
            else "candidate does not beat current champion promotion gate"
        ),
        "champion_before": champion,
        "candidate": candidate,
        "min_score_delta": min_score_delta,
    }


def _persist_eval_artifacts(
    *,
    skill_name: str,
    snapshot: dict[str, Any],
    replay_pool: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    rule_summary: dict[str, Any],
    status: str,
    reasons: list[str],
    candidate_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lineage_id = _lineage_id_for_skill(skill_name)
    eval_dir = _lineage_eval_dir(lineage_id)
    _write_json(
        eval_dir / "eval_spec.json",
        {
            "lineage_id": lineage_id,
            "skill": skill_name,
            "rules": rules,
            "response_source": "history_latest_assistant",
        },
    )

    run_id = _run_id(lineage_id)
    run_dir = _lineage_run_dir(lineage_id, run_id)
    outputs = [
        {
            "sample_id": sample.get("sample_id", ""),
            "variant_id": "current_active",
            "split": sample.get("split", ""),
            "source_type": sample.get("source_type", ""),
            "latest_user": sample.get("latest_user", ""),
            "response_source": "history_latest_assistant",
            "response_text": sample.get("latest_assistant", ""),
        }
        for sample in replay_pool
    ]
    bundle = candidate_bundle if isinstance(candidate_bundle, dict) else {}
    outputs.extend(list(bundle.get("outputs") or []))
    judgments = [
        {
            "sample_id": outcome.get("sample_id", ""),
            "variant_id": "current_active",
            "split": outcome.get("split", ""),
            "rule_id": outcome.get("rule_id", ""),
            "label": outcome.get("label", ""),
            "kind": outcome.get("kind", "programmatic"),
            "hard": bool(outcome.get("hard")),
            "passed": bool(outcome.get("passed")),
            "score": float(outcome.get("score", 0.0) or 0.0),
            "details": outcome.get("details", {}),
        }
        for outcome in list(rule_summary.get("outcomes") or [])
        if isinstance(outcome, dict)
    ]
    for outcome in list(bundle.get("judgments") or []):
        if not isinstance(outcome, dict):
            continue
        judgments.append(
            {
                "sample_id": outcome.get("sample_id", ""),
                "variant_id": outcome.get("variant_id", ""),
                "split": outcome.get("split", ""),
                "rule_id": outcome.get("rule_id", ""),
                "label": outcome.get("label", ""),
                "kind": outcome.get("kind", "programmatic"),
                "hard": bool(outcome.get("hard")),
                "passed": bool(outcome.get("passed")),
                "score": float(outcome.get("score", 0.0) or 0.0),
                "details": outcome.get("details", {}),
            }
        )
    _write_jsonl(run_dir / "outputs.jsonl", outputs)
    _write_jsonl(run_dir / "judgments.jsonl", judgments)

    candidate_summary = _variant_summary(
        skill_name=skill_name,
        snapshot=snapshot,
        rule_summary=rule_summary,
        replay_pool=replay_pool,
    )
    promotion_candidate = candidate_summary
    promotion_snapshot = snapshot
    best_variant = bundle.get("best_variant") if isinstance(bundle.get("best_variant"), dict) else {}
    best_dev_summary = bundle.get("best_dev_summary") if isinstance(bundle.get("best_dev_summary"), dict) else {}
    best_test_summary = bundle.get("best_test_summary") if isinstance(bundle.get("best_test_summary"), dict) else {}
    candidate_beats_current = bool(
        best_dev_summary
        and float(best_dev_summary.get("average_score", 0.0) or 0.0)
        >= float(candidate_summary.get("average_score", 0.0) or 0.0) + DEFAULT_MIN_SCORE_DELTA
        and int(best_dev_summary.get("hard_failures", 0) or 0) <= int(candidate_summary.get("hard_failures", 0) or 0)
    )
    if best_variant and best_test_summary and candidate_beats_current:
        promotion_candidate = dict(best_test_summary)
        promotion_snapshot = best_variant.get("snapshot") if isinstance(best_variant.get("snapshot"), dict) else snapshot
    champion_before = _load_champion(lineage_id)
    promotion = _promotion_decision(
        status=status,
        candidate=promotion_candidate,
        champion=champion_before,
    )
    if promotion.get("promoted"):
        _set_champion(
            lineage_id,
            {
                "lineage_id": lineage_id,
                "skill": skill_name,
                "snapshot": promotion_snapshot,
                "summary": promotion_candidate,
                "promotion": promotion,
                "updated_at": _utc_now(),
            },
        )

    # 触发冠军：when_to_use 改写经检索重放验证有提升时，另存一份触发版快照，
    # 与指令冠军并列（各自存在、各自人工取用），不自动覆盖线上。
    trigger = bundle.get("trigger") if isinstance(bundle.get("trigger"), dict) else {}
    trigger_metrics = trigger.get("metrics") if isinstance(trigger.get("metrics"), dict) else {}
    trigger_variant = trigger.get("variant") if isinstance(trigger.get("variant"), dict) else {}
    if trigger_metrics.get("improved"):
        trigger_snapshot = trigger_variant.get("snapshot") if isinstance(trigger_variant.get("snapshot"), dict) else {}
        if trigger_snapshot:
            _write_champion_skill_file(_lineage_champion_dir(lineage_id) / "trigger-SKILL.md", trigger_snapshot)

    summary = {
        "run_id": run_id,
        "lineage_id": lineage_id,
        "skill": skill_name,
        "status": status,
        "reasons": reasons,
        "candidate": candidate_summary,
        "trigger_candidate": {
            "variant": {k: v for k, v in trigger_variant.items() if k != "snapshot"},
            "metrics": trigger_metrics,
        }
        if trigger_variant
        else {},
        "candidate_variants": [
            {
                key: value
                for key, value in dict(variant).items()
                if key != "snapshot"
            }
            for variant in list(bundle.get("candidate_variants") or [])
            if isinstance(variant, dict)
        ],
        "variant_summaries": list(bundle.get("variant_summaries") or []),
        "best_candidate": dict(bundle.get("best_dev_summary") or {}),
        "promotion": promotion,
        "replay_counts": {
            "total": len(replay_pool),
            "mutate_dev": sum(1 for item in replay_pool if item.get("split") == "mutate_dev"),
            "promotion_test": sum(1 for item in replay_pool if item.get("split") == "promotion_test"),
        },
        "artifacts": {
            "dataset": str(_lineage_dataset_dir(lineage_id) / "replay_pool.jsonl"),
            "eval_spec": str(eval_dir / "eval_spec.json"),
            "outputs": str(run_dir / "outputs.jsonl"),
            "judgments": str(run_dir / "judgments.jsonl"),
        },
    }
    _write_json(run_dir / "summary.json", summary)
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "dataset_file": str(_lineage_dataset_dir(lineage_id) / "replay_pool.jsonl"),
        "eval_spec_file": str(eval_dir / "eval_spec.json"),
        "champion_file": str(_lineage_champion_dir(lineage_id) / "champion.json"),
        "promotion": promotion,
    }


async def _evaluate_online_skill_evolution_core(
    *,
    min_replay_samples: int = DEFAULT_MIN_REPLAY_SAMPLES,
    min_promotion_tests: int = DEFAULT_MIN_PROMOTION_TESTS,
    min_retrieved: int = DEFAULT_MIN_RETRIEVED,
    min_used_rate: float = DEFAULT_MIN_USED_RATE,
    min_relevance_rate: float = DEFAULT_MIN_RELEVANCE_RATE,
    min_rule_pass_rate: float = DEFAULT_MIN_RULE_PASS_RATE,
    write_report: bool = True,
    write_artifacts: bool = True,
    side_query: SideQuery | None = None,
    include_llm_rules: bool = False,
) -> dict[str, Any]:
    root = get_evolution_dir()
    provenance_rows = _read_jsonl(root / ONLINE_PROVENANCE_LOG)
    provenance_index = _read_json(root / ONLINE_PROVENANCE_INDEX, {})
    usage_stats = _read_json(root / SKILL_USAGE_STATS, {})
    lifecycle_stats = load_skill_stats()
    active_skills = _active_skill_snapshots()
    grouped_rows = _rows_by_skill(provenance_rows)

    action_counts = {"none": 0, "add": 0, "merge": 0, "revise": 0, "discard": 0, "failed": 0, "denied": 0, "other": 0}
    ok_count = 0
    candidate_events = 0
    accepted_events = 0
    recent_failures: list[dict[str, Any]] = []
    for row in provenance_rows:
        action = str(row.get("action") or "none")
        bucket = _action_bucket(action)
        action_counts[bucket] = int(action_counts.get(bucket, 0)) + 1
        if row.get("ok"):
            ok_count += 1
        if bucket not in {"none", "failed", "denied"}:
            candidate_events += 1
        if bucket in {"add", "merge", "revise"} and row.get("ok"):
            accepted_events += 1
        if bucket in {"failed", "denied"} or row.get("error"):
            recent_failures.append(
                {
                    "time": row.get("time", ""),
                    "action": action,
                    "skill": row.get("skill", ""),
                    "error": row.get("error", ""),
                }
            )

    all_names = set(active_skills)
    if isinstance(provenance_index, dict):
        all_names.update(str(name) for name in provenance_index if str(name).strip())
    if isinstance(usage_stats, dict):
        all_names.update(str(name) for name in usage_stats if str(name).strip())
    all_names.update(str(name) for name in lifecycle_stats if str(name).strip())

    skills: list[dict[str, Any]] = []
    for name in sorted(all_names):
        lineage_raw = provenance_index.get(name, {}) if isinstance(provenance_index, dict) else {}
        usage_raw = usage_stats.get(name, {}) if isinstance(usage_stats, dict) else {}
        lifecycle_raw = lifecycle_stats.get(name, {}) if isinstance(lifecycle_stats, dict) else {}
        lineage = lineage_raw if isinstance(lineage_raw, dict) else {}
        usage = usage_raw if isinstance(usage_raw, dict) else {}
        lifecycle = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
        snapshot = active_skills.get(name, {})
        if not snapshot:
            snapshot = {
                "name": name,
                "description": str(lineage.get("description") or lifecycle.get("description") or ""),
                "when_to_use": str(lineage.get("when_to_use") or ""),
                "instructions": "",
            }

        replay_pool = _build_replay_pool(name, grouped_rows.get(name, []), lineage, freeze=write_artifacts)
        rules = _compile_eval_rules(snapshot, include_llm_rules=include_llm_rules)
        rule_summary = await _summarize_rule_outcomes_async(
            rules,
            replay_pool,
            skill_name=name,
            side_query=side_query,
        )
        public_rule_summary = dict(rule_summary)
        public_rule_summary.pop("outcomes", None)
        retrieved = int(usage.get("retrieved", lifecycle.get("retrieved", 0)) or 0)
        relevant = int(usage.get("relevant", lifecycle.get("relevant", 0)) or 0)
        used = int(usage.get("used", lifecycle.get("used", 0)) or 0)
        pruned = bool(usage.get("pruned") or lifecycle.get("pruned"))
        promotion_test_count = sum(1 for item in replay_pool if item.get("split") == "promotion_test")
        status, reasons = _skill_status(
            replay_count=len(replay_pool),
            promotion_test_count=promotion_test_count,
            retrieved=retrieved,
            relevant=relevant,
            used=used,
            pruned=pruned,
            rule_summary=rule_summary,
            min_replay_samples=min_replay_samples,
            min_promotion_tests=min_promotion_tests,
            min_retrieved=min_retrieved,
            min_used_rate=min_used_rate,
            min_relevance_rate=min_relevance_rate,
            min_rule_pass_rate=min_rule_pass_rate,
        )
        if status == "incubating":
            # 僵尸孵化期清理：沉淀后长期从未被检索的 skill 归档（对称于"被检索没用"的 prune）。
            rows_for_skill = grouped_rows.get(name, [])
            seen_times = [str(r.get("time") or "") for r in rows_for_skill if r.get("time")]
            first_seen = min(seen_times) if seen_times else str(
                lifecycle.get("created_at") or lineage.get("last_time") or ""
            )
            zombie_stats = {
                "source": str(snapshot.get("source") or usage.get("source") or ""),
                "retrieved": retrieved,
                "skill_dir": str(snapshot.get("skill_dir") or usage.get("skill_dir") or ""),
            }
            try:
                if maybe_archive_never_retrieved_skill(name, zombie_stats, first_seen=first_seen):
                    status, reasons = "pruned", ["archived: never retrieved within trial window"]
                    try:
                        from .skills import reset_skill_cache

                        reset_skill_cache()
                    except Exception:
                        pass
            except Exception:
                pass
        current_version = (
            lineage.get("current_version", lifecycle.get("version", ""))
        )
        snapshot["version"] = str(current_version or "")
        candidate_bundle = await _build_candidate_eval_bundle_async(
            lineage_id=_lineage_id_for_skill(name),
            snapshot=snapshot,
            replay_pool=replay_pool,
            rules=rules,
            rule_summary=rule_summary,
            side_query=side_query,
            usage=usage,
        )
        artifacts = (
            _persist_eval_artifacts(
                skill_name=name,
                snapshot=snapshot,
                replay_pool=replay_pool,
                rules=rules,
                rule_summary=rule_summary,
                status=status,
                reasons=reasons,
                candidate_bundle=candidate_bundle,
            )
            if write_artifacts
            else {}
        )
        skills.append(
            {
                "skill": name,
                "lineage_id": _lineage_id_for_skill(name),
                "status": status,
                "reasons": reasons,
                "source_count": int(lineage.get("source_count", 0) or 0) if isinstance(lineage, dict) else 0,
                "history_count": int(lineage.get("history_count", 0) or 0) if isinstance(lineage, dict) else 0,
                "last_action": lineage.get("last_action", "") if isinstance(lineage, dict) else "",
                "last_time": lineage.get("last_time", "") if isinstance(lineage, dict) else "",
                "current_version": current_version,
                "created": int(lifecycle.get("created", 0) or 0),
                "evolutions": int(lifecycle.get("evolutions", 0) or 0),
                "invocations": int(lifecycle.get("invocations", 0) or 0),
                "feedback": int(lifecycle.get("feedback", 0) or 0),
                "retrieved": retrieved,
                "relevant": relevant,
                "used": used,
                "relevance_rate": _ratio(relevant, retrieved),
                "used_rate": _ratio(used, retrieved),
                "used_when_relevant_rate": _ratio(used, relevant),
                "replay": {
                    "count": len(replay_pool),
                    "mutate_dev": sum(1 for item in replay_pool if item.get("split") == "mutate_dev"),
                    "promotion_test": promotion_test_count,
                    "sources": sorted({str(item.get("source_type") or "") for item in replay_pool if item.get("source_type")}),
                },
                "eval": public_rule_summary,
                "candidate_eval": {
                    "candidate_count": len(list(candidate_bundle.get("candidate_variants") or [])) if isinstance(candidate_bundle, dict) else 0,
                    "best_candidate": dict(candidate_bundle.get("best_dev_summary") or {}) if isinstance(candidate_bundle, dict) else {},
                    "has_promotion_test_eval": bool((candidate_bundle or {}).get("best_test_summary")) if isinstance(candidate_bundle, dict) else False,
                },
                "artifacts": artifacts,
                "file": lifecycle.get("file", ""),
                "skill_dir": snapshot.get("skill_dir", ""),
            }
        )

    # 物化状态表：线上检索权重与试用期标注读这份缓存，不在检索路径上现算状态。
    _write_json(
        _online_eval_root() / "status_map.json",
        {
            "generated_at": _utc_now(),
            "statuses": {
                str(item.get("skill") or ""): str(item.get("status") or "")
                for item in skills
                if item.get("skill")
            },
        },
    )

    status_counts: dict[str, int] = {}
    champion_status_counts: dict[str, int] = {}
    total_replay = 0
    total_rule_outcomes = 0
    total_rule_passed = 0
    total_llm_rules = 0
    total_llm_rule_outcomes = 0
    total_llm_rule_passed = 0
    total_candidate_variants = 0
    for item in skills:
        status = str(item.get("status") or "unknown")
        status_counts[status] = int(status_counts.get(status, 0)) + 1
        artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
        promotion = artifacts.get("promotion") if isinstance(artifacts.get("promotion"), dict) else {}
        champion_status = str(promotion.get("status") or "unknown")
        champion_status_counts[champion_status] = int(champion_status_counts.get(champion_status, 0)) + 1
        replay = item.get("replay") if isinstance(item.get("replay"), dict) else {}
        eval_data = item.get("eval") if isinstance(item.get("eval"), dict) else {}
        total_replay += int(replay.get("count", 0) or 0)
        total_rule_outcomes += int(eval_data.get("outcome_count", 0) or 0)
        total_rule_passed += round(float(eval_data.get("pass_rate", 0.0) or 0.0) * int(eval_data.get("outcome_count", 0) or 0))
        total_llm_rules += int(eval_data.get("llm_rule_count", 0) or 0)
        llm_outcome_count = int(eval_data.get("llm_outcome_count", 0) or 0)
        total_llm_rule_outcomes += llm_outcome_count
        total_llm_rule_passed += round(float(eval_data.get("llm_pass_rate", 0.0) or 0.0) * llm_outcome_count)
        candidate_eval = item.get("candidate_eval") if isinstance(item.get("candidate_eval"), dict) else {}
        total_candidate_variants += int(candidate_eval.get("candidate_count", 0) or 0)

    report = {
        "generated_at": _utc_now(),
        "mode": "online_skill_lineage_eval",
        "data_dir": str(root),
        "methodology": {
            "lineage": "group online provenance and usage by skill",
            "replay": "freeze compact online conversation windows as replay samples",
            "rules": "compile deterministic rules and optional LLM judge rules from each active skill's description and instructions",
            "gate": "mark skills incubating, watch, healthy, pruned, or unobserved from replay, rule, and usage signals",
            "champion": "promote the current active version into a local online-eval champion only when it is healthy and beats the prior champion gate",
        },
        "llm_judge": {
            "enabled": bool(side_query),
            "rule_kind": "llm_binary",
            "response_source": "history_latest_assistant",
        },
        "thresholds": {
            "min_replay_samples": min_replay_samples,
            "min_promotion_tests": min_promotion_tests,
            "min_retrieved": min_retrieved,
            "min_used_rate": min_used_rate,
            "min_relevance_rate": min_relevance_rate,
            "min_rule_pass_rate": min_rule_pass_rate,
        },
        "aggregate": {
            "online_ingests": len(provenance_rows),
            "ok": ok_count,
            "ok_rate": _ratio(ok_count, len(provenance_rows)),
            "candidate_events": candidate_events,
            "accepted_events": accepted_events,
            "acceptance_rate": _ratio(accepted_events, candidate_events),
            "actions": action_counts,
            "skills": len(skills),
            "statuses": status_counts,
            "champion_statuses": champion_status_counts,
            "replay_samples": total_replay,
            "rule_outcomes": total_rule_outcomes,
            "rule_pass_rate": _ratio(total_rule_passed, total_rule_outcomes),
            "llm_rules": total_llm_rules,
            "llm_rule_outcomes": total_llm_rule_outcomes,
            "llm_rule_pass_rate": _ratio(total_llm_rule_passed, total_llm_rule_outcomes),
            "candidate_variants": total_candidate_variants,
        },
        "skills": skills,
        "recent_failures": recent_failures[-10:],
    }
    if write_report:
        report_path = root / "online_eval_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["report_file"] = str(report_path)
    return report


def evaluate_online_skill_evolution(
    *,
    min_replay_samples: int = DEFAULT_MIN_REPLAY_SAMPLES,
    min_promotion_tests: int = DEFAULT_MIN_PROMOTION_TESTS,
    min_retrieved: int = DEFAULT_MIN_RETRIEVED,
    min_used_rate: float = DEFAULT_MIN_USED_RATE,
    min_relevance_rate: float = DEFAULT_MIN_RELEVANCE_RATE,
    min_rule_pass_rate: float = DEFAULT_MIN_RULE_PASS_RATE,
    write_report: bool = True,
    write_artifacts: bool = True,
) -> dict[str, Any]:
    import asyncio

    return asyncio.run(
        _evaluate_online_skill_evolution_core(
            min_replay_samples=min_replay_samples,
            min_promotion_tests=min_promotion_tests,
            min_retrieved=min_retrieved,
            min_used_rate=min_used_rate,
            min_relevance_rate=min_relevance_rate,
            min_rule_pass_rate=min_rule_pass_rate,
            write_report=write_report,
            write_artifacts=write_artifacts,
            side_query=None,
            include_llm_rules=False,
        )
    )


async def evaluate_online_skill_evolution_async(
    *,
    side_query: SideQuery | None = None,
    min_replay_samples: int = DEFAULT_MIN_REPLAY_SAMPLES,
    min_promotion_tests: int = DEFAULT_MIN_PROMOTION_TESTS,
    min_retrieved: int = DEFAULT_MIN_RETRIEVED,
    min_used_rate: float = DEFAULT_MIN_USED_RATE,
    min_relevance_rate: float = DEFAULT_MIN_RELEVANCE_RATE,
    min_rule_pass_rate: float = DEFAULT_MIN_RULE_PASS_RATE,
    write_report: bool = True,
    write_artifacts: bool = True,
) -> dict[str, Any]:
    return await _evaluate_online_skill_evolution_core(
        min_replay_samples=min_replay_samples,
        min_promotion_tests=min_promotion_tests,
        min_retrieved=min_retrieved,
        min_used_rate=min_used_rate,
        min_relevance_rate=min_relevance_rate,
        min_rule_pass_rate=min_rule_pass_rate,
        write_report=write_report,
        write_artifacts=write_artifacts,
        side_query=side_query,
        include_llm_rules=side_query is not None,
    )


def _status_rank(status: str) -> int:
    order = {"watch": 0, "incubating": 1, "unobserved": 2, "pruned": 3, "healthy": 4}
    return order.get(str(status or ""), 9)


def _format_eval_failure_summary(eval_data: dict[str, Any], *, limit: int = 2) -> str:
    failures = eval_data.get("failures") if isinstance(eval_data.get("failures"), list) else []
    parts: list[str] = []
    for failure in failures[: max(1, int(limit))]:
        if not isinstance(failure, dict):
            continue
        rule_id = str(failure.get("rule_id") or "unknown_rule").strip()
        details = failure.get("details") if isinstance(failure.get("details"), dict) else {}
        reason = str(details.get("reason") or details.get("error") or "").strip()
        if not reason:
            reason = "no failure reason recorded"
        parts.append(f"{rule_id}: {reason}")
    if len(failures) > len(parts):
        parts.append(f"... {len(failures) - len(parts)} more")
    return "; ".join(parts)


def format_online_skill_eval(report: dict[str, Any] | None = None) -> str:
    report = report or evaluate_online_skill_evolution()
    aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), dict) else {}
    actions = aggregate.get("actions") if isinstance(aggregate.get("actions"), dict) else {}
    statuses = aggregate.get("statuses") if isinstance(aggregate.get("statuses"), dict) else {}
    champion_statuses = aggregate.get("champion_statuses") if isinstance(aggregate.get("champion_statuses"), dict) else {}
    llm_judge = report.get("llm_judge") if isinstance(report.get("llm_judge"), dict) else {}
    llm_enabled = bool(llm_judge.get("enabled"))

    lines = [
        "Online skill eval:",
        f"  data_dir={report.get('data_dir', '')}",
        (
            "  aggregate: "
            f"ingests={aggregate.get('online_ingests', 0)}, "
            f"ok_rate={_pct(float(aggregate.get('ok_rate', 0) or 0))}, "
            f"candidate_events={aggregate.get('candidate_events', 0)}, "
            f"acceptance_rate={_pct(float(aggregate.get('acceptance_rate', 0) or 0))}, "
            f"replay_samples={aggregate.get('replay_samples', 0)}, "
            f"rule_pass_rate={_pct(float(aggregate.get('rule_pass_rate', 0) or 0))}, "
            f"llm={'on' if llm_enabled else 'off'}, "
            f"llm_rules={aggregate.get('llm_rules', 0)}, "
            f"llm_judgments={aggregate.get('llm_rule_outcomes', 0)}, "
            f"llm_pass_rate={_pct(float(aggregate.get('llm_rule_pass_rate', 0) or 0))}, "
            f"candidates={aggregate.get('candidate_variants', 0)}"
        ),
        (
            "  actions: "
            f"none={actions.get('none', 0)}, add={actions.get('add', 0)}, "
            f"merge={actions.get('merge', 0)}, discard={actions.get('discard', 0)}, "
            f"failed={actions.get('failed', 0)}, denied={actions.get('denied', 0)}"
        ),
    ]
    if statuses:
        lines.append("  statuses: " + ", ".join(f"{key}={statuses[key]}" for key in sorted(statuses)))
    if champion_statuses:
        lines.append("  champion_statuses: " + ", ".join(f"{key}={champion_statuses[key]}" for key in sorted(champion_statuses)))

    skills = report.get("skills") if isinstance(report.get("skills"), list) else []
    if not skills:
        lines.append("  no online skill lineage or usage records found yet")
    else:
        lines.append("  skills:")
        ranked = sorted(
            skills,
            key=lambda item: (
                _status_rank(str(item.get("status") or "")),
                -int((item.get("replay") or {}).get("count", 0) if isinstance(item.get("replay"), dict) else 0),
                -int(item.get("retrieved", 0) or 0),
                str(item.get("skill") or ""),
            ),
        )
        for item in ranked[:20]:
            replay = item.get("replay") if isinstance(item.get("replay"), dict) else {}
            eval_data = item.get("eval") if isinstance(item.get("eval"), dict) else {}
            candidate_eval = item.get("candidate_eval") if isinstance(item.get("candidate_eval"), dict) else {}
            best_candidate = candidate_eval.get("best_candidate") if isinstance(candidate_eval.get("best_candidate"), dict) else {}
            artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
            promotion = artifacts.get("promotion") if isinstance(artifacts.get("promotion"), dict) else {}
            reasons = "; ".join(str(reason) for reason in item.get("reasons", []) if str(reason).strip())
            failure_summary = _format_eval_failure_summary(eval_data)
            suffix_parts = []
            if reasons:
                suffix_parts.append(reasons)
            if failure_summary:
                suffix_parts.append(f"failures: {failure_summary}")
            suffix = f" - {'; '.join(suffix_parts)}" if suffix_parts else ""
            lines.append(
                "    "
                f"{item.get('skill')}: status={item.get('status')}, "
                f"replay={replay.get('count', 0)} "
                f"(test={replay.get('promotion_test', 0)}), "
                f"rules={eval_data.get('rule_count', 0)}, "
                f"llm_rules={eval_data.get('llm_rule_count', 0)}, "
                f"llm_judgments={eval_data.get('llm_outcome_count', 0)}, "
                f"candidates={candidate_eval.get('candidate_count', 0)}, "
                f"best_candidate_score={float(best_candidate.get('average_score', 0.0) or 0.0):.2f}, "
                f"rule_pass={_pct(float(eval_data.get('pass_rate', 0) or 0))}, "
                f"hard_failures={eval_data.get('hard_failures', 0)}, "
                f"retrieved={item.get('retrieved', 0)}, "
                f"used_rate={_pct(float(item.get('used_rate', 0) or 0))}, "
                f"champion={promotion.get('status', 'n/a')}"
                f"{suffix}"
            )
        if len(skills) > 20:
            lines.append(f"    ... {len(skills) - 20} more skill(s) omitted")

    failures = report.get("recent_failures") if isinstance(report.get("recent_failures"), list) else []
    if failures:
        lines.append("  recent failures:")
        for failure in failures[-5:]:
            lines.append(
                "    "
                f"{failure.get('time', '')} {failure.get('action', '')} "
                f"{failure.get('skill', '')}: {failure.get('error', '')}"
            )

    if report.get("report_file"):
        lines.append(f"  report_file={report['report_file']}")
    return "\n".join(lines)


async def format_online_skill_eval_async(
    *,
    side_query: SideQuery | None = None,
    report: dict[str, Any] | None = None,
) -> str:
    if report is None:
        report = await evaluate_online_skill_evolution_async(side_query=side_query)
    return format_online_skill_eval(report)


if __name__ == "__main__":
    print(format_online_skill_eval())
