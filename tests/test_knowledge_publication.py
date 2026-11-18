"""A fixed source manifest publishes as one version; invalid input preserves it."""
import json

import pytest
from sqlalchemy import select

from test_execution_knowledge import env
from test_experience_execution_contract import seed,index
from evoharness.knowledge.compiler import prepare_manifest,publish_manifest,scope_key
from evoharness.knowledge.models import KnowledgeClaim,KnowledgePublication
from evoharness.knowledge.service import KnowledgeError
from evoharness.models import Experience,Message,Task

SCOPE=dict(org_id='org-demo',space_id='space-support',tenant='local',owner='founder')


def test_fixed_manifest_publish_and_unchanged_reuse(env):
    app,api,task_id,run_id,_=env
    with app.transaction() as session:
        seed(session)
    first=api.sync_knowledge(org_id='org-demo',space_id='space-support',tenant='local',actor='founder')
    assert first['ok'] and first['data']['completed'] and first['data']['processed']==1,first
    again=api.sync_knowledge(org_id='org-demo',space_id='space-support',tenant='local',actor='founder')
    assert again['ok'] and again['data']['unchanged'] and again['data']['revision']==1,again
    with app.transaction() as session:
        assert len(index(session,task_id,run_id)['claims'])==1


@pytest.mark.parametrize('failure',['changed','omitted','unknown_field','stale_revision'])
def test_bad_build_cannot_replace_previous_publication(env,failure):
    app,_,_,_,_=env
    with app.transaction() as session:
        seed(session)
        manifest=prepare_manifest(session,**SCOPE)
        receipt=publish_manifest(session,**SCOPE,manifest=manifest,expected_revision=0)
        frozen=prepare_manifest(session,**SCOPE)
        if failure=='changed':
            session.scalar(select(Message).where(Message.message_id=='contract-source')).content_text='新内容'
            session.flush()
        elif failure=='omitted':
            frozen=[]
        elif failure=='unknown_field':
            frozen=[{**frozen[0],'unknown':True}]
        with pytest.raises(KnowledgeError):
            publish_manifest(session,**SCOPE,manifest=frozen,
                             expected_revision=0 if failure=='stale_revision' else 1)
        publication=session.get(KnowledgePublication,scope_key('org-demo','space-support','founder'))
        assert publication.revision==1 and publication.build_sha256==receipt['build_sha256']
        assert json.loads(publication.claim_ids_json)==receipt['claim_ids']


def test_related_claims_expand_only_inside_current_reader_scope(env):
    app,_,task_id,run_id,_=env
    with app.transaction() as session:
        row,_=seed(session)
        second=Experience(tenant=row.tenant,owner=row.owner,intent_id='rate-limit',version=1,
            question_pattern='配额恢复流程',canonical_answer='联系值班人恢复配额',
            status='approved_auto',visibility='public',source_ids=row.source_ids)
        session.add(second)
        session.flush()
        manifest=prepare_manifest(session,**SCOPE)
        publish_manifest(session,**SCOPE,manifest=manifest,expected_revision=0)
        found=index(session,task_id,run_id)
        assert len(found['claims'])==2
        private=session.scalar(select(KnowledgeClaim).where(KnowledgeClaim.subject=='experience:rate-limit'))
        private.owner_scope='founder'
        session.flush()
        found=index(session,task_id,run_id)
        assert len(found['claims'])==1
        assert private.claim_id not in json.dumps(found)


def test_removed_source_is_not_kept_by_a_previous_good_publication(env):
    app,_,task_id,run_id,_=env
    with app.transaction() as session:
        row,source=seed(session)
        receipt=publish_manifest(session,**SCOPE,manifest=prepare_manifest(session,**SCOPE),expected_revision=0)
        source.enabled=False
        session.flush()
        assert index(session,task_id,run_id)['claims']==[]
        current=publish_manifest(session,**SCOPE,manifest=prepare_manifest(session,**SCOPE),expected_revision=1)
        assert current['processed']==0 and current['revision']==2
        assert session.get(KnowledgeClaim,receipt['claim_ids'][0]).state=='stale'
