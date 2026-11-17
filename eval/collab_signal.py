#!/usr/bin/env python3
"""桥接层闭环信号（eval-workflow.v1 第 14 号信号源）：collab-eval artifact → findings。

与 feishu_signal.py 完全同构的三判据（干净与坏了不能外观相同）：
- artifact_missing  缺产物 → 桥接层质量不可宣称（medium）
- artifact_invalid  产物无法解析（high）
- artifact_stale    产物超 7 天（medium）
- eval_failed / severe_errors / safety_failed（high）
- golden_changed    用例定义变了但产物没重跑（medium）
不在这里执行任何真实写操作；RED 失败按冻结门 severity 映射。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INTEGRATION = REPO / "integration"
ARTIFACT = INTEGRATION / "data" / "collab-eval.json"
GOLDEN = INTEGRATION / "eval" / "golden_cases.json"
GATES = INTEGRATION / "eval" / "gates.json"
MAX_AGE_DAYS = 7
REPRO = "python integration/eval/collab_eval.py --run"


def _gates() -> dict:
    try:
        return json.loads(GATES.read_text(encoding="utf-8")).get("severity", {})
    except (OSError, ValueError):
        return {}


def evaluate_report(report: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sev_map = _gates()
    issues: list[str] = []
    generated = report.get("generatedAt")
    try:
        generated_at = datetime.fromisoformat(generated)
        age_days = (now - generated_at).total_seconds() / 86400
    except (TypeError, ValueError):
        generated_at = None
        age_days = None
    if generated_at is None:
        issues.append("artifact_invalid_timestamp")
    elif age_days > MAX_AGE_DAYS:
        issues.append("artifact_stale")
    if GOLDEN.is_file() and report.get("goldenSha256") != hashlib.sha256(
            GOLDEN.read_bytes()).hexdigest():
        issues.append("golden_changed")
    if report.get("ok") is not True:
        issues.append("eval_failed")
    if report.get("safetyFailed"):
        issues.append("safety_failed")
    severe = int(report.get("severe") or 0)
    if severe > 0:
        issues.append("severe_errors")
    hard = {"eval_failed", "safety_failed", "severe_errors"}
    severity = "high" if hard & set(issues) else "medium"
    severity = sev_map.get(issues[0], severity) if issues else severity
    return {
        "ok": not issues,
        "issues": issues,
        "ageDays": round(age_days, 2) if age_days is not None else None,
        "overallRate": report.get("overallRate"),
        "cases": report.get("cases"),
        "artifact": report.get("goldenSha256"),
        "generatedAt": generated,
    }


def findings_for(path: Path = ARTIFACT, *, now: datetime | None = None) -> tuple[list[dict], dict]:
    if not path.is_file():
        return (
            [{
                "source": "collab",
                "key": "collab:artifact-missing",
                "severity": "medium",
                "title": "桥接层评测产物缺失，intake/nudge/ask 质量不可宣称",
                "detail": {"artifact": str(path), "repro": REPRO},
            }],
            {"ok": False, "issues": ["artifact_missing"]},
        )
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return (
            [{
                "source": "collab",
                "key": "collab:artifact-invalid",
                "severity": "high",
                "title": "桥接层评测产物无法解析",
                "detail": {"artifact": str(path), "error": str(exc), "repro": REPRO},
            }],
            {"ok": False, "issues": ["artifact_invalid"]},
        )
    summary = evaluate_report(report, now=now)
    findings: list[dict] = []
    if summary["issues"]:
        severity = "high" if {"eval_failed", "severe_errors", "safety_failed"} & set(
            summary["issues"]) else "medium"
        findings.append({
            "source": "collab",
            "key": "collab:eval:" + ",".join(sorted(summary["issues"])),
            "severity": severity,
            "title": "桥接层评测未通过或产物过期",
            "detail": {
                **summary,
                "artifact": str(path),
                "failingCases": report.get("failingCases") or [],
                "repro": REPRO,
            },
        })
    return findings, summary


def collect(path: Path = ARTIFACT, *, now: datetime | None = None) -> dict:
    findings, summary = findings_for(path, now=now)
    return {"ok": summary.get("ok", False), "findings": findings, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = collect()
    if args.json or not args.check:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"ok": result["ok"],
                          "issues": result["summary"].get("issues", [])},
                         ensure_ascii=False))
    issues = result["summary"].get("issues") or []
    if issues and {"eval_failed", "severe_errors", "safety_failed"} & set(issues):
        return 1
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
