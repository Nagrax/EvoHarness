"""Storage for explicitly scoped evaluation evidence; no global-report fallback.

The producer must evaluate the identified file and capture its SHA256 before the
run. This module records supplied judgments; it does not perform evaluation or
authorize callers. Each outcome follows online_skill_eval's judgment format.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile

from agents.skill_scope import ScopedSkillRef


def _valid_ref(ref: ScopedSkillRef) -> bool:
    return (isinstance(ref, ScopedSkillRef)
            and all(isinstance(getattr(ref, k), str) and getattr(ref, k).strip()
                    for k in ("name", "tenant", "space", "owner", "producer"))
            and type(ref.source_revision) is int and ref.source_revision >= 0)


def _ref_matches(value: object, ref: ScopedSkillRef) -> bool:
    expected = asdict(ref)
    return (isinstance(value, dict) and value.keys() == expected.keys()
            and all(type(value[key]) is type(item) and value[key] == item
                    for key, item in expected.items()))


def _path(ref: ScopedSkillRef, root: Path | None) -> Path:
    if root is None:
        from agents.skill_evolution import get_evolution_dir
        root = get_evolution_dir()
    return Path(root) / "scoped_eval_reports" / (ref.cache_key() + ".json")


def _digest(skill_file: str | Path) -> str:
    return hashlib.sha256(Path(skill_file).read_bytes()).hexdigest()


def _valid_outcomes(outcomes: object) -> bool:
    return (isinstance(outcomes, list) and bool(outcomes)
            and all(isinstance(row, dict)
                    and isinstance(row.get("sample_id"), str) and row["sample_id"].strip()
                    and isinstance(row.get("rule_id"), str) and row["rule_id"].strip()
                    and type(row.get("passed")) is bool for row in outcomes)
            and len({(row["sample_id"], row["rule_id"]) for row in outcomes}) == len(outcomes))


def write_scoped_skill_report(ref: ScopedSkillRef, skill_file: str | Path, *,
                              skill_sha256: str, protocol_id: str, run_id: str,
                              outcomes: list[dict], evolution_root: Path | None = None,
                              scope_generation: int = 0) -> dict:
    """Persist actual scoped-run judgments, rejecting a changed skill file."""
    from agents.skill_evolution import skill_state_read_only
    if skill_state_read_only():
        return {"ok": False, "error": "skill_state_read_only"}
    if not _valid_ref(ref):
        raise ValueError("complete scoped skill ref required")
    if type(scope_generation) is not int or scope_generation < 0:
        raise ValueError("invalid scope generation")
    if not all(isinstance(v, str) and v.strip() for v in (protocol_id, run_id)):
        raise ValueError("protocol_id and run_id required")
    if not _valid_outcomes(outcomes):
        raise ValueError("nonempty sample/rule outcomes with boolean passed required")
    if not isinstance(skill_sha256, str) or _digest(skill_file) != skill_sha256:
        raise ValueError("skill content changed since evaluation")
    report = {"ref": asdict(ref), "skill_sha256": skill_sha256,
              "scope_generation": scope_generation,
              "protocol_id": protocol_id, "run_id": run_id, "outcomes": outcomes}
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True)
    path = _path(ref, evolution_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".scoped-report-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return report


def read_scoped_skill_report(ref: ScopedSkillRef, skill_file: str | Path, *,
                             evolution_root: Path | None = None, scope_generation: int = 0) -> dict | None:
    """Return only complete evidence matching all scope fields and current bytes."""
    if not _valid_ref(ref) or type(scope_generation) is not int or scope_generation < 0:
        return None
    try:
        report = json.loads(_path(ref, evolution_root).read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not _ref_matches(report.get("ref"), ref):
            return None
        if type(report.get("scope_generation")) is not int or report["scope_generation"] != scope_generation:
            return None
        if report.get("skill_sha256") != _digest(skill_file):
            return None
        if not all(isinstance(report.get(k), str) and report[k].strip()
                   for k in ("protocol_id", "run_id")):
            return None
        outcomes = report.get("outcomes")
        if not _valid_outcomes(outcomes):
            return None
    except (OSError, ValueError, TypeError):
        return None
    report["outcome_count"] = len(outcomes)
    report["samples"] = len({row["sample_id"] for row in outcomes})
    report["check_count"] = len(outcomes)
    report["passed"] = sum(row["passed"] for row in outcomes)
    report["pass_rate"] = report["passed"] / len(outcomes)
    return report


def evaluate_scoped_skill(ref: ScopedSkillRef, skill_file: str | Path, *,
                          usage_samples: list[dict], protocol_id: str, run_id: str,
                          evolution_root: Path | None = None, scope_generation: int = 0) -> dict:
    """Evaluate only explicitly bound usage samples using existing online rules.

    Each sample must contain the full ref and the hash of the skill actually
    used when the conversation was recorded. Legacy unscoped samples cannot
    establish that association and are ignored. This caller supplies the
    scoped evidence directly; no global log or global report is consulted.
    """
    from agents.frontmatter import parse_frontmatter
    from agents.online_skill_eval import (_build_replay_pool, _compile_eval_rules,
                                          _summarize_rule_outcomes)
    if not _valid_ref(ref):
        raise ValueError("complete scoped skill ref required")
    content = Path(skill_file).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    selected = [r for r in usage_samples if isinstance(r, dict)
                and _ref_matches(r.get("ref"), ref) and r.get("skill_sha256") == digest
                and type(r.get("scope_generation")) is int
                and r["scope_generation"] == scope_generation]
    parsed = parse_frontmatter(content.decode("utf-8-sig"))
    snapshot = {"name": ref.name, "description": str(parsed.meta.get("description") or ""),
                "when_to_use": str(parsed.meta.get("when-to-use") or ""),
                "instructions": parsed.body}
    rules = _compile_eval_rules(snapshot)
    samples = _build_replay_pool(ref.name, [], {}, usage_rows=selected, freeze=False)
    outcomes = _summarize_rule_outcomes(rules, samples).get("outcomes", [])
    if not _valid_outcomes(outcomes):
        return {"ok": False, "error": "scope_evaluation_evidence_missing"}
    written = write_scoped_skill_report(ref, skill_file, skill_sha256=digest, protocol_id=protocol_id,
                                        run_id=run_id, outcomes=outcomes, evolution_root=evolution_root,
                                        scope_generation=scope_generation)
    if written.get("error") == "skill_state_read_only":
        return {"ok": False, "error": "skill_state_read_only",
                "outcomes": outcomes}
    return {"ok": True, "report": read_scoped_skill_report(ref, skill_file,
                                                            evolution_root=evolution_root,
                                                            scope_generation=scope_generation)}


def record_scoped_turn(bindings: list[dict], *, user_text: str, assistant_text: str,
                       evolution_root: Path | None = None, scope_generation: int = 0) -> int:
    """Record exact invocation bindings captured during this completed turn."""
    from datetime import datetime, timezone
    from agents.skill_evolution import skill_state_read_only
    if skill_state_read_only():
        return 0
    if type(scope_generation) is not int or scope_generation < 0:
        return 0
    if not user_text.strip() or not assistant_text.strip():
        return 0
    recorded = 0
    seen = set()
    for binding in bindings:
        try:
            ref = ScopedSkillRef(**binding["ref"])
            digest = binding["skill_sha256"]
        except (TypeError, KeyError):
            continue
        if (not _valid_ref(ref) or not _ref_matches(binding.get("ref"), ref)
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            continue
        key = (ref.cache_key(), digest)
        if key in seen:
            continue
        seen.add(key)
        path = _path(ref, evolution_root).parent.parent / "scoped_eval_samples" / (ref.cache_key() + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ref": asdict(ref), "skill_sha256": digest,
               "scope_generation": scope_generation,
               "recorded_at": datetime.now(timezone.utc).isoformat(),
               "messages": [{"role": "user", "content": user_text},
                            {"role": "assistant", "content": assistant_text}]}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        recorded += 1
    return recorded


def evaluate_scoped_skill_from_log(ref: ScopedSkillRef, skill_file: str | Path, *,
                                   protocol_id: str, run_id: str,
                                   evolution_root: Path | None = None,
                                   scope_generation: int = 0) -> dict:
    """Evaluate recorded turns in the caller-authorized scope generation.

    The caller must separately check current authorization; generation is the
    trusted latest scope-event ID (zero only when no event has occurred).
    """
    if not _valid_ref(ref) or type(scope_generation) is not int or scope_generation < 0:
        return {"ok": False, "error": "scope_authorization_required"}
    path = _path(ref, evolution_root).parent.parent / "scoped_eval_samples" / (ref.cache_key() + ".jsonl")
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except (ValueError, TypeError):
                continue
    except OSError:
        pass
    from agents.skill_scope import skill_scope
    try:
        scope = skill_scope(skill_file)
        if not isinstance(scope, dict) or any(type(scope.get(k)) is not type(getattr(ref, k))
                or scope[k] != getattr(ref, k) for k in
                ("tenant", "space", "owner", "producer", "source_revision")):
            return {"ok": False, "error": "skill_scope_mismatch"}
    except (OSError, ValueError, UnicodeError):
        return {"ok": False, "error": "skill_scope_mismatch"}
    return evaluate_scoped_skill(ref, skill_file, usage_samples=rows, protocol_id=protocol_id,
                                 run_id=run_id, evolution_root=evolution_root,
                                 scope_generation=scope_generation)
