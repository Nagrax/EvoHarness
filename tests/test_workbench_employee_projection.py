from evoharness.__main__ import org_view, seed_organization
from evoharness.api import WorkApi
from evoharness.application import Application
from evoharness.organization.models import EmployeeProfile, Membership, Organization


def test_employee_projection_is_scoped_and_excludes_runtime(tmp_path):
    app = Application.open('sqlite:///' + (tmp_path / 'org.db').as_posix())
    api = WorkApi(app, artifacts_dir=tmp_path / 'artifacts')
    with app.transaction() as session:
        seed_organization(session)
        session.add(Organization(org_id='other', title='Other', owner_principal_id='other-owner'))
        session.flush()
        session.add(Membership(membership_id='other-member', org_id='other', principal_id='other-ai', principal_kind='ai_service'))
        session.flush()
        session.add(EmployeeProfile(employee_id='other-employee', org_id='other', membership_id='other-member', display_name='Other', runtime_binding='secret-binding', instructions_version='secret-instruction'))
    owner = {'principal_id': 'founder', 'org_ids': ['org-demo'], 'admin': False}
    result = org_view(api, org_id='org-demo', principal=owner)
    assert result['ok']
    employees = result['data']['employees']
    assert employees and all(set(e) == {'employee_id', 'membership_id', 'display_name', 'status'} for e in employees)
    assert all(e['employee_id'] != 'other-employee' for e in employees)
    denied = org_view(api, org_id='other', principal=owner)
    assert not denied['ok']
    assert 'other-employee' not in str(denied)
    app.engine.dispose()


def test_workbench_approval_and_start_through_fastapi(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from evoharness.api import open_api
    import server.app as web

    api = open_api('sqlite:///' + (tmp_path / 'runs.db').as_posix(),
                   artifacts_dir=tmp_path / 'artifacts')
    monkeypatch.setattr(web, '_get_work_api', lambda: api)
    monkeypatch.setattr(web, '_work_gate', lambda request: (None, None))
    task = api.create_work_item(tenant='tenant', owner='alice', title='delegated',
                                goal='fix', acceptance=[{'kind': 'code'}],
                                assignee_id='alice', workflow_kind='delegated_delivery',
                                org_id='org')
    assert task['ok'], task
    task_id = task['data']['id']
    with TestClient(web.app) as client:
        def op(name, params):
            response = client.post('/api/work/op', json={'op': name, 'params': params})
            assert response.status_code == 200
            return response.json()

        plan = op('run_request_payload', {'task_id': task_id})
        assert plan['ok'], plan
        approval = op('submit_approval', {
            'org_id': 'org', 'actor_id': 'alice', 'grantee_id': 'alice',
            'target_type': 'work_item', 'target_id': str(task_id),
            'target_revision': plan['data']['target_revision'],
            'payload': plan['data']['payload'], 'actions': ['work.execute'],
        })
        assert approval['ok'], approval
        started = op('start_run', {'task_id': task_id, 'actor_id': 'alice'})
        assert started['ok'], started
        assert started['data']['operator_id'] == 'alice'
        assert started['data']['decision_id'] == approval['data']['decision_id']
    api._app.engine.dispose()
