"""DE-16 owning source（Experience）→ 知识主张 provenance 桥。

只读源、幂等登记、版本升级 supersede、停用/过期/撤权失效、私有可见性；
Experience 仍是唯一写者，召回链不变。全部离线。
"""
from datetime import timedelta
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from evoharness.clock import utcnow
from evoharness.knowledge import experience_bridge, service as knowledge_service
from evoharness.knowledge.models import KnowledgeClaim
from evoharness.models import Base, Experience, ExperienceStatus


def _experience(session, *, version=1, status=ExperienceStatus.APPROVED_AUTO,
                visibility="private", valid_until=None, answer="先重启网关"):
    row = Experience(tenant="t", owner="alice", intent_id="intent-1",
                     version=version, question_pattern="网关超时怎么处理",
                     canonical_answer=answer, status=status,
                     visibility=visibility, source_ids='["src-1"]',
                     source_message_id="m-1", valid_until=valid_until)
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def env():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def _claims(session):
    return list(session.scalars(select(KnowledgeClaim)))


def test_sync_registers_claim_with_provenance(env):
    with env.begin() as session:
        row = _experience(session)
        outcome = experience_bridge.sync_experience(session, row, space_id="space")
        assert outcome["action"] == "recorded"
        claim = session.scalar(select(KnowledgeClaim))
        assert claim.subject == "experience:intent-1"
        assert claim.owner_scope == "alice"          # 私有经验不公开
        refs = json.loads(claim.source_refs_json)
        assert "experience:intent-1@1" in refs
        assert "message:m-1" in refs and "source:src-1" in refs
        assert claim.value == "先重启网关"


def test_sync_is_idempotent(env):
    with env.begin() as session:
        row = _experience(session)
        experience_bridge.sync_experience(session, row)
        second = experience_bridge.sync_experience(session, row)
        assert second["action"] == "unchanged"
        assert int(session.scalar(select(func.count(KnowledgeClaim.claim_id))) or 0) == 1


def test_new_version_supersedes_old_with_chain(env):
    with env.begin() as session:
        old = _experience(session, version=1, answer="重启网关")
        experience_bridge.sync_experience(session, old)
        new = _experience(session, version=2, answer="先查限流再重启")
        outcome = experience_bridge.sync_experience(session, new)
        assert outcome["action"] == "superseded"
        claims = _claims(session)
        assert len(claims) == 2
        fresh = session.scalar(select(KnowledgeClaim).where(
            KnowledgeClaim.source_revision == 2))
        assert fresh.value == "先查限流再重启"
        assert fresh.supersedes
        old_claim = session.scalar(select(KnowledgeClaim).where(
            KnowledgeClaim.source_revision == 1))
        assert old_claim.state in {"superseded", "stale"}


def test_paused_or_expired_experience_invalidates_claims(env):
    with env.begin() as session:
        row = _experience(session)
        experience_bridge.sync_experience(session, row)
        row.status = ExperienceStatus.PAUSED
        outcome = experience_bridge.sync_experience(session, row)
        assert outcome["action"] == "invalidated" and outcome["claim_ids"]
        claim = session.scalar(select(KnowledgeClaim))
        assert claim.state == "stale" and "stale:experience_paused" in claim.confidence_basis

    with env.begin() as session:
        expired = _experience(session, version=3,
                              valid_until=utcnow() - timedelta(seconds=1))
        outcome = experience_bridge.sync_experience(session, expired)
        assert outcome["action"] == "invalidated"


def test_public_experience_claim_is_public(env):
    with env.begin() as session:
        row = _experience(session, visibility="public")
        experience_bridge.sync_experience(session, row)
        claim = session.scalar(select(KnowledgeClaim))
        assert claim.owner_scope == "public"
        found = knowledge_service.search(session, space_id="", subject="experience:intent-1")
        assert len(found["claims"]) == 1


def test_sync_all_picks_latest_version_and_counts(env):
    with env.begin() as session:
        _experience(session, version=1, answer="v1")
        _experience(session, version=2, answer="v2")
        counts = experience_bridge.sync_all(session, tenant="t", space_id="space")
        assert counts["recorded"] == 1          # 只同步最新版本
        claim = session.scalar(select(KnowledgeClaim))
        assert claim.source_revision == 2
        again = experience_bridge.sync_all(session, tenant="t", space_id="space")
        assert again["unchanged"] == 1
