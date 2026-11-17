import json
from pathlib import Path


def test_roadmap_execution_references_resolve_to_preserved_backlog_items():
    import server.app as web
    backlog = json.loads((web.ROOT / 'tests/fixtures/loop-backlog.json').read_text(encoding='utf-8'))
    ids = {item['id'] for item in backlog['items']}
    missing = {(task_id, ref) for task_id, task in backlog['deMapping']['tasks'].items()
               for ref in task.get('l', []) if ref not in ids}
    assert not missing, sorted(missing)
