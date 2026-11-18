"""Atomic projection writes and history must survive native Windows path limits."""
import json
import os
from pathlib import Path

import pytest

from agents.skill_evolution import write_managed_projection


@pytest.mark.skipif(os.name != 'nt', reason='Windows native filesystem path limit')
def test_long_projection_tempfile_and_history_keep_scope_and_audit(tmp_path, monkeypatch):
    monkeypatch.delenv('EVOHARNESS_HOLDOUT_READONLY', raising=False)
    padding = 231 - len(str(tmp_path.resolve())) - 1
    assert padding > 0
    root = tmp_path / ('p' * padding)
    state = root / ('.projection-state-' + 's' * 64)
    target = root / 'feishu-hr/SKILL.md'
    assert len(str(target)) < 260
    assert len(str(target.parent / '.projection-12345678')) >= 260
    scope = {'tenant': 'test-tenant', 'owner': 'owner-a'}
    args = dict(skills_root=root, evolution_root=state, name='feishu-hr',
                description='Private projection', when_to_use='Authorized questions',
                scope=scope, source_revisions=[], actor='test')
    first = write_managed_projection(**args, instructions='First private answer')
    assert first['ok'] and first['action'] == 'created'
    second = write_managed_projection(**args, instructions='Updated private answer')
    assert second['ok'] and second['action'] == 'evolved'
    assert second['file'] == str(target.resolve())
    assert 'Updated private answer' in target.read_text(encoding='utf-8')
    native_state = Path('\\\\?\\' + str(state.resolve()))
    usage = [json.loads(line) for line in (native_state/'usage.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [row['action'] for row in usage] == ['created', 'evolved']
    history = [json.loads(line) for line in (native_state/'history/feishu-hr.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(history) == 1 and 'First private answer' in history[0]['content']
    assert history[0]['file'] == str(target.resolve())
    rejected = write_managed_projection(**{**args, 'scope': {**scope, 'owner':'owner-b'}},
                                        instructions='Other owner answer')
    assert not rejected['ok'] and rejected['error'] == 'unmanaged_projection_conflict'
    assert 'Other owner answer' not in target.read_text(encoding='utf-8')
