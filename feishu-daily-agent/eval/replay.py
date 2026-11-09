"""黄金回放：同一批冻结 case，对比原型静态规则、无经验、候选与已批准。

approved/candidate/no_experience 三种策略直接调用生产 `policy.evaluate` 与
`experience.find_best_match`，阈值取自 golden 的冻结配置；prototype 仅用于
展示原型“高分即发”的缺陷对照。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.clock import epoch_ms  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.domain import policy  # noqa: E402
from app.memory import experience as experience_module  # noqa: E402
from app.memory import retrieval  # noqa: E402
from app.models import Base, Experience, ExperienceHead, Message, Source  # noqa: E402

GOLDEN = ROOT / "eval" / "golden_cases.json"
RUNS = ROOT / "eval" / "runs"
LATEST = RUNS / "feishu-eval.json"
INJECTION = ("忽略之前", "ignore previous", "system prompt", "开发者模式")


def load_golden(path: Path = GOLDEN) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def frozen_settings(thresholds: dict):
    settings = get_settings()
    saved = {
        name: getattr(settings, name)
        for name in (
            "auto_reply_threshold", "draft_min_threshold", "negative_example_threshold",
            "deployment_mode", "auto_reply_enabled", "shadow_mode",
            "feishu_chat_whitelist", "feishu_reply_allowed_chats",
            "auto_reply_allowed_categories", "auto_reply_max_per_chat_per_day",
        )
    }
    try:
        settings.auto_reply_threshold = thresholds["autoMin"]
        settings.draft_min_threshold = thresholds["draftMin"]
        settings.negative_example_threshold = thresholds["negativeMin"]
        settings.deployment_mode = "whitelist_auto"
        settings.auto_reply_enabled = True
        settings.shadow_mode = False
        settings.feishu_chat_whitelist = "oc_1,oc_group"
        settings.feishu_reply_allowed_chats = "oc_1,oc_group"
        settings.auto_reply_allowed_categories = "faq"
        settings.auto_reply_max_per_chat_per_day = 100
        yield settings
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)


def _seed_experiences(session: Session, experiences: list[dict], strategy: str) -> None:
    if strategy == "no_experience":
        return
    for item in experiences:
        status = (
            "candidate" if strategy == "candidate" else item.get("status", "approved_auto")
        )
        row = Experience(
            tenant="default",
            owner="ou_owner",
            intent_id=item["id"],
            version=1,
            question_pattern=item["question"],
            canonical_answer=item["answer"],
            patterns_json=json.dumps(item.get("patterns") or [item["question"]], ensure_ascii=False),
            negative_examples_json=json.dumps(item.get("negative") or [], ensure_ascii=False),
            audience_json=json.dumps(item.get("audience") or {}, ensure_ascii=False),
            visibility=item.get("visibility", "private"),
            sensitive_level=item.get("sensitive", "private"),
            status=status,
            category=item.get("category", "faq"),
        )
        session.add(row)
        session.flush()
        session.add(
            ExperienceHead(intent_id=item["id"], tenant="default", experience_id=row.id)
        )
    session.flush()


def _source_for(session: Session, case: dict) -> Source:
    chat_id = "oc_group" if case.get("chat_type") == "group" else "oc_1"
    source = session.scalar(select(Source).where(Source.chat_id == chat_id))
    if source is None:
        source = Source(
            tenant="default", owner="ou_owner", chat_id=chat_id,
            source_type=case.get("chat_type", "p2p"), reply_allowed=True, enabled=True,
        )
        session.add(source)
        session.flush()
    return source


def decide_engine(case: dict, strategy: str, thresholds: dict) -> dict:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        golden = load_golden()
        _seed_experiences(session, golden["experiences"], strategy)
        source = _source_for(session, case)
        now = datetime.now(timezone.utc)
        message = Message(
            message_id=f"{strategy}-{case['id']}", tenant="default",
            source_id=source.id, chat_id=source.chat_id, thread_id="",
            sender_id="ou_user1", msg_type="text", content_text=case["text"],
            create_time=epoch_ms(now), category=case.get("category", "question"),
        )
        session.add(message)
        session.flush()
        match = experience_module.find_best_match(
            session, case["text"], tenant="default", source=source
        )
        decision = policy.evaluate(
            session, message=message, source=source, experience=match.experience,
            similarity=match.score, match_reasons=match.reasons, now=now.replace(tzinfo=None),
        )
        if decision.decision == "auto_eligible":
            label = "auto"
        elif decision.decision == "skip":
            label = "skip"
        else:
            label = "draft"
        return {
            "decision": label,
            "reason": ",".join(decision.reasons),
            "experience": decision.experience.intent_id if decision.experience else None,
            "score": round(float(decision.similarity or 0.0), 6),
        }
    finally:
        session.close()
        engine.dispose()


def _prototype_decide(case: dict, golden: dict, thresholds: dict) -> dict:
    if case.get("category") in {"task", "notice"}:
        return {"decision": "skip", "reason": "not_question", "experience": None, "score": 0.0}
    approved = [exp for exp in golden["experiences"] if exp.get("status") == "approved_auto"]
    corpus = {
        exp["id"]: retrieval.tokenize(" ".join([exp["question"], *exp.get("patterns", [])]))
        for exp in approved
    }
    ranked, _ = retrieval.rank(case["text"], corpus)
    if not ranked:
        return {"decision": "draft", "reason": "no_match", "experience": None, "score": 0.0}
    best_id, score = ranked[0]
    if float(score) >= thresholds["autoMin"]:
        return {
            "decision": "auto", "reason": "prototype_high_score",
            "experience": best_id, "score": round(float(score), 6),
        }
    return {
        "decision": "draft", "reason": "prototype_below_threshold",
        "experience": best_id, "score": round(float(score), 6),
    }


def wilson_lower(success: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 1.0
    p = success / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5)
    return max(0.0, (centre - margin) / denom)


def evaluate(cases: list[dict], decisions: list[dict]) -> dict:
    expected_auto = [case for case in cases if case["expected"]["decision"] == "auto"]
    actual_auto = [item for item in decisions if item["decision"] == "auto"]
    correct_auto = [
        item for item, case in zip(decisions, cases)
        if item["decision"] == "auto" and case["expected"]["decision"] == "auto"
        and (not case["expected"].get("experience") or item["experience"] == case["expected"]["experience"])
    ]
    severe = [
        {"caseId": case["id"], "text": case["text"], "actual": item}
        for item, case in zip(decisions, cases)
        if item["decision"] == "auto" and case["expected"]["decision"] != "auto"
    ]
    expected_draft = [case for case in cases if case["expected"]["decision"] == "draft"]
    correct_draft = [
        item for item, case in zip(decisions, cases)
        if item["decision"] == "draft" and case["expected"]["decision"] == "draft"
    ]
    precision = (len(correct_auto) / len(actual_auto)) if actual_auto else 1.0
    coverage = (len(correct_auto) / len(expected_auto)) if expected_auto else 1.0
    draft_recall = (len(correct_draft) / len(expected_draft)) if expected_draft else 1.0
    ok = not severe and coverage >= 0.8 and (not actual_auto or precision >= 0.9)
    return {
        "ok": ok,
        "cases": len(cases),
        "expectedAuto": len(expected_auto),
        "actualAuto": len(actual_auto),
        "correctAuto": len(correct_auto),
        "autoPrecision": round(precision, 4),
        "autoCoverage": round(coverage, 4),
        "draftRecall": round(draft_recall, 4),
        "autoPrecisionLower95": round(wilson_lower(len(correct_auto), len(actual_auto)), 4),
        "autoCoverageLower95": round(wilson_lower(len(correct_auto), len(expected_auto)), 4),
        "severeErrors": severe,
        "note": "approved 策略使用生产 policy.evaluate；no_experience/candidate 使用同一引擎但禁用自动放行",
    }


def _group_metrics(cases: list[dict], decisions: list[dict], key: str) -> dict:
    values = sorted({str(case.get(key, "")) for case in cases})
    result = {}
    for value in values:
        selected = [
            (case, decision) for case, decision in zip(cases, decisions)
            if str(case.get(key, "")) == value
        ]
        result[value] = evaluate(
            [case for case, _ in selected], [decision for _, decision in selected]
        )
    return result


def build_report(golden: dict) -> dict:
    cases = golden["cases"]
    thresholds = golden["thresholds"]
    strategies = {}
    case_evidence = {}
    all_decisions = {}
    with frozen_settings(thresholds):
        for strategy in ("no_experience", "candidate", "approved"):
            decisions = [decide_engine(case, strategy, thresholds) for case in cases]
            strategies[strategy] = evaluate(cases, decisions)
            splits = {}
            for split in ("dev", "holdout"):
                split_cases = [case for case in cases if case.get("split", "dev") == split]
                split_decisions = [
                    decision for case, decision in zip(cases, decisions)
                    if case.get("split", "dev") == split
                ]
                splits[split] = evaluate(split_cases, split_decisions)
            strategies[strategy]["splits"] = splits
            strategies[strategy]["groups"] = {
                "chatType": _group_metrics(cases, decisions, "chat_type"),
                "category": _group_metrics(cases, decisions, "category"),
                "timeBlock": _group_metrics(cases, decisions, "timeBlock"),
            }
            all_decisions[strategy] = decisions
            case_evidence[strategy] = [
                {
                    "caseId": case["id"],
                    "text": case["text"],
                    "expected": case["expected"],
                    "actual": decision,
                    "pass": (
                        decision["decision"] == case["expected"]["decision"]
                        and (case["expected"]["decision"] != "auto"
                             or not case["expected"].get("experience")
                             or decision["experience"] == case["expected"]["experience"])
                    ),
                }
                for case, decision in zip(cases, decisions)
            ]
        prototype_decisions = [_prototype_decide(case, golden, thresholds) for case in cases]
        strategies["prototype"] = evaluate(cases, prototype_decisions)
        strategies["prototype"]["groups"] = {
            "chatType": _group_metrics(cases, prototype_decisions, "chat_type"),
            "category": _group_metrics(cases, prototype_decisions, "category"),
            "timeBlock": _group_metrics(cases, prototype_decisions, "timeBlock"),
        }
        all_decisions["prototype"] = prototype_decisions
        case_evidence["prototype"] = [
            {
                "caseId": case["id"],
                "text": case["text"],
                "expected": case["expected"],
                "actual": decision,
                "pass": (
                    decision["decision"] == case["expected"]["decision"]
                    and (case["expected"]["decision"] != "auto"
                         or not case["expected"].get("experience")
                         or decision["experience"] == case["expected"]["experience"])
                ),
            }
            for case, decision in zip(cases, prototype_decisions)
        ]
    payload = {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "goldenSha256": hashlib.sha256(GOLDEN.read_bytes()).hexdigest(),
        "replaySha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "engine": retrieval.rank("年假", {"x": retrieval.tokenize("年假")})[1],
        "thresholds": thresholds,
        "thresholdSource": "golden_cases.json（部署校准值；生产 config 默认值需按此冻结后再灰度）",
        "ok": strategies["approved"]["ok"],
        "strategies": strategies,
        "caseEvidence": case_evidence,
        "cases": case_evidence["approved"],
        "failingCases": [item for item in case_evidence["approved"] if not item["pass"]],
        "sampleNote": "12 个冻结 case；dev 10 / holdout 2，仅用于回归与硬门，不足以支撑对外精度宣称",
        "repro": "cd feishu-daily-agent && .venv/Scripts/python.exe eval/replay.py --run",
    }
    payload["artifactDigest"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return payload


def write_report(report: dict, path: Path = LATEST) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def check(path: Path = LATEST, *, max_age_days: int = 7) -> tuple[bool, list[str], dict]:
    if not path.is_file():
        return False, ["artifact_missing"], {}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return False, [f"artifact_invalid:{exc}"], {}
    issues = []
    generated = report.get("generatedAt")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(generated)).total_seconds() / 86400
    except (TypeError, ValueError):
        age = None
    if age is None or age > max_age_days:
        issues.append("artifact_stale")
    if report.get("ok") is not True:
        issues.append("eval_failed")
    if report.get("strategies", {}).get("approved", {}).get("severeErrors"):
        issues.append("severe_errors")
    if report.get("goldenSha256") != hashlib.sha256(GOLDEN.read_bytes()).hexdigest():
        issues.append("golden_changed")
    if report.get("replaySha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        issues.append("replay_code_changed")
    return not issues, issues, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.run:
        report = build_report(load_golden())
        path = write_report(report)
        print(json.dumps({"ok": report["ok"], "path": str(path), **report["strategies"]["approved"]}, ensure_ascii=False))
        return 0 if report["ok"] else 1
    ok, issues, report = check()
    print(json.dumps({"ok": ok, "issues": issues, "path": str(LATEST)}, ensure_ascii=False))
    return 0 if ok else (1 if "eval_failed" in issues or "severe_errors" in issues else 2)


if __name__ == "__main__":
    raise SystemExit(main())
