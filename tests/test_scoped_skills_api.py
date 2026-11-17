import json
from datetime import timedelta

import pytest

from agents.frontmatter import format_frontmatter
from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.clock import utcnow
from evoharness.knowledge.skill_scope_events import record_scope_event
from evoharness.organization.models import BusinessSpace, Membership, Organization


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    root.mkdir()
    monkeypatch.setenv('EVOHARNESS_SKILLS_ROOT', str(root))
    monkeypatch.setenv('EVOHARNESS_EVOLUTION_DIR', str(tmp_path / 'evolution'))
    app = Application.open('sqlite:///' + (tmp_path / 'scoped.db').as_posix())
    with app.transaction() as session:
        for org, tenant, owner in [('one', 't1', 'alice'), ('two', 't2', 'bob')]:
            session.add(Organization(org_id=org, title=org, owner_principal_id=owner))
            session.flush()
            session.add(BusinessSpace(space_id=org + '-space', org_id=org, title=org))
            session.add(Membership(membership_id=org + '-member', org_id=org,
                principal_id=owner, tenant_id=tenant, app_id='app',
                grants_json='["knowledge.read"]'))
    api = WorkApi(app)
    principal = {'subject': 'alice', 'org_ids': ['one'], 'admin': False}

    def put(directory, *, tenant='t1', space='one-space', owner='alice',
            producer='source', revision=1, bound=True):
        scope = dict(tenant=tenant, space=space, owner=owner, producer=producer,
                     source_revision=revision)
        folder = root / directory
        folder.mkdir(exist_ok=True)
        file = folder / 'SKILL.md'
        meta = {'name': 'same-name', 'description': directory}
        if bound:
            meta['scope-json'] = json.dumps(scope)
        file.write_text(format_frontmatter(meta, 'instructions'), encoding='utf-8')
        return file, scope

    yield api, principal, put, tmp_path
    app.engine.dispose()


def test_member_skills_bind_server_identity_and_never_global(scoped):
    api, principal, put, tmp = scoped
    put('owned')
    put('other-owner', owner='bob')
    put('other-tenant', tenant='t2')
    put('other-space', space='two-space')
    put('unbound', bound=False)
    put('missing-producer', producer='')
    put('bad-revision', revision=True)
    evolution = tmp / 'evolution'
    evolution.mkdir()
    (evolution / 'online_eval_report.json').write_text(
        '{"secret-global-champion":"bob"}', encoding='utf-8')
    (evolution / 'usage.jsonl').write_text(json.dumps(
        {'event': 'invoke', 'skill': 'same-name', 'scope': {
            'tenant': 't1', 'space': 'one-space', 'owner': 'alice',
            'producer': 'source', 'source_revision': 1}}) + '\n', encoding='utf-8')
    result = api.skills_view(org_id='one', space_id='one-space',
        tenant='t2', owner='bob', principal=principal)
    assert result['ok'], result
    data = result['data']
    assert data['scope']['owner'] == 'alice'
    assert data['scope']['tenant'] == 't1'
    assert data['capabilities'] == {'legacy_tools': False}
    assert not data['has_report']
    assert len(data['skills']) == 1
    assert data['skills'][0]['description'] == 'owned'
    assert data['skills'][0]['counts']['invocations'] == 1
    assert data['skills'][0]['evaluation'] is None
    assert 'secret-global-champion' not in str(result)
    assert api.skills_view(principal=principal)['reason_code'] == 'organization_required'
    assert not api.skills_view(legacy=True, principal=principal)['ok']
    assert not api.skills_view(org_id='two', principal=principal)['ok']
    assert not api.skills_view(org_id='one', space_id='two-space', principal=principal)['ok']
    selection = api.skills_view(org_id='one', principal=principal)
    assert selection['ok'] and selection['data']['scope'] is None
    assert selection['data']['spaces'] == [{'space_id': 'one-space', 'title': 'one'}]
    assert api.skills_view()['data']['mode'] == 'legacy'


def test_member_revocation_and_source_rewrite_are_not_cached(scoped):
    api, principal, put, _ = scoped
    file, _scope = put('owned')
    query = dict(org_id='one', space_id='one-space', principal=principal)
    assert len(api.skills_view(**query)['data']['skills']) == 1
    with api._app.transaction() as session:
        record_scope_event(session, skill_name='same-name', tenant='t1',
            space='one-space', owner='alice', source_revision=1, action='revoked')
    assert api.skills_view(**query)['data']['skills'] == []
    with api._app.transaction() as session:
        record_scope_event(session, skill_name='same-name', tenant='t1',
            space='one-space', owner='alice', source_revision=1, action='restored')
    assert len(api.skills_view(**query)['data']['skills']) == 1
    put('owned', owner='bob')
    assert api.skills_view(**query)['data']['skills'] == []
    file.unlink()
    assert api.skills_view(**query)['data']['skills'] == []


