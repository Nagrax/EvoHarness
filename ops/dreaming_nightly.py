"""Dreaming 夜间调度部署入口（EVO-05 / DE-17 剩余）：时间触发 oneshot。

职责边界
--------
- 复用 Dreaming 单轮编排 ``reconcile.run_dreaming_cycle``（开作业 → 逐项裁决 → 收口），
  不新增状态、不新增表、不新增常驻进程；
- 候选**不自动采用**：夜间产物 = 报告 + 候选变更集，采用必须走
  ``reconcile.adopt_candidate`` 的显式责任人入口；
- 默认裁决 ``SnapshotRecheckJudge`` 是确定性代码：按已存来源快照逐 ref 重读比对，
  不调模型（declared 声明级 → absent，没有提及不等于推翻）；
- 单写者冲突（同空间已有 pending 对账作业）按"跳过并留痕"处理，不整夜失败；
- 报告规范化（排除 job_id 等易变字段，date 可注入固定）：同输入两轮字节级一致，
  供离线配对实验（eval/paired_dreaming_check.py）做哈希对比。

部署（一键，见 ops/NIGHTLY-DREAMING.md）
----------------------------------------
ops/evoharness-dreaming.timer（每日 02:30 + Persistent）
  → ops/evoharness-dreaming.service（Type=oneshot）
  → ``python -m ops.dreaming_nightly --db $DATABASE_URL --space <空间> ...``

组合口径：supervisor（DE-23）管常驻 worker；wakeups（DE-21）管事件驱动；
本入口只补"时间触发"这一条腿，互不替代。
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date as _date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.ids import sha256_json
from evoharness.knowledge import reconcile
from evoharness.knowledge.models import KnowledgeClaim
from evoharness.knowledge.provenance import read_experience_source
from evoharness.models import Base
from evoharness.migrations import migrate

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = REPO / "eval" / "runs" / "dreaming"
DEFAULT_PREPARED_BY = "dreaming-nightly"


class SnapshotRecheckJudge:
    """确定性重读裁决（模型无关）：已存快照逐 ref 重读，整字典比对。

    - declared 声明级（无快照）→ absent：没有提及不等于推翻，不产候选；
    - 全部一致 → supports；
    - 快照漂移（重读成功但内容不一致）→ contradicts（证据 = 漂移 refs，不猜新值）；
    - 来源读取失败 → error：读取失败不是矛盾证据，不伪造裁决；计入 failed、
      partial 可见、可重试（与 wakeups source_health"读取失败不与业务空态混同"
      同一口径）。
    reader 端口与 ``knowledge.service.record_verified_claim`` 同形：
    ``reader(session, ref, *, org_id, space_id, owner_scope) -> 快照 dict``。
    部署默认 ``provenance.read_experience_source``；离线实验注入确定性 fake reader。
    """

    def __init__(self, session: Session, source_reader=read_experience_source):
        self._session = session
        self._reader = source_reader

    def __call__(self, item: str) -> reconcile.Evidence:
        claim = self._session.get(KnowledgeClaim, item)
        if claim is None:
            return reconcile.Evidence("error", [], "claim_not_found")
        try:
            snapshots = json.loads(claim.source_snapshots_json or "[]")
            refs = json.loads(claim.source_refs_json or "[]")
        except ValueError:
            snapshots, refs = [], []
        if not snapshots or [s.get("ref") for s in snapshots] != list(refs):
            return reconcile.Evidence("absent", [],
                                      "declared_evidence_no_verifiable_snapshot")
        drifted: list[str] = []
        unreadable: list[str] = []
        for expected in snapshots:
            ref = expected.get("ref")
            try:
                actual = self._reader(
                    self._session, ref,
                    org_id=claim.org_id, space_id=claim.space_id,
                    owner_scope=claim.owner_scope)
            except Exception:  # noqa: BLE001 — 重读失败是裁决输入，不是崩溃
                unreadable.append(ref)
                continue
            if actual != expected:
                drifted.append(ref)
        if drifted:
            keys = sorted(set(drifted))
            return reconcile.Evidence("contradicts", keys,
                                      f"source_drift:{','.join(keys)}")
        if unreadable:
            keys = sorted(set(unreadable))
            return reconcile.Evidence("error", [],
                                      f"source_unreadable:{','.join(keys)}")
        return reconcile.Evidence("supports", list(refs), "snapshot_current")


def build_manifest(session: Session, *, space_id: str, now=None) -> list[str]:
    """确定性枚举待对账主张：该空间 active/conflicting 且未过期，按 claim_id 排序。

    superseded/stale 不重扫（已被采用链/失效链处理）；过期主张由检索侧 expired_hint 呈现。
    """
    moment = now or utcnow()
    rows = session.scalars(select(KnowledgeClaim).where(
        KnowledgeClaim.space_id == space_id,
        KnowledgeClaim.state.in_(["active", "conflicting"]))).all()
    return sorted(row.claim_id for row in rows
                  if row.valid_until is None or row.valid_until > moment)


def nightly_revision(session: Session, manifest: list[str]) -> int:
    """CAS 版本 = manifest 内主张的 max(source_revision)，下限 1。

    语义：任何采用链 supersede 都会抬升该值——拿"最新 revision"去采用旧夜间
    报告的候选会被 CAS 拒绝（stale_reconcile_revision），必须用报告自带的 revision。
    """
    revisions = []
    for claim_id in manifest:
        claim = session.get(KnowledgeClaim, claim_id)
        if claim is not None:
            revisions.append(int(claim.source_revision or 0))
    return max(revisions or [0]) or 1


def nightly_report(session: Session, *, space_id: str, prepared_by: str,
                   date_str: str, manifest: list[str], revision: int,
                   result: dict) -> dict:
    """规范化夜间报告：排除 job_id/时钟时间戳，同输入两轮字节级一致。"""
    job = session.get(reconcile.ReconciliationJob, result["job_id"])
    results = json.loads(job.results_json)
    verdicts = {}
    for item in sorted(results):
        entry = results[item]
        verdicts[item] = {"verdict": entry["verdict"],
                          "source_refs": list(entry["source_refs"]),
                          "note": entry["note"],
                          "candidate": bool(entry["candidate_revision"])}
    return {"kind": "dreaming-nightly-report",
            "date": date_str,
            "space_id": space_id,
            "prepared_by": prepared_by,
            "revision": int(revision),
            "manifest_sha256": sha256_json(sorted(manifest)),
            "verdicts": verdicts,
            "counts": {"total": int(result["total"]),
                       "processed": int(result["processed"]),
                       "failed": len(result["failed"]),
                       "candidates": len(result["candidates"]),
                       "completed": bool(result["completed"])}}


def run_nightly(session_factory, *, spaces: list[str],
                prepared_by: str = DEFAULT_PREPARED_BY,
                source_reader=read_experience_source,
                judge_factory=None, now=None,
                report_dir: str | Path | None = None) -> dict:
    """逐空间跑一轮 Dreaming；返回可审计结果并（可选）落报告文件。

    - ``judge_factory(session, space_id)`` 可注入（配对实验用它包一层成本记账）；
      默认 = SnapshotRecheckJudge(source_reader)。
    - manifest 为空 → status=empty；单写者冲突 → status=skipped + reason（均留痕）。
    - 报告文件名 ``<date>-<space>.json``（date 注入固定时文件内容字节级确定）。
    """
    if isinstance(now, datetime):
        moment, date_str = now, now.date().isoformat()
    elif isinstance(now, _date):
        moment, date_str = datetime(now.year, now.month, now.day), now.isoformat()
    else:
        moment = utcnow()
        date_str = moment.date().isoformat()
    if judge_factory is None:
        def judge_factory(session, space_id):  # noqa: ANN001 — 固定签名
            return SnapshotRecheckJudge(session, source_reader)

    entries: list[dict] = []
    for space_id in sorted(dict.fromkeys(spaces)):
        entry: dict = {"space_id": space_id}
        with session_factory.begin() as session:
            try:
                manifest = build_manifest(session, space_id=space_id, now=moment)
                if not manifest:
                    entry.update(status="empty", report=None, reason="manifest_empty")
                else:
                    revision = nightly_revision(session, manifest)
                    judge = judge_factory(session, space_id)
                    result = reconcile.run_dreaming_cycle(
                        session, space_id=space_id, prepared_by=prepared_by,
                        input_manifest=manifest, expected_revision=revision,
                        judge=judge)
                    report = nightly_report(
                        session, space_id=space_id, prepared_by=prepared_by,
                        date_str=date_str, manifest=manifest, revision=revision,
                        result=result)
                    entry.update(status="done" if result["completed"] else "partial",
                                 report=report, job_id=result["job_id"],
                                 revision=revision)
            except reconcile.ReconcileError as exc:
                entry.update(status="skipped", report=None, reason=exc.code)
        entries.append(entry)
    if report_dir is not None:
        out = Path(report_dir)
        out.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            if entry.get("report") is not None:
                (out / f"{date_str}-{entry['space_id']}.json").write_text(
                    json.dumps(entry["report"], ensure_ascii=False,
                               sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")
    return {"ok": True, "date": date_str, "spaces": entries}


# ---------------------------------------------------------------------------
# CLI（systemd oneshot 调用）
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dreaming 夜间对账 oneshot 入口（候选不自动采用）")
    parser.add_argument("--db", default=os.environ.get("EVOHARNESS_DATABASE_URL",
                                                       "sqlite:///data/agent.db"))
    parser.add_argument("--space", action="append", required=True,
                        help="待对账业务空间（可重复）")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--prepared-by", default=DEFAULT_PREPARED_BY)
    parser.add_argument("--date", default="",
                        help="固定日期 YYYY-MM-DD（确定性演练；默认当天）")
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(args.db, future=True)
    try:
        Base.metadata.create_all(bind=engine)
        migrate(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        now = datetime.strptime(args.date, "%Y-%m-%d") if args.date else None
        outcome = run_nightly(factory, spaces=args.space,
                              prepared_by=args.prepared_by, now=now,
                              report_dir=args.report_dir)
    finally:
        engine.dispose()
    print(json.dumps(outcome, ensure_ascii=False, indent=2, default=str))
    return 0 if outcome["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
