"""The real Worker reads only scoped SOPs and persists the actual loaded version."""
import hashlib
import json
from pathlib import Path

from test_execution_knowledge import env
from agents.frontmatter import format_frontmatter
from evoharness.governance.budget_gateway import BudgetGateway
from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
from evoharness.work.worker import WorkWorker
from evoharness.work.models import ExecutionRun


def skill(tmp_path, monkeypatch, *, owner='ai-support'):
    root = tmp_path / 'skills'
    path = root / 'refund' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    scope = dict(tenant='local',space='space-support',owner=owner,
                 producer='native',source_revision=1)
    path.write_text(format_frontmatter({'name':'refund', 'description':'退款规则核对',
        'version':'1.0.0','scope-json':json.dumps(scope)},
        '先读取退款规则；核对来源，再给答复。'),encoding='utf-8')
    monkeypatch.setenv('EVOHARNESS_SKILLS_ROOT',str(root))
    monkeypatch.setenv('EVOHARNESS_EVOLUTION_DIR',str(tmp_path/'evolution'))
    return path


def test_worker_loads_and_receipts_scoped_sop(env, tmp_path, monkeypatch):
    app, api, _, run_id, _ = env
    path = skill(tmp_path,monkeypatch)
    def script(context):
        if not context['history']:
            assert [s['name'] for s in context['skill_index']['skills']] == ['refund']
            return [{'tool_calls':[{'name':'skill','arguments':{'skill_name':'refund'}}]}]
        loaded = json.loads(context['history'][-1]['detail'])
        assert '核对来源' in loaded['instructions']
        return [{},{'content':'已按来源完成答复。','final':True}]
    adapter = RestrictedAgentAdapter(provider=FakeProvider(script),gateway=BudgetGateway(app.session_factory))
    result = WorkWorker(app.session_factory,adapter,api._authorize_adapter()).process_run(run_id)
    assert result['ok'],result
    with app.transaction() as session:
        checkpoint = json.loads(session.get(ExecutionRun,run_id).checkpoint_json)
        assert checkpoint['skills']['loaded'][0]['file_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert checkpoint['skills']['loaded'][0]['ref']['owner'] == 'ai-support'


def test_another_members_sop_is_not_available(env, tmp_path, monkeypatch):
    app, api, _, run_id, _ = env
    skill(tmp_path,monkeypatch,owner='founder')
    def script(context):
        assert context['skill_index']['skills'] == []
        return [{'content':'答复。','final':True}]
    adapter = RestrictedAgentAdapter(provider=FakeProvider(script),gateway=BudgetGateway(app.session_factory))
    assert WorkWorker(app.session_factory,adapter,api._authorize_adapter()).process_run(run_id)['ok']


def test_source_edit_during_model_response_stops_delivery(env, tmp_path, monkeypatch):
    app, api, _, run_id, _ = env
    path = skill(tmp_path,monkeypatch)
    def script(context):
        path.write_text(path.read_text(encoding='utf-8')+'\n新规则',encoding='utf-8')
        return [{'content':'基于旧版的答复。','final':True}]
    adapter = RestrictedAgentAdapter(provider=FakeProvider(script),gateway=BudgetGateway(app.session_factory))
    result = WorkWorker(app.session_factory,adapter,api._authorize_adapter()).process_run(run_id)
    assert not result['ok'] and result['error'] == 'skill_context_changed',result
