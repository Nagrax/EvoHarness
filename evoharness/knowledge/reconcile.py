"""增量对账工作流：固定输入、证据裁决、候选变更集、单写者（DE-17）。

- 没有提及不等于推翻（absent ≠ contradicts）
- 输出是写候选区的变更集，不直接改已发布事实；采用由 owning service 校验
- 少处理一份不能报告全完成；两个整理者不覆盖（同空间单 pending）
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, Session

from evoharness.models import Base
from evoharness.clock import utcnow


class ReconciliationJob(Base):
    __tablename__ = "knowledge_reconciliation_jobs"
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(64), index=True)
    prepared_by: Mapped[str] = mapped_column(String(128), default="")
    # 固定的输入清单：待对账 claim/source 引用与快照指纹
    input_manifest_json: Mapped[str] = mapped_column(Text, default="[]")
    expected_revision: Mapped[int] = mapped_column(Integer, default=1)
    results_json: Mapped[str] = mapped_column(Text, default="{}")
    # pending / partial / done / failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReconcileError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class Evidence:
    verdict: str        # supports / contradicts / absent / error
    source_refs: list[str]
    note: str = ""


def start_job(session: Session, *, space_id: str, prepared_by: str,
              input_manifest: list[str], expected_revision: int) -> ReconciliationJob:
    """固定输入清单开一个对账作业；同空间同时只允许一个未收口作业（单写者）。

    pending=进行中（阻塞新作业）；partial=已收口但有缺口，可经原作业续处理，
    不阻塞下一轮增量对账。
    """
    if not input_manifest:
        raise ReconcileError("reconcile_input_required")
    pending = session.scalar(select(ReconciliationJob).where(
        ReconciliationJob.space_id == space_id,
        ReconciliationJob.status == "pending"))
    if pending is not None:
        raise ReconcileError("reconciler_already_active")
    job = ReconciliationJob(job_id=uuid4().hex, space_id=space_id,
                            prepared_by=prepared_by,
                            input_manifest_json=json.dumps(sorted(input_manifest), ensure_ascii=False),
                            expected_revision=expected_revision)
    session.add(job)
    session.flush()
    return job


def process(session: Session, job_id: str, *, item: str,
            judge) -> dict:
    """逐项对账：judge(item) -> Evidence。裁决进结果；候选变更集只记录不生效。

    - contradicts：生成 supersede 建议（引用证据，不猜新值）
    - absent：没有提及不等于推翻，保持原主张
    - error：失败可见，计入 failed
    """
    job = session.get(ReconciliationJob, job_id)
    if job is None:
        raise ReconcileError("job_not_found")
    if job.status not in {"pending", "partial"}:
        raise ReconcileError("job_closed")
    if item not in json.loads(job.input_manifest_json):
        raise ReconcileError("item_not_in_manifest")
    results = json.loads(job.results_json)
    if item in results and results[item]["verdict"] != "error":
        return results[item]        # 幂等：已有事实结论不覆盖；error 可重试
    evidence = judge(item)
    if not isinstance(evidence, Evidence):
        raise ReconcileError("evidence_verdict_required")
    if evidence.verdict not in {"supports", "contradicts", "absent", "error"}:
        raise ReconcileError("evidence_verdict_invalid")
    if evidence.verdict == "contradicts" and not evidence.source_refs:
        raise ReconcileError("contradiction_requires_evidence")
    entry = {"verdict": evidence.verdict,
             "source_refs": evidence.source_refs,
             "note": evidence.note,
             "candidate_revision": None}
    if evidence.verdict == "contradicts":
        # 候选变更集：建议 supersede + 证据；不写新值、不直接生效
        entry["candidate_revision"] = {
            "action": "propose_supersede", "target_item": item,
            "evidence_refs": evidence.source_refs,
            "reason": evidence.note,
        }
    results[item] = entry
    job.results_json = json.dumps(results, ensure_ascii=False)
    processed = len(results)
    total = len(json.loads(job.input_manifest_json))
    job.status = "pending" if processed < total else job.status
    session.flush()
    return entry


def finish(session: Session, job_id: str, *, expected_revision: int) -> dict:
    """收口：少处理一份不能报全完成；CAS 版本不匹配拒绝。"""
    job = session.get(ReconciliationJob, job_id)
    if job is None:
        raise ReconcileError("job_not_found")
    if job.expected_revision != expected_revision:
        raise ReconcileError("stale_reconcile_revision")
    results = json.loads(job.results_json)
    manifest = json.loads(job.input_manifest_json)
    processed = [i for i in manifest if i in results]
    failed = [i for i in processed if results[i]["verdict"] == "error"]
    candidates = [i for i in processed if results[i]["candidate_revision"]]
    report = {"total": len(manifest), "processed": len(processed),
              "failed": failed, "candidates": candidates,
              "completed": len(processed) == len(manifest) and not failed}
    job.status = "done" if report["completed"] else "partial"
    job.finished_at = utcnow()
    session.flush()
    return report


def adopt_candidate(session: Session, job_id: str, *, item: str, new_value: str,
                    actor: str, expected_revision: int,
                    source_refs: list[str] | None = None,
                    confidence_basis: str = "") -> dict:
    """候选变更集的显式采用：由 owning service 校验后写入新主张并 supersede 旧主张。

    - 整理器只产候选（propose_supersede + 证据），不自作主张写新值；
    - 本入口要求已收口作业、显式新值/证据/责任人（actor），缺一拒绝；
    - 旧主张标 superseded、历史与来源保留（record_claim 语义）；
    - 幂等：同一候选重复采用返回首次采用的 claim_id，不重复写。
    """
    from evoharness.knowledge import service as knowledge_service
    from evoharness.knowledge.models import KnowledgeClaim

    job = session.get(ReconciliationJob, job_id)
    if job is None:
        raise ReconcileError("job_not_found")
    if job.expected_revision != expected_revision:
        raise ReconcileError("stale_reconcile_revision")
    if job.status not in {"done", "partial"}:
        raise ReconcileError("job_not_finished")
    results = json.loads(job.results_json)
    entry = results.get(item)
    if not entry or not entry.get("candidate_revision"):
        raise ReconcileError("candidate_not_found")
    existing = entry.get("adoption")
    if existing:
        return {"ok": True, "reason": "already_adopted", **existing}
    value = str(new_value or "").strip()
    if not value:
        raise ReconcileError("adoption_value_required")
    refs = [str(v) for v in (source_refs if source_refs is not None
                             else entry.get("source_refs") or [])
            if str(v).strip()]
    if not refs:
        raise ReconcileError("adoption_evidence_required")
    actor = str(actor or "").strip()
    if not actor:
        raise ReconcileError("adoption_actor_required")
    target = session.get(KnowledgeClaim, item)
    if target is None:
        raise ReconcileError("target_claim_missing")
    claim = knowledge_service.record_claim(
        session, org_id=target.org_id, space_id=target.space_id,
        owner_scope=target.owner_scope, subject=target.subject,
        predicate=target.predicate, value=value, source_refs=refs,
        source_revision=int(target.source_revision or 0) + 1,
        confidence_basis=confidence_basis or f"adopted-by:{actor}",
        supersedes=target.claim_id)
    adoption = {"claim_id": claim.claim_id, "adopted_by": actor,
                "value": value, "source_refs": refs,
                "at": utcnow().isoformat()}
    entry["adoption"] = adoption
    results[item] = entry
    job.results_json = json.dumps(results, ensure_ascii=False)
    session.flush()
    return {"ok": True, "reason": "adopted", **adoption}


def run_dreaming_cycle(session: Session, *, space_id: str, prepared_by: str,
                       input_manifest: list[str], expected_revision: int,
                       judge) -> dict:
    """Dreaming 单轮编排：开作业 → 逐项裁决 → 收口。

    只是可被任意调度器调用的一轮整理；候选不自动采用（采用必须走
    ``adopt_candidate`` 的显式责任人入口）。真实夜间/变更触发调度属部署。
    """
    job = start_job(session, space_id=space_id, prepared_by=prepared_by,
                    input_manifest=input_manifest,
                    expected_revision=expected_revision)
    processed: list[str] = []
    for item in sorted(input_manifest):
        process(session, job.job_id, item=item, judge=judge)
        processed.append(item)
    report = finish(session, job.job_id, expected_revision=expected_revision)
    return {"job_id": job.job_id, "processed": processed, **report}
