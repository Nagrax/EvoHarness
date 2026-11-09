#!/usr/bin/env python3
"""飞书日报闭环信号：读取黄金回放产物，产出 loop 可消费的 finding。

设计原则与 loop_source_health 一致：
- 缺产物、过期产物、评测崩溃都必须可见；
- 干净与坏掉不能外观相同；
- 不在这里执行真实飞书写操作。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ARTIFACT = REPO / "feishu-daily-agent" / "eval" / "runs" / "feishu-eval.json"
GOLDEN = REPO / "feishu-daily-agent" / "eval" / "golden_cases.json"
REPLAY = REPO / "feishu-daily-agent" / "eval" / "replay.py"
MAX_AGE_DAYS = 7
REPRO = "cd feishu-daily-agent && .venv/Scripts/python.exe eval/replay.py --run"


def evaluate_report(report: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
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
    if GOLDEN.is_file() and report.get("goldenSha256") != hashlib.sha256(GOLDEN.read_bytes()).hexdigest():
        issues.append("golden_changed")
    if REPLAY.is_file() and report.get("replaySha256") != hashlib.sha256(REPLAY.read_bytes()).hexdigest():
        issues.append("replay_code_changed")
    approved = (report.get("strategies") or {}).get("approved") or {}
    if report.get("ok") is not True:
        issues.append("eval_failed")
    severe = approved.get("severeErrors") or []
    if severe:
        issues.append("severe_errors")
    return {
        "ok": not issues,
        "issues": issues,
        "ageDays": round(age_days, 2) if age_days is not None else None,
        "severeErrors": severe,
        "metrics": {
            key: approved.get(key)
            for key in ("autoPrecision", "autoCoverage", "draftRecall", "actualAuto", "correctAuto")
        },
        "artifact": report.get("artifactDigest"),
        "generatedAt": generated,
    }


def findings_for(path: Path = ARTIFACT, *, now: datetime | None = None) -> tuple[list[dict], dict]:
    if not path.is_file():
        return (
            [{
                "source": "feishu",
                "key": "feishu:artifact-missing",
                "severity": "medium",
                "title": "飞书黄金回放产物缺失，自动回复质量不可宣称",
                "detail": {"artifact": str(path), "repro": REPRO},
            }],
            {"ok": False, "issues": ["artifact_missing"]},
        )
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return (
            [{
                "source": "feishu",
                "key": "feishu:artifact-invalid",
                "severity": "high",
                "title": "飞书黄金回放产物无法解析",
                "detail": {"artifact": str(path), "error": str(exc), "repro": REPRO},
            }],
            {"ok": False, "issues": ["artifact_invalid"]},
        )
    summary = evaluate_report(report, now=now)
    findings: list[dict] = []
    if summary["issues"]:
        severity = "high" if {"eval_failed", "severe_errors"} & set(summary["issues"]) else "medium"
        findings.append({
            "source": "feishu",
            "key": "feishu:eval:" + ",".join(sorted(summary["issues"])),
            "severity": severity,
            "title": "飞书黄金回放未通过或产物过期",
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
    parser.add_argument("--path", default=str(ARTIFACT))
    args = parser.parse_args(argv)
    result = collect(Path(args.path))
    if args.json or not args.check:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(
            {"ok": result["ok"], "issues": result["summary"].get("issues", [])},
            ensure_ascii=False,
        ))
    issues = result["summary"].get("issues") or []
    if issues and {"eval_failed", "severe_errors"} & set(issues):
        return 1
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
