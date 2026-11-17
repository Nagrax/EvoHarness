"""DE-17 对账候选采用链与 Dreaming 单轮编排。

整理器只产候选（propose_supersede + 证据），不写新值；采用必须由责任人经
owning service 显式提交，旧主张 superseded、历史与来源保留；重复采用幂等；
未收口/过期 revision/缺值缺证据均拒绝。Dreaming 单轮只整理，不自动采用。
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from evoharness.knowledge import reconcile
from evoharness.knowledge import service as knowledge_service
from evoharness.knowledge.models import KnowledgeClaim
from evoharness.models import Base

CONTRA = reconcile.Evidence("contradicts", ["commit:abc123"],
                            "配置已改为 5s")
SUPPORTS = reconcile.Evidence("supports", ["config:gateway@2"], "一致")


@pytest.fixture()
def env():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        claim = knowledge_service.record_claim(
            session, space_id="space", subject="gateway timeout",
            predicate="value", value="30s", source_refs=["config:gateway@1"],
            confidence_basis="baseline")
        claim_id = claim.claim_id
    yield factory, claim_id
    engine.dispose()


def _job_with_candidate(session, claim_id, evidence=CONTRA):
    job = reconcile.start_job(session, space_id="space", prepared_by="dreamer",
                              input_manifest=[claim_id], expected_revision=1)
    reconcile.process(session, job.job_id, item=claim_id,
                      judge=lambda item: evidence)
    return job


def _finished_candidate(session, claim_id):
    job = _job_with_candidate(session, claim_id)
    report = reconcile.finish(session, job.job_id, expected_revision=1)
    assert report["completed"] is True
    assert report["candidates"] == [claim_id]
    return job


def test_candidate_does_not_change_claim_until_adopted(env):
    factory, claim_id = env
    with factory.begin() as session:
        _finished_candidate(session, claim_id)
        claim = session.get(KnowledgeClaim, claim_id)
        assert claim.state == "active" and claim.value == "30s"
    with factory() as session:
        assert int(session.scalar(select(func.count(KnowledgeClaim.claim_id))) or 0) == 1


def test_adopt_candidate_supersedes_with_provenance(env):
    factory, claim_id = env
    with factory.begin() as session:
        job = _finished_candidate(session, claim_id)
        outcome = reconcile.adopt_candidate(
            session, job.job_id, item=claim_id, new_value="5s",
            actor="maintainer", expected_revision=1)
        assert outcome["reason"] == "adopted"
        claim = session.get(KnowledgeClaim, claim_id)
        assert claim.state == "superseded"
        fresh = session.scalar(select(KnowledgeClaim).where(
            KnowledgeClaim.supersedes == claim_id))
        assert fresh is not None and fresh.value == "5s"
        import json as _json
        assert _json.loads(fresh.source_refs_json) == ["commit:abc123"]
        assert "adopted-by:maintainer" in fresh.confidence_basis
    with factory() as session:
        assert int(session.scalar(select(func.count(KnowledgeClaim.claim_id))) or 0) == 2


def test_adoption_is_idempotent(env):
    factory, claim_id = env
    with factory.begin() as session:
        job = _finished_candidate(session, claim_id)
        first = reconcile.adopt_candidate(
            session, job.job_id, item=claim_id, new_value="5s",
            actor="maintainer", expected_revision=1)
        second = reconcile.adopt_candidate(
            session, job.job_id, item=claim_id, new_value="5s",
            actor="maintainer", expected_revision=1)
        assert second["reason"] == "already_adopted"
        assert second["claim_id"] == first["claim_id"]
    with factory() as session:
        assert int(session.scalar(select(func.count(KnowledgeClaim.claim_id))) or 0) == 2


def test_adoption_requires_explicit_value_actor_and_evidence(env):
    factory, claim_id = env
    with factory.begin() as session:
        job = _finished_candidate(session, claim_id)
        with pytest.raises(reconcile.ReconcileError, match="adoption_value_required"):
            reconcile.adopt_candidate(session, job.job_id, item=claim_id,
                                      new_value="", actor="maintainer",
                                      expected_revision=1)
        with pytest.raises(reconcile.ReconcileError, match="adoption_actor_required"):
            reconcile.adopt_candidate(session, job.job_id, item=claim_id,
                                      new_value="5s", actor="",
                                      expected_revision=1)
        with pytest.raises(reconcile.ReconcileError, match="adoption_evidence_required"):
            reconcile.adopt_candidate(session, job.job_id, item=claim_id,
                                      new_value="5s", actor="maintainer",
                                      source_refs=[], expected_revision=1)


def test_adoption_rejects_stale_revision_and_unfinished_job(env):
    factory, claim_id = env
    with factory.begin() as session:
        job = _finished_candidate(session, claim_id)
        with pytest.raises(reconcile.ReconcileError, match="stale_reconcile_revision"):
            reconcile.adopt_candidate(session, job.job_id, item=claim_id,
                                      new_value="5s", actor="maintainer",
                                      expected_revision=2)
        pending = _job_with_candidate(session, claim_id)
        with pytest.raises(reconcile.ReconcileError, match="job_not_finished"):
            reconcile.adopt_candidate(session, pending.job_id, item=claim_id,
                                      new_value="5s", actor="maintainer",
                                      expected_revision=1)


def test_adoption_requires_a_candidate(env):
    factory, claim_id = env
    with factory.begin() as session:
        job = reconcile.start_job(session, space_id="space2", prepared_by="dreamer",
                                  input_manifest=[claim_id], expected_revision=1)
        reconcile.process(session, job.job_id, item=claim_id,
                          judge=lambda item: SUPPORTS)
        reconcile.finish(session, job.job_id, expected_revision=1)
        with pytest.raises(reconcile.ReconcileError, match="candidate_not_found"):
            reconcile.adopt_candidate(session, job.job_id, item=claim_id,
                                      new_value="5s", actor="maintainer",
                                      expected_revision=1)


def test_dreaming_cycle_reports_candidates_without_auto_adoption(env):
    factory, claim_id = env
    with factory.begin() as session:
        second = knowledge_service.record_claim(
            session, space_id="space", subject="retry budget",
            predicate="value", value="3", source_refs=["config:retry@1"])
        second_id = second.claim_id
        verdicts = {claim_id: CONTRA, second_id: SUPPORTS}
        report = reconcile.run_dreaming_cycle(
            session, space_id="space", prepared_by="dreamer",
            input_manifest=[claim_id, second_id], expected_revision=1,
            judge=lambda item: verdicts[item])
        assert report["completed"] is True
        assert report["candidates"] == [claim_id]
        assert session.get(KnowledgeClaim, claim_id).state == "active"
        assert session.get(KnowledgeClaim, second_id).state == "active"
