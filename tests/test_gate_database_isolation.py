"""Deployment database URLs must not enter an offline gate subprocess."""
import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


@pytest.mark.parametrize('variable', ['EVOHARNESS_DATABASE_URL', 'DATABASE_URL'])
def test_gate_overrides_inherited_and_dotenv_database_urls(tmp_path, variable):
    project = Path(__file__).resolve().parents[1]
    production = tmp_path / 'production'
    production.mkdir()
    database = production / 'org.db'
    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute('CREATE TABLE preserved (value TEXT)')
            connection.execute("INSERT INTO preserved VALUES ('original')")
    original = hashlib.sha256(database.read_bytes()).hexdigest()
    url = 'sqlite:///' + database.as_posix()
    mini = tmp_path / 'mini'
    (mini / 'tests').mkdir(parents=True)
    (mini / '.env').write_text(f'{variable}={url}\n', encoding='utf-8')
    (mini / 'tests/test_database.py').write_text(
        "import os\nfrom pathlib import Path\nfrom dotenv import load_dotenv\n"
        "from evoharness.application import Application\n"
        "def test_database():\n"
        "    load_dotenv('.env', override=False)\n"
        f"    app = Application.open(os.environ[{variable!r}])\n"
        "    try:\n"
        "        assert Path(app.engine.url.database).resolve().is_relative_to(Path(os.environ['HOME']).resolve())\n"
        "    finally:\n"
        "        app.engine.dispose()\n", encoding='utf-8')
    env = os.environ.copy()
    env.update(EVOHARNESS_DATABASE_URL=url, DATABASE_URL=url,
               PYTHONPATH=str(project))
    output = tmp_path / 'gate'
    result = subprocess.run(
        [sys.executable, '-X', 'utf8', str(project/'eval/digital_employee_gate.py'),
         '--jobs', 'product', '--repo-root', str(mini), '--protect-root', str(production),
         '--output-dir', str(output), '--timeout', '120'],
        env=env, capture_output=True, text=True, encoding='utf-8', timeout=180)
    report = json.loads((output/'gate-report.json').read_text(encoding='utf-8'))
    assert result.returncode == 0, report['overall']
    assert report['jobs']['product']['suites'][0]['passed'] == 1
    assert not report['write_guard']['breach']
    assert hashlib.sha256(database.read_bytes()).hexdigest() == original
    assert list(production.iterdir()) == [database]


@pytest.mark.parametrize('relative', [
    'data/org.db', 'org.db', 'org.db.bak-original',
    'feishu-daily-agent/data/agent.db',
    'feishu-daily-agent/data/nested/auth.db', 'integration/data/legacy.db',
])
def test_gate_detects_explicit_writes_to_unified_database_files(tmp_path, relative):
    project = Path(__file__).resolve().parents[1]
    root = tmp_path/'mini'
    (root/'tests').mkdir(parents=True)
    database = root/relative
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b'original database sentinel')
    (root/'tests/test_write.py').write_text(
        "from pathlib import Path\n"
        "def test_writer():\n"
        f"    (Path(__file__).resolve().parents[1]/{relative!r}).write_bytes(b'changed')\n",
        encoding='utf-8')
    output = tmp_path/'gate'
    result = subprocess.run(
        [sys.executable,'-X','utf8',str(project/'eval/digital_employee_gate.py'),
         '--repo-root',str(root),'--jobs','product','--output-dir',str(output)],
        capture_output=True,text=True,encoding='utf-8',timeout=120)
    report = json.loads((output/'gate-report.json').read_text(encoding='utf-8'))
    assert result.returncode == 2, report['overall']
    assert report['write_guard']['breach']
    assert any(row['path']==relative and row['change']=='modified'
               for row in report['write_guard']['changed'])
