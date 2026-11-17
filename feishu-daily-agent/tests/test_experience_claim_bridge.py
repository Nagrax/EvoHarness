"""DE-16 生命周期自动同步：Experience 发布/暂停 → 知识主张登记/失效。"""
from sqlalchemy import select

from app.core.database import engine, session_scope
from app.memory import experience as exp
from evoharness.models import Base, Experience, ExperienceStatus
from evoharness.knowledge.models import KnowledgeClaim
import evoharness.knowledge.models  # noqa: F401 — 注册 knowledge_claims 表


def test_publish_and_pause_sync_knowledge_claim():
    Base.metadata.create_all(engine)          # 飞书库补知识表（生产由迁移建）
    with session_scope() as session:
        row = Experience(tenant="tenant", owner="alice",
                         intent_id="intent-bridge", version=1,
                         question_pattern="如何重启网关",
                         canonical_answer="先查限流再重启",
                         status=ExperienceStatus.CANDIDATE,
                         visibility="private", source_ids='["src-1"]',
                         source_message_id="m-1")
        session.add(row)
        session.flush()
        exp.publish(session, row.id, actor="owner")
        claim = session.scalar(select(KnowledgeClaim).where(
            KnowledgeClaim.subject == "experience:intent-bridge"))
        assert claim is not None and claim.state in {"active", "conflicting"}
        assert "experience:intent-bridge@1" in claim.source_refs_json
        exp.pause(session, row.id, actor="owner", reason="manual pause")
        session.flush()
        claim = session.scalar(select(KnowledgeClaim).where(
            KnowledgeClaim.subject == "experience:intent-bridge"))
        assert claim.state == "stale"
