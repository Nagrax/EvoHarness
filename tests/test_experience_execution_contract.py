"""Source-owned Experience reaches the existing Worker with a verifiable binding."""
import json

import pytest
from sqlalchemy import select

from test_execution_knowledge import env
from evoharness.models import Experience, Message, Source, Task
from evoharness.organization.models import ChannelBinding, Membership
from evoharness.knowledge.experience_bridge import sync_experience
from evoharness.knowledge.models import KnowledgeClaim
from evoharness.knowledge.retrieval import execution_index, read_indexed_claim, RetrievalError


def seed(session, *, version=1, answer='先检查网关限流'):
    member = session.scalar(select(Membership).where(Membership.principal_id == 'founder'))
    source = session.scalar(select(Source).where(Source.chat_id == 'contract-chat'))
    if source is None:
        source = Source(tenant='local', owner='founder', chat_id='contract-chat')
        session.add(source)
        session.flush()
        session.add(Message(tenant='local', message_id='contract-source',
            source_id=source.id, chat_id=source.chat_id, content_text='已批准网关处理办法',
            create_time=1))
        session.add(ChannelBinding(org_id='org-demo', space_id='space-support',
            tenant_id='local', app_id=member.app_id, channel_id=source.chat_id))
    row = Experience(tenant='local', owner='founder', intent_id='gateway-procedure',
        version=version, question_pattern='网关超时怎么处理', canonical_answer=answer,
        patterns_json=json.dumps(['网关超时', '网关故障处理']), visibility='public',
        status='approved_auto', source_ids='["contract-source"]')
    session.add(row)
    session.flush()
    return row, source


def index(session, task_id, run_id):
    task = session.get(Task, task_id)
    task.title = '网关超时怎么处理'
    task.goal = '请按网关故障处理说明答复'
    session.flush()
    return execution_index(session, task_id=task_id, run_id=run_id,
        org_id='org-demo', space_id='space-support', principal_id='ai-support')


def test_business_question_recalls_bound_source_without_internal_intent(env):
    app, _, task_id, run_id, _ = env
    with app.transaction() as session:
        row, _ = seed(session)
        result = sync_experience(session, row)
        claim = session.get(KnowledgeClaim, result['claim_id'])
        assert (claim.org_id, claim.space_id) == ('org-demo', 'space-support')
        assert claim.evidence_level == 'source_verified'
        assert json.loads(claim.source_snapshots_json)[0]['app_id']
        result = index(session, task_id, run_id)
        assert [c['claim_id'] for c in result['claims']] == [claim.claim_id]
        assert read_indexed_claim(session, index=result, claim_id=claim.claim_id)['value'] == row.canonical_answer


@pytest.mark.parametrize('mutation', ['revoke', 'edit', 'binding', 'membership', 'pause'])
def test_source_change_or_revocation_removes_projection_before_read(env, mutation):
    app, _, task_id, run_id, _ = env
    with app.transaction() as session:
        row, source = seed(session)
        result = sync_experience(session, row)
        original = index(session, task_id, run_id)
        if mutation == 'revoke':
            source.enabled = False
        elif mutation == 'edit':
            session.scalar(select(Message).where(Message.message_id == 'contract-source')).content_text = '已修改规范'
        elif mutation == 'binding':
            session.scalar(select(ChannelBinding)).read_grant = False
        elif mutation == 'membership':
            session.scalar(select(Membership).where(Membership.principal_id == 'founder')).status = 'suspended'
        else:
            row.status = 'paused'
        session.flush()
        current = index(session, task_id, run_id)
        assert current['claims'] == []
        assert current['snapshot_sha256'] != original['snapshot_sha256']
        with pytest.raises(RetrievalError, match='knowledge_context_changed'):
            read_indexed_claim(session, index=original, claim_id=result['claim_id'])


def test_private_source_keeps_owner_scope(env):
    app, _, task_id, run_id, _ = env
    with app.transaction() as session:
        row, _ = seed(session)
        row.visibility = 'private'
        result = sync_experience(session, row)
        assert session.get(KnowledgeClaim, result['claim_id']).owner_scope == 'founder'
        assert index(session, task_id, run_id)['claims'] == []


def test_explicit_wrong_organization_is_rejected_without_claim(env):
    app, _, _, _, _ = env
    with app.transaction() as session:
        row, _ = seed(session)
        from evoharness.knowledge.service import KnowledgeError
        with pytest.raises(KnowledgeError, match='claim_source_scope_mismatch'):
            sync_experience(session, row, org_id='other-org')
        assert not list(session.scalars(select(KnowledgeClaim).where(
            KnowledgeClaim.subject == 'experience:gateway-procedure')))


def test_unknown_visibility_is_never_promoted_to_public_knowledge(env):
    app,_,_,_,_=env
    with app.transaction() as session:
        row,_=seed(session)
        row.visibility='unclassified'
        session.flush()
        from evoharness.knowledge.service import KnowledgeError
        with pytest.raises(KnowledgeError,match='claim_source_unavailable'):
            sync_experience(session,row)
        assert not list(session.scalars(select(KnowledgeClaim).where(
            KnowledgeClaim.subject=='experience:gateway-procedure')))


def test_other_tenant_same_intent_cannot_invalidate_original(env):
    app, _, task_id, run_id, _ = env
    with app.transaction() as session:
        row, _ = seed(session)
        result = sync_experience(session, row)
        other = Experience(tenant='other', owner='founder', intent_id=row.intent_id,
            version=1, question_pattern=row.question_pattern, canonical_answer='other',
            status='paused')
        session.add(other)
        session.flush()
        sync_experience(session, other)
        assert session.get(KnowledgeClaim, result['claim_id']).state == 'active'
        assert len(index(session, task_id, run_id)['claims']) == 1
