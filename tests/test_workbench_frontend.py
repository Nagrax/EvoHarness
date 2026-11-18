"""Exercise the workbench's actual display logic with a small browser API stub."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_home_uses_scoped_api_fields_and_preserves_errors():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for frontend JavaScript')
    frontend = Path(__file__).resolve().parents[1] / 'frontend' / 'index.html'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const source=html.slice(html.indexOf('function viewFromHash('),html.indexOf("$('#homeRefresh').onclick=loadHome;"));
const nodes={};
function $(id){return nodes[id]||(nodes[id]={value:id==='#homeFilter'?'active':'',textContent:'',innerHTML:'',className:'',querySelectorAll(){return []},append(){}})}
const records={
 '/api/work/view':{buckets:{pending:[{id:7,title:'待处理事项',assignee:'执行者',display:{label:'待处理',next_action:'确认'}}]}},
 '/api/work/org':{memberships:[{membership_id:'m',principal_id:'principal',principal_kind:'ai_service',status:'active'}],employees:[{employee_id:'employee',membership_id:'m',display_name:'助手'}],assignments:[{employee_id:'employee',position_id:'p',status:'active'}],positions:[{position_id:'p',title:'分析员'}]},
 '/api/work/approvals':{approvals:[]}
};
let fail=false;
let requested=[];
const context={$,workScope:'personal',document:{createElement(){return {}}},localStorage:{getItem(k){return k==='evo.orgId'?'org':''}},workIdentity(){return {tenant:'t',owner:'o'}},apiFetch:async url=>{requested.push(url);return {json:async()=>fail?{ok:false,reason_code:'denied',next_action:'check'}:{ok:true,data:records[url.split('?')[0]]}}},failText(j){return j.reason_code},esc2(s){return String(s??'')},openWorkDrawer(){},setView(){},openIdentity(){}};
vm.createContext(context);vm.runInContext(source,context);
assert.equal(context.viewFromHash(''),'home');assert.equal(context.viewFromHash('#/chat'),'chat');assert.equal(context.viewFromHash('#/work'),'work');
(async()=>{
 await context.loadHome();assert.match(requested[0],/org=$/);
 assert.match(nodes['#homeTasks'].innerHTML,/执行者/);assert.match(nodes['#homeTasks'].innerHTML,/确认/);
 assert.match(nodes['#homeOrg'].innerHTML,/助手/);assert.match(nodes['#homeOrg'].innerHTML,/分析员/);
 context.workScope='org';requested=[];await context.loadHome();assert.match(requested[0],/org=org$/);
 let release;const slow=new Promise(resolve=>{release=resolve});let calls=0;
 context.apiFetch=async url=>{if(url.startsWith('/api/work/view')&&++calls===1){await slow;return {json:async()=>({ok:true,data:{buckets:{pending:[{id:8,title:'旧结果',display:{label:'待处理'}}]}}})}}return {json:async()=>({ok:true,data:records[url.split('?')[0]]})}};
 const old=context.loadHome();const fresh=context.loadHome();await fresh;release();await old;
 assert.match(nodes['#homeTasks'].innerHTML,/待处理事项/);assert.doesNotMatch(nodes['#homeTasks'].innerHTML,/旧结果/);
 context.apiFetch=async()=>({json:async()=>({ok:false,reason_code:'denied'})});
 await context.loadHome();assert.match(nodes['#homeState'].textContent,/denied/);assert.match(nodes['#homeTasks'].innerHTML,/暂不可用/);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script, str(frontend)], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_approval_binds_grantee_and_refreshes_after_start_failure():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for frontend JavaScript')
    frontend = Path(__file__).resolve().parents[1] / 'frontend' / 'index.html'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const source=html.slice(html.indexOf('async function approveAndStart('),html.indexOf('async function reviewRun('));
const calls=[],messages=[];let refresh=0,drawer=0;
const context={workIdentity:()=>({owner:'operator'}),workOrg:()=>'org',workDrawerTask:7,
 document:{body:{dataset:{view:'work'}}},toast:x=>messages.push(x),failText:x=>x.reason_code,
 closeWorkDrawer(){},loadWork(){refresh++},openWorkDrawer(){drawer++},loadHome(){},
 opCall:async(op,params)=>{calls.push({op,params});if(op==='run_request_payload')return {ok:true,data:{target_revision:2,payload:{goal:'x'}}};if(op==='submit_approval')return {ok:true};return {ok:false,reason_code:'blocked'}}};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{await context.approveAndStart(7);assert.deepEqual(calls.map(c=>c.op),['run_request_payload','submit_approval','start_run']);assert.equal(calls[1].params.grantee_id,'operator');assert.equal(calls[2].params.actor_id,'operator');assert.equal(refresh,1);assert.equal(drawer,1);assert.match(messages[0],/批准已记录/);})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script, str(frontend)], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_organization_switch_discards_old_knowledge_and_uses_current_work_identity():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for frontend JavaScript')
    frontend = Path(__file__).resolve().parents[1] / 'frontend' / 'index.html'
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
const source = html.slice(html.indexOf('const OG_TENANT='), html.indexOf('function openWorkItem('));
const nodes = {
  '#knSubject': {value: 'subject'},
  '#knSpace': {
    value: '', options: [],
    replaceChildren(...options) {this.options = options; this.value = options[0]?.value || '';},
    add(option) {this.options.push(option);},
  },
};
const $ = id => nodes[id] ||= {value: '', addEventListener(){}};
const requests = [];
let org = 'alpha', releaseAlpha, releaseSearch;
let owner = 'alpha-owner';
const context = {
  $, URLSearchParams,
  Option: function(text, value) {this.text = text; this.value = value;},
  localStorage: {getItem: () => org},
  document: {body: {dataset: {view: 'knowledge'}}},
  workIdentity: () => ({tenant: 'different-tenant', owner}),
  invalidateSkills(){},
  loadHome(){}, loadWork(){}, setView(){},
  esc: value => String(value),
  async fetchJson(path) {
    requests.push(path);
    if (path.includes('/view?')) return {ok: true, data: {buckets: {}}};
    if (path.includes('/knowledge?')) return new Promise(resolve => {releaseSearch = resolve;});
    if (path.endsWith('alpha')) return new Promise(resolve => {releaseAlpha = resolve;});
    const id = path.endsWith('beta') ? 'beta' : 'gamma';
    return {ok: true, data: {spaces: [{space_id: id, title: id+' space'}]}};
  },
};
vm.createContext(context);
vm.runInContext(source, context);
(async () => {
  const old = context.loadKnowledge();
  org = 'beta';
  owner = 'beta-owner';
  context.organizationScopeChanged();
  await context.loadKnowledge();
  releaseAlpha({ok: true, data:{spaces:[{space_id:'alpha',title:'old scope'}]}});
  await old;
  assert.equal(nodes['#knSpace'].value, 'beta');
  assert.equal(nodes['#knSync'].disabled, false);
  const oldSearch = context.searchKnowledge();
  org = 'gamma';
  owner = 'gamma-owner';
  context.organizationScopeChanged();
  await context.loadKnowledge();
  releaseSearch({ok: true, data: {claims: [{value:'old claim'}]}});
  await oldSearch;
  assert.equal(nodes['#knSpace'].value, 'gamma');
  assert.equal(nodes['#knBody'].textContent, '');
  assert.doesNotMatch(nodes['#knBody'].innerHTML || '', /old claim/);
  await context.loadGoals();
  assert.ok(requests.some(path => path.includes('org=gamma&tenant=different-tenant&owner=gamma-owner')));
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    result = subprocess.run(
        [node, '-e', script, str(frontend)], capture_output=True, text=True, encoding='utf-8',
    )
    assert result.returncode == 0, result.stderr


def test_compact_navigation_restores_focus_and_shares_the_view_router():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for frontend JavaScript')
    frontend = Path(__file__).resolve().parents[1] / 'frontend' / 'index.html'
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
const nav = html.slice(html.indexOf('const compactNav='), html.indexOf('function saveSessions()'));
const router = html.slice(html.indexOf('function setView('), html.indexOf('async function loadSkills('));
const nodes = {}, events = {}, routed = [];
let focused;
function $(id) {
  if(nodes[id])return nodes[id];
  const classes = new Set(), attrs = new Map();
  return nodes[id] = {
    classList:{
      contains: key=>classes.has(key),
      toggle: (key, enabled)=>enabled?classes.add(key):classes.delete(key),
    },
    handlers:{},
    addEventListener(name, handler){this.handlers[name]=handler;},
    getAttribute:key=>attrs.get(key),
    setAttribute:(key, val)=>attrs.set(key,val),
    removeAttribute:key=>attrs.delete(key),
    focus(){focused=id;},
  };
}
const routes = [['#desktopChat','chat'],['#mobileChat','chat'],['#desktopOrg','org']];
const tabs = routes.map(([id,view])=>Object.assign($(id),{dataset:{view}}));
const compact = {matches:true,addEventListener(name,handler){events[name]=handler;}};
const context = {
  $, matchMedia:()=>compact,
  addEventListener:(event, handler)=>{events[event]=handler;},
  document:{querySelector:$, querySelectorAll:()=>tabs, body:{dataset:{view:'home'}}},
  VIEWS:['home','work','chat','org'], HASH:{home:'',chat:'#/chat',org:'#/org'},
  location:{hash:'',pathname:'/'}, history:{replaceState(_state,_label,hash){routed.push(hash);}},
  loadOrg(){routed.push('org');},
  loadHome(){},
};
vm.createContext(context);
vm.runInContext(nav+'\n'+router,context);
assert.equal($('#workspaceNav').inert,true);
assert.equal($('.main').inert,false);
$('#mobileMore').onclick();
assert.equal($('#mobileMore').getAttribute('aria-expanded'),'true');
assert.equal($('#mobileMask').hidden,false);
assert.equal($('.main').inert,true);
assert.equal(focused,'#mobileClose');
$('#nav').handlers.click({target:{closest:()=>$('#desktopOrg')}});
assert.equal(context.document.body.dataset.view,'org');
assert.equal($('#desktopOrg').getAttribute('aria-current'),'page');
assert.equal($('#mobileMore').classList.contains('on'),true);
assert.equal($('.main').inert,false);
assert.equal(focused,'#mobileMore');
assert.deepEqual(routed,['#/org','org']);
$('#mobileDock').handlers.click({target:{closest:()=>$('#mobileChat')}});
assert.equal($('#desktopChat').getAttribute('aria-current'),'page');
assert.equal($('#mobileChat').getAttribute('aria-current'),'page');
assert.equal($('#desktopOrg').getAttribute('aria-current'),undefined);
$('#mobileMore').onclick();
events.keydown({key:'Escape'});
assert.equal($('#mobileMore').getAttribute('aria-expanded'),'false');
assert.equal(focused,'#mobileMore');
$('#mobileMore').onclick();
compact.matches=false;
events.change();
assert.equal($('#workspaceNav').inert,false);
assert.equal($('#mobileMask').hidden,true);
assert.equal($('.main').inert,false);
'''
    result = subprocess.run(
        [node, '-e', script, str(frontend)], capture_output=True, text=True, encoding='utf-8',
    )
    assert result.returncode == 0, result.stderr
