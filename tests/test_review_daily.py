from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def daily_web(tmp_path, monkeypatch):
    import server.app as web
    monkeypatch.setattr(web, 'DEPLOYMENT_MODE', 'hosted')
    monkeypatch.setattr(web, 'HOSTED_TOKEN', 'review-admin')
    monkeypatch.setattr(web, 'HOSTED_PRINCIPALS', {
        'review-member': {'subject': 'alice', 'org_ids': ['org'], 'admin': False}})
    db = tmp_path / 'agent.db'
    with closing(sqlite3.connect(db)) as con, con:
        con.execute('CREATE TABLE daily_reports (id INTEGER PRIMARY KEY, logical_date TEXT, '
                    'status TEXT, delivered_at TEXT, created_at TEXT, content_json TEXT)')
        con.execute('INSERT INTO daily_reports VALUES (1, ?, ?, ?, ?, ?)',
                    ('2026-11-16', 'ready', None, '', json.dumps({
                        'tasks': [{'title': 'other-owner-private-sentinel'}]})))
    monkeypatch.setattr(web, '_FEISHU_DAILY_DB', db)
    return web, db


@pytest.mark.parametrize('token,status', [('', 401), ('review-member', 403), ('review-admin', 200)])
def test_latest_daily_requires_shared_runtime_admin(daily_web, token, status):
    web, _ = daily_web
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    response = TestClient(web.app).get('/api/daily/latest', headers=headers)
    assert response.status_code == status
    assert ('other-owner-private-sentinel' in response.text) is (status == 200)


@pytest.mark.parametrize('content', [[], None, 'invalid', {'pending_replies': [None, 1],
    'gaps': [{'count': 'unknown'}, None], 'summary': []}])
def test_invalid_persisted_daily_never_crashes(daily_web, content):
    web, db = daily_web
    with closing(sqlite3.connect(db)) as con, con:
        con.execute('UPDATE daily_reports SET content_json=?', (json.dumps(content),))
    response = TestClient(web.app).get('/api/daily/latest',
        headers={'Authorization': 'Bearer review-admin'})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body.get('available'), bool)
    if body['available']:
        assert isinstance(body['report']['summary'], dict)


def test_latest_daily_local_user_can_read_uri_sensitive_path(daily_web, monkeypatch):
    web, db = daily_web
    renamed = db.with_name('日报 #1.db')
    db.rename(renamed)
    monkeypatch.setattr(web, '_FEISHU_DAILY_DB', renamed)
    monkeypatch.setattr(web, 'DEPLOYMENT_MODE', 'local-single-user')
    body = TestClient(web.app).get('/api/daily/latest').json()
    assert body['available'] and body['report']['sections']['tasks']['total'] == 1


def test_settings_saves_and_clears_access_token_without_persisting_it():
    import server.app as web
    html = (web.ROOT / 'frontend/index.html').read_text(encoding='utf-8')
    script = html.split('function openSettings(){', 1)[1].split('function closeSettings()', 1)[0]
    script = 'function openSettings(){' + script
    runner = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const nodes=new Map();
const node=s=>{if(!nodes.has(s))nodes.set(s,{value:'',style:{},classList:{add(){}},focus(){}});return nodes.get(s)};
const local=new Map(), session=new Map([['evo.accessToken','before']]);
let reloads=0;
const storage=m=>({getItem:k=>m.get(k)||null,setItem:(k,v)=>m.set(k,v),removeItem:k=>m.delete(k)});
const ctx={$:node,loadApiCfg:()=>({}),localStorage:storage(local),sessionStorage:storage(session),
 closeSettings(){},updateSrvModel(){},toast(){},setTimeout(){},location:{reload(){reloads++}}};
vm.createContext(ctx);vm.runInContext(SCRIPT,ctx);ctx.openSettings();
assert(node('#setModal .set-body').innerHTML.includes('id="setAccessToken"'));
assert.equal(node('#setAccessToken').value,'before');
node('#setAccessToken').value=' after ';node('#setSave').onclick();
assert.equal(session.get('evo.accessToken'),'after');
assert(!JSON.stringify([...local]).includes('after'));
ctx.openSettings();node('#setClear').onclick();assert(!session.has('evo.accessToken'));
assert.equal(reloads,2);
'''
    result = subprocess.run(['node', '-e', runner.replace('SCRIPT', json.dumps(script))],
        capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
