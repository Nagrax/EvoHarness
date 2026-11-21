#!/usr/bin/env python3
"""Dreaming 离线配对实验（EVO-05）：同输入两臂确定性对比，产物哈希 + 成本落盘。

实验口径
--------
- 同一合成知识库 + 同一确定性"源漂移"输入，跑两臂：
  - ``dreaming-off``（基线）：不做夜间对账，主张原样；
  - ``dreaming-on``：经 ``ops.dreaming_nightly.run_nightly`` 跑 Dreaming 单轮
    （SnapshotRecheckJudge，确定性代码不调模型）→ 漂移项产候选 → **显式采用**
    （固定责任人、新值取漂移源当前值，不猜答案）。
- 成本走真实预算台账（``budget.reserve``/``budget.settle``，category=dreaming）；
  off 臂无夜跑调用（空记录即诚实）。记录是台账行的规范化投影（去 uuid/时间戳）。
- 离线：``sqlite:///:memory:`` + 确定性 fake source reader，不触网、不依赖真实模型。
- 归一化排除 claim_id(uuid)/时钟时间戳 → 同输入两轮重跑产物哈希一致。

产物：``eval/runs/paired_dreaming/<tag>-<digest8>/``（gitignored）
  - ``arm-dreaming-on.json`` / ``arm-dreaming-off.json``（双臂产物）
  - ``comparison.json`` / ``comparison.md``（哈希、候选/采用、成本对比）
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.governance import budget
from evoharness.governance.models import BudgetReservation
from evoharness.ids import sha256_json
from evoharness.knowledge import reconcile
from evoharness.knowledge import service as knowledge_service
from evoharness.knowledge.models import KnowledgeClaim
from evoharness.models import Base
from ops import dreaming_nightly

DEFAULT_OUT = REPO / "eval" / "runs" / "paired_dreaming"
SPACE = "support"
DATE = "2026-11-21"          # 固定日期：重跑哈希可复现
ADOPT_ACTOR = "maintainer-offline"
DRIFT_REF = "config:retry@1"

# 合成输入（确定性）：verified 主张带快照，declared 主张仅声明来源
CLAIMS = [
    {"subject": "gateway timeout", "predicate": "value", "value": "30s",
     "source_refs": ["config:gateway@1"], "verified": True},
    {"subject": "retry budget", "predicate": "value", "value": "3",
     "source_refs": [DRIFT_REF], "verified": True},
    {"subject": "legacy runbook", "predicate": "pointer", "value": "wiki:runbook-7",
     "source_refs": ["note:runbook@1"], "verified": False},
]
BASE_STATE = {
    "config:gateway@1": {"revision": 1, "value": "30s"},
    DRIFT_REF: {"revision": 1, "value": "3"},
}
# 确定性 scripted usage：真实台账格式演示（judgement 类别 → tokens）
COST_TOKENS = {"supports": 120, "absent": 80, "contradicts": 340, "error": 150}


# ── 确定性输入与 fake source ─────────────────────────────────────────────────

def apply_drift(state: dict) -> dict:
    """确定性地制造一次源漂移：revision+1、值变更（快照比对会看到）。"""
    drifted = dict(state[DRIFT_REF])
    drifted["revision"] = int(drifted["revision"]) + 1
    drifted["value"] = "5"
    state[DRIFT_REF] = drifted
    return state


def input_digest(state: dict) -> str:
    return sha256_json({"space": SPACE, "claims": CLAIMS, "source_state": state})


def make_source_reader(state: dict):
    """确定性 fake source reader（与 record_verified_claim 同形端口）。"""
    def read(session, ref, **scope):
        entry = state[ref]  # 未知 ref = 来源不可用（上抛即裁决输入）
        full = {key: scope.get(key, "")
                for key in ("org_id", "space_id", "owner_scope")}
        return {"ref": ref,
                "sha256": sha256_json({"ref": ref, "value": entry["value"], **full}),
                "revision": int(entry["revision"]), **full}
    return read


def build_wiki(session_factory, *, state: dict) -> dict:
    """合成知识库：verified 主张存真实快照；返回 subject → claim_id。"""
    ids = {}
    with session_factory.begin() as session:
        reader = make_source_reader(state)
        for spec in CLAIMS:
            fields = dict(space_id=SPACE, owner_scope="public",
                          subject=spec["subject"], predicate=spec["predicate"],
                          value=spec["value"], source_refs=spec["source_refs"],
                          confidence_basis="synthetic-seed")
            if spec["verified"]:
                row = knowledge_service.record_verified_claim(
                    session, source_reader=reader, **fields)
            else:
                row = knowledge_service.record_claim(session, **fields)
            ids[spec["subject"]] = row.claim_id
    return ids


# ── 成本：真实预算台账的规范化投影 ────────────────────────────────────────────

def ledger_cost_records(session, *, run_id: str) -> list[dict]:
    """读真实 BudgetReservation 行，规范化为可哈希记录（无 reservation_id/时间戳）。"""
    rows = session.scalars(select(BudgetReservation).where(
        BudgetReservation.run_id == run_id)).all()
    records = [{"category": row.category, "call_id": row.call_id, "kind": row.kind,
                "reserved_tokens": int(row.reserved_tokens),
                "used_tokens": int(row.used_tokens),
                "usage_status": row.usage_status,
                "intent_status": row.intent_status,
                "outcome": row.outcome} for row in rows]
    records.sort(key=lambda rec: rec["call_id"])
    return records


def _cost_summary(records: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    for rec in records:
        by_status[rec["usage_status"]] = (by_status.get(rec["usage_status"], 0)
                                          + rec["used_tokens"])
    return {"records": records,
            "total_tokens": sum(rec["used_tokens"] for rec in records),
            "by_usage_status": dict(sorted(by_status.items()))}


def _costed_judge(base, session, *, space_id: str, run_id: str):
    """给确定性裁决包一层真实台账记账：reserve → judge → settle（恰好结算一次）。

    call_id 用 subject|predicate 稳定键（claim_id 是 uuid，不进可哈希产物）。
    """
    def judge(item):
        evidence = base(item)
        claim = session.get(KnowledgeClaim, item)
        key = f"{claim.subject}|{claim.predicate}" if claim is not None else item
        tokens = COST_TOKENS[evidence.verdict]
        reservation = budget.reserve(
            session, run_id=run_id, category="dreaming", tokens=tokens,
            call_id=f"dreaming|{space_id}|{key}")
        budget.settle(session, reservation.reservation_id, used_tokens=tokens,
                      usage_status="recorded")
        return evidence
    return judge


# ── 归一化：uuid/时间戳不进产物 ───────────────────────────────────────────────

def _id_key_map(session) -> dict:
    rows = session.scalars(select(KnowledgeClaim)).all()
    return {row.claim_id: f"{row.subject}|{row.predicate}" for row in rows}


def _normalize_report(report: dict, id_key: dict) -> dict:
    normalized = dict(report)
    normalized["verdicts"] = {id_key[item]: verdict for item, verdict
                              in sorted(report["verdicts"].items())}
    normalized["manifest_sha256"] = sha256_json(
        sorted(id_key[item] for item in report["verdicts"]))
    return normalized


def _claim_summary(session, *, space_id: str) -> list[dict]:
    rows = session.scalars(select(KnowledgeClaim).where(
        KnowledgeClaim.space_id == space_id)).all()
    by_id = {row.claim_id: row for row in rows}
    out = []
    for row in sorted(rows, key=lambda r: (r.subject, r.predicate, r.value, r.state)):
        predecessor = by_id.get(row.supersedes)
        out.append({"subject": row.subject, "predicate": row.predicate,
                    "value": row.value, "state": row.state,
                    "source_revision": int(row.source_revision or 0),
                    "evidence_level": row.evidence_level,
                    "supersedes_key": (f"{predecessor.subject}|{predecessor.predicate}"
                                       if predecessor else "")})
    return out


def _owner_value(session, claim_id: str, state: dict) -> str:
    """责任人显式新值：取漂移源当前值（不猜答案；无 ref 可取则空 → 采用被拒）。"""
    claim = session.get(KnowledgeClaim, claim_id)
    for ref in json.loads(claim.source_refs_json or "[]"):
        if ref in state:
            return state[ref]["value"]
    return ""


# ── 两臂执行 ──────────────────────────────────────────────────────────────────

def run_arm(*, arm: str, date_str: str = DATE,
            prepared_by: str = "paired-offline") -> dict:
    """单臂运行：全新内存库 + 同一输入；返回归一化产物（可哈希）。

    时序：主张按漂移前来源登记（快照 = rev1）→ 源确定性漂移（rev2）→
    两臂看到的输入都是"漂移后的源世界"；on 臂的 recheck 裁决抓漂移。
    """
    base_state = copy.deepcopy(BASE_STATE)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    build_wiki(factory, state=base_state)
    state = apply_drift(copy.deepcopy(base_state))
    reader = make_source_reader(state)
    run_id = f"dreaming-nightly|{date_str}|{SPACE}"
    report, adoption_outcomes, adopted = None, [], 0
    if arm == "dreaming-on":
        outcome = dreaming_nightly.run_nightly(
            factory, spaces=[SPACE], prepared_by=prepared_by,
            judge_factory=lambda session, space_id: _costed_judge(
                dreaming_nightly.SnapshotRecheckJudge(session, reader),
                session, space_id=SPACE, run_id=run_id),
            now=datetime.strptime(date_str, "%Y-%m-%d"))
        entry = outcome["spaces"][0]
        raw_report = entry.get("report")
        if raw_report is not None:
            job_id, revision = entry["job_id"], entry["revision"]
            # 显式采用：责任人固定，新值 = 漂移源当前值（owning service 语义）
            with factory.begin() as session:
                for item, verdict in raw_report["verdicts"].items():
                    if not verdict["candidate"]:
                        continue
                    try:
                        res = reconcile.adopt_candidate(
                            session, job_id, item=item,
                            new_value=_owner_value(session, item, state),
                            actor=ADOPT_ACTOR, expected_revision=revision)
                        adoption_outcomes.append({"item": item,
                                                  "reason": res["reason"]})
                    except reconcile.ReconcileError as exc:
                        adoption_outcomes.append({"item": item, "reason": exc.code})
            with factory.begin() as session:
                id_key = _id_key_map(session)
                report = _normalize_report(raw_report, id_key)
                adoption_outcomes = [{"item": id_key.get(o["item"], o["item"]),
                                      "reason": o["reason"]}
                                     for o in sorted(adoption_outcomes,
                                                     key=lambda o: o["item"])]
            adopted = sum(1 for o in adoption_outcomes if o["reason"] == "adopted")
    with factory.begin() as session:
        claims = _claim_summary(session, space_id=SPACE)
        cost = _cost_summary(ledger_cost_records(session, run_id=run_id)
                             if arm == "dreaming-on" else [])
    artifact = {"kind": "paired-dreaming-arm", "arm": arm, "date": date_str,
                "space_id": SPACE, "report": report, "claims": claims,
                "counts": {"claims": len(claims),
                           "candidates": (report["counts"]["candidates"]
                                          if report else 0),
                           "adoptions": adopted,
                           "superseded": sum(1 for c in claims
                                             if c["state"] == "superseded")},
                "adoption_outcomes": adoption_outcomes,
                "cost": cost}
    engine.dispose()
    return artifact


def _comparison_md(comparison: dict) -> str:
    lines = ["# Dreaming 离线配对实验（同输入两臂，离线确定性）", "",
             f"- input_digest: `{comparison['input_digest']}`",
             f"- date: {comparison['date']}（固定日期，重跑哈希可复现）", "",
             "| 臂 | artifact sha256 | candidates | adoptions | superseded | tokens |",
             "|---|---|---|---|---|---|"]
    for arm in ("dreaming-off", "dreaming-on"):
        bundle = comparison["arms"][arm]
        counts = bundle["counts"]
        lines.append(f"| {arm} | `{bundle['artifact_sha256'][:16]}` | "
                     f"{counts['candidates']} | {counts['adoptions']} | "
                     f"{counts['superseded']} | {bundle['cost']['total_tokens']} |")
    lines += ["",
              f"- candidates Δ(on-off): {comparison['delta']['candidates_on_minus_off']}",
              f"- tokens Δ(on-off): {comparison['delta']['tokens_on_minus_off']}",
              "",
              "on 臂成本 = 真实预算台账（budget.reserve/settle, category=dreaming）的规范化投影；"
              "off 臂无夜跑调用，空记录即诚实。两臂产物哈希重跑一致（确定性）。",
              ""]
    return "\n".join(lines)


def run_experiment(*, out_dir: str | Path | None = None,
                   tag: str = "nightly-check", date_str: str = DATE) -> dict:
    """两臂同输入各跑一次，落盘产物与对比，返回 comparison dict。"""
    drifted = apply_drift(copy.deepcopy(BASE_STATE))
    digest = input_digest(drifted)
    bundles = {}
    for arm in ("dreaming-off", "dreaming-on"):
        artifact = run_arm(arm=arm, date_str=date_str)
        bundles[arm] = (artifact, sha256_json(artifact))
    run_dir = Path(out_dir or DEFAULT_OUT) / f"{tag}-{digest[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    for arm, (artifact, _sha) in bundles.items():
        (run_dir / f"arm-{arm}.json").write_text(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
    comparison = {"kind": "paired-dreaming-comparison", "date": date_str,
                  "input_digest": digest,
                  "arms": {arm: {"artifact_sha256": sha,
                                 "counts": artifact["counts"],
                                 "cost": artifact["cost"]}
                           for arm, (artifact, sha) in bundles.items()},
                  "delta": {"candidates_on_minus_off":
                            (bundles["dreaming-on"][0]["counts"]["candidates"]
                             - bundles["dreaming-off"][0]["counts"]["candidates"]),
                            "tokens_on_minus_off":
                            (bundles["dreaming-on"][0]["cost"]["total_tokens"]
                             - bundles["dreaming-off"][0]["cost"]["total_tokens"])}}
    (run_dir / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    (run_dir / "comparison.md").write_text(_comparison_md(comparison),
                                           encoding="utf-8")
    return comparison


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dreaming 离线配对实验（同输入两臂确定性对比）")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--tag", default="nightly-check")
    parser.add_argument("--date", default=DATE,
                        help="固定日期 YYYY-MM-DD（默认固定值保证重跑哈希一致）")
    args = parser.parse_args(argv)
    comparison = run_experiment(out_dir=args.out, tag=args.tag, date_str=args.date)
    print(json.dumps({"ok": True, "input_digest": comparison["input_digest"],
                      "arms": {arm: bundle["artifact_sha256"]
                               for arm, bundle in comparison["arms"].items()}},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
