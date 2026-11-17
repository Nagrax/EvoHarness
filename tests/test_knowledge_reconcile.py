"""DE-17：增量对账——证据裁决、候选变更集、单写者与全完成口径。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evoharness.knowledge import reconcile
from evoharness.knowledge.reconcile import Evidence, ReconcileError
from evoharness.models import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


def test_single_writer_rejects_second_active_job(session):
    first = reconcile.start_job(session, space_id="support", prepared_by="dreaming-1",
                                input_manifest=["claim:a", "claim:b"], expected_revision=1)
    with pytest.raises(ReconcileError, match="reconciler_already_active"):
        reconcile.start_job(session, space_id="support", prepared_by="dreaming-2",
                            input_manifest=["claim:c"], expected_revision=1)
    # 完成后可开新作业
    reconcile.finish(session, first.job_id, expected_revision=1)
    second = reconcile.start_job(session, space_id="support", prepared_by="dreaming-2",
                                 input_manifest=["claim:c"], expected_revision=2)
    assert second.job_id != first.job_id


def test_wrong_memory_corrected_by_evidence_but_absence_is_not_refutation(session):
    job = reconcile.start_job(session, space_id="support", prepared_by="dreaming",
                              input_manifest=["claim:wrong", "claim:unmentioned"], expected_revision=1)
    corrected = reconcile.process(session, job.job_id, item="claim:wrong",
                                  judge=lambda i: Evidence("contradicts",
                                                           ["msg:owner-fix@7"],
                                                           "权威决定为 15 天"))
    assert corrected["verdict"] == "contradicts"
    assert corrected["candidate_revision"]["action"] == "propose_supersede"
    assert corrected["candidate_revision"]["evidence_refs"] == ["msg:owner-fix@7"]
    # 没有提及不等于推翻：absent 不产生候选修订
    absent = reconcile.process(session, job.job_id, item="claim:unmentioned",
                               judge=lambda i: Evidence("absent", []))
    assert absent["verdict"] == "absent" and absent["candidate_revision"] is None
    # 无证据的"矛盾"裁决被拒绝（冲突不能猜答案）——用未处理过的项验证
    job3 = reconcile.start_job(session, space_id="qa", prepared_by="dreaming",
                               input_manifest=["claim:e"], expected_revision=1)
    with pytest.raises(ReconcileError, match="contradiction_requires_evidence"):
        reconcile.process(session, job3.job_id, item="claim:e",
                          judge=lambda i: Evidence("contradicts", []))
    # 幂等：重复处理同一项不覆盖已有结论
    again = reconcile.process(session, job.job_id, item="claim:wrong",
                              judge=lambda i: Evidence("absent", []))
    assert again["verdict"] == "contradicts"


def test_partial_processing_never_reports_complete(session):
    job = reconcile.start_job(session, space_id="support", prepared_by="dreaming",
                              input_manifest=["claim:a", "claim:b", "claim:c"], expected_revision=4)
    reconcile.process(session, job.job_id, item="claim:a",
                      judge=lambda i: Evidence("supports", ["doc:v1"]))
    report = reconcile.finish(session, job.job_id, expected_revision=4)
    # 只处理 1/3：partial，不能宣称全完成
    assert not report["completed"] and job.status == "partial"
    # CAS：过期 revision 拒绝
    with pytest.raises(ReconcileError, match="stale_reconcile_revision"):
        reconcile.finish(session, job.job_id, expected_revision=3)
    # 处理完剩余（含一个失败项）仍是 partial；partial 可继续补处理
    reconcile.process(session, job.job_id, item="claim:b",
                      judge=lambda i: Evidence("supports", ["doc:v1"]))
    reconcile.process(session, job.job_id, item="claim:c",
                      judge=lambda i: Evidence("error", [], note="证据源不可读"))
    report = reconcile.finish(session, job.job_id, expected_revision=4)
    assert not report["completed"] and report["failed"] == ["claim:c"]
    # partial 作业可重开失败项并修复后全完成
    reconcile.process(session, job.job_id, item="claim:c",
                      judge=lambda i: Evidence("supports", ["doc:v1"]))
    report = reconcile.finish(session, job.job_id, expected_revision=4)
    assert report["completed"] and job.status == "done"
    # 完成作业不再接受处理
    with pytest.raises(ReconcileError, match="job_closed"):
        reconcile.process(session, job.job_id, item="claim:a",
                          judge=lambda i: Evidence("supports", []))


def test_manifest_scope_enforced(session):
    job = reconcile.start_job(session, space_id="support", prepared_by="dreaming",
                              input_manifest=["claim:a"], expected_revision=1)
    with pytest.raises(ReconcileError, match="item_not_in_manifest"):
        reconcile.process(session, job.job_id, item="claim:not-in",
                          judge=lambda i: Evidence("supports", []))
    with pytest.raises(ReconcileError, match="reconcile_input_required"):
        reconcile.start_job(session, space_id="other", prepared_by="dreaming",
                            input_manifest=[], expected_revision=1)
