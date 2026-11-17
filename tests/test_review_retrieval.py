import json

import pytest

from test_execution_knowledge import env
from evoharness.knowledge.retrieval import execution_index, RetrievalError
from evoharness.organization.models import Assignment, Membership, Organization, BusinessSpace, Position
from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
from evoharness.governance.budget_gateway import BudgetGateway
from evoharness.work.worker import WorkWorker


@pytest.mark.parametrize('change', ['membership', 'grant', 'assignment', 'position', 'org', 'space'])
def test_knowledge_rechecks_current_role_authority(env, change):
    app, _, task_id, run_id, _ = env
    with app.transaction() as session:
        if change == 'membership':
            session.get(Membership, 'm-ai-support').status = 'revoked'
        elif change == 'grant':
            session.get(Membership, 'm-ai-support').grants_json = '["work.execute"]'
        elif change == 'assignment':
            session.get(Assignment, 'asg-support').status = 'ended'
        elif change == 'position':
            session.get(Position, 'pos-support').action_ceiling_json = '["work.execute"]'
        elif change == 'org':
            session.get(Organization, 'org-demo').enabled = False
        else:
            session.get(BusinessSpace, 'space-support').enabled = False
    with app.transaction() as session:
        with pytest.raises(RetrievalError, match='knowledge_not_authorized'):
            execution_index(session, run_id=run_id, task_id=task_id, org_id='org-demo',
                space_id='space-support', principal_id='ai-support')


def test_role_revocation_during_model_call_stops_delivery(env):
    app, api, _, run_id, _ = env
    def script(context):
        with app.transaction() as session:
            session.get(Membership, 'm-ai-support').status = 'revoked'
        return [{'content': 'must not deliver', 'final': True}]
    adapter = RestrictedAgentAdapter(provider=FakeProvider(script), gateway=BudgetGateway(app.session_factory))
    result = WorkWorker(app.session_factory, adapter, api._authorize_adapter()).process_run(run_id)
    assert not result['ok'] and result['error'] == 'knowledge_not_authorized', result


def test_scheduler_assignment_actor_uses_the_members_private_scope(env):
    from evoharness.work.models import ExecutionRun
    app, _, task_id, run_id, (_, private, _) = env
    with app.transaction() as session:
        session.get(ExecutionRun, run_id).actor_id = 'asg-support'
        session.flush()
        index = execution_index(session, run_id=run_id, task_id=task_id, org_id='org-demo',
            space_id='space-support', principal_id='asg-support')
    assert private in {row['claim_id'] for row in index['claims']}


def test_acceptance_rejects_role_revoked_after_execution(env):
    app, api, task_id, run_id, _ = env
    adapter = RestrictedAgentAdapter(provider=FakeProvider([
        {'content': 'answer with provenance', 'final': True}]), gateway=BudgetGateway(app.session_factory))
    result = WorkWorker(app.session_factory, adapter, api._authorize_adapter()).process_run(run_id)
    assert result['ok'], result
    with app.transaction() as session:
        session.get(Membership, 'm-ai-support').status = 'revoked'
    result = api.request_acceptance(task_id, run_id=run_id, requester='ai-support')
    assert not result['ok'] and result['reason_code'] == 'knowledge_not_authorized', result