@pytest.mark.parametrize('change,code', [
    ('membership', 'membership_not_active'),
    ('expired', 'membership_not_active'),
    ('future', 'membership_not_active'),
    ('org', 'principal_org_not_authorized'),
    ('space', 'space_not_authorized'),
    ('ambiguous', 'skill_namespace_ambiguous'),
    ('historical-app', 'skill_namespace_ambiguous'),
])
def test_scope_requires_current_database_authority(scoped, change, code):
    api, principal, put, _ = scoped
    put('owned')
    with api._app.transaction() as session:
        member = session.get(Membership, 'one-member')
        if change == 'membership':
            member.status = 'suspended'
        elif change == 'expired':
            member.valid_until = utcnow() - timedelta(seconds=1)
        elif change == 'future':
            member.valid_from = utcnow() + timedelta(days=1)
        elif change == 'org':
            session.get(Organization, 'one').enabled = False
        elif change == 'space':
            session.get(BusinessSpace, 'one-space').enabled = False
        else:
            if change == 'historical-app':
                member.status = 'revoked'
            session.add(Membership(membership_id='duplicate', org_id='one',
                principal_id='alice', tenant_id='t1', app_id='other-app'))
    result = api.skills_view(org_id='one', space_id='one-space', principal=principal)
    assert not result['ok'] and result['reason_code'] == code, result
    assert 'owned' not in str(result)


def test_scoped_report_is_hash_and_full_ref_bound(scoped):
    import hashlib
    from agents.scoped_skill_report import write_scoped_skill_report
    from agents.skill_scope import ScopedSkillRef
    api, principal, put, _ = scoped
    file, scope = put('owned')
    ref = ScopedSkillRef(name='same-name', **scope)
    write_scoped_skill_report(ref, file, skill_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
        protocol_id='test-protocol', run_id='run-1',
        outcomes=[{'sample_id': 's1', 'rule_id': 'r1', 'passed': True},
                  {'sample_id': 's2', 'rule_id': 'r1', 'passed': False}])
    query = dict(org_id='one', space_id='one-space', principal=principal)
    data = api.skills_view(**query)['data']
    assert data['has_report']
    assert data['skills'][0]['evaluation'] == {
        'protocol_id': 'test-protocol', 'run_id': 'run-1',
        'samples': 2, 'checks': 2, 'passed': 1, 'pass_rate': 0.5}
    file.write_text(file.read_text(encoding='utf-8') + '\nchanged', encoding='utf-8')
    data = api.skills_view(**query)['data']
    assert not data['has_report'] and data['skills'][0]['evaluation'] is None


def test_restored_scope_does_not_reuse_previous_generation_report(scoped):
    import hashlib
    from agents.scoped_skill_report import write_scoped_skill_report
    from agents.skill_scope import ScopedSkillRef
    api, principal, put, _ = scoped
    file, scope = put('owned')
    ref = ScopedSkillRef(name='same-name', **scope)
    kwargs = dict(skill_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
        protocol_id='test-protocol', run_id='run-before-revoke',
        outcomes=[{'sample_id': 'one', 'rule_id': 'a', 'passed': True},
                  {'sample_id': 'one', 'rule_id': 'b', 'passed': False}])
    write_scoped_skill_report(ref, file, **kwargs)
    query = dict(org_id='one', space_id='one-space', principal=principal)
    before = api.skills_view(**query)['data']['skills'][0]['evaluation']
    assert before['samples'] == 1 and before['checks'] == 2
    with api._app.transaction() as session:
        for action in ['revoked', 'restored']:
            event = record_scope_event(session, skill_name='same-name', tenant='t1',
                space='one-space', owner='alice', source_revision=1, action=action)
        generation = event.id
    assert not api.skills_view(**query)['data']['has_report']
    write_scoped_skill_report(ref, file, scope_generation=generation, **kwargs)
    assert api.skills_view(**query)['data']['has_report']


def test_hosted_http_and_operation_share_member_scope(scoped, monkeypatch):
    from fastapi.testclient import TestClient
    import server.app as web
    api, principal, put, _ = scoped
    put('owned')
    monkeypatch.setattr(web, 'DEPLOYMENT_MODE', 'hosted')
    monkeypatch.setattr(web, 'HOSTED_PRINCIPALS', {'member': principal})
    monkeypatch.setattr(web, '_get_work_api', lambda: api)
    with TestClient(web.app) as client:
        assert client.get('/api/work/skills').status_code == 401
        headers = {'Authorization': 'Bearer member'}
        response = client.get('/api/work/skills?org=one&space=one-space&owner=bob&tenant=t2', headers=headers)
        assert response.status_code == 200
        assert response.json()['data']['scope']['owner'] == 'alice'
        response = client.post('/api/work/op', headers=headers, json={
            'op': 'skills_view', 'params': {'org_id': 'one', 'space_id': 'one-space',
                'legacy': True, 'principal': {'admin': True}}})
        assert not response.json()['ok']
        assert client.get('/api/skills', headers=headers).status_code == 403
    assert 'skills_view' in api.schema()['data']['operations']


def test_registry_preserves_trusted_namespace(monkeypatch):
    import server.app as web
    monkeypatch.setenv('EVOHARNESS_HOSTED_PRINCIPALS', json.dumps({'token': {
        'subject': 'alice', 'org_ids': ['one'], 'tenant_id': 't1', 'app_id': 'app'}}))
    principal = web._load_hosted_principals()['token']
    assert principal['tenant_id'] == 't1' and principal['app_id'] == 'app'
