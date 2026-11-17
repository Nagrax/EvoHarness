import shutil
import subprocess
from pathlib import Path

import pytest


def test_scoped_skills_clear_stale_data_and_never_fallback_on_failure():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for frontend JavaScript')
    frontend = Path(__file__).resolve().parents[1] / 'frontend' / 'index.html'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const state=html.slice(html.indexOf('let skData=null'),html.indexOf('function setView('));
const functions=html.slice(html.indexOf('async function loadSkills()'),html.indexOf('function gateBar('));
const elements={};function $(selector){return elements[selector] ||= {value:'',hidden:false,innerHTML:'',textContent:'',style:{},classList:{add(){},remove(){},contains(){return false}},addEventListener(name,fn){this[name]=fn}}}
const calls=[];let pending;let reply;
const response=data=>({ok:true,json:async()=>({ok:true,data})});
const ctx={$,URLSearchParams,closeDrawer(){},addEventListener(){},workIdentity:()=>({tenant:'local',owner:'user'}),
 esc2:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
 apiFetch:url=>{calls.push(url);if(!reply)return new Promise(r=>pending=r);return Promise.resolve(reply)},renderSkills(){throw Error('legacy must not render')}};
vm.createContext(ctx);vm.runInContext(state+functions,ctx);
(async()=>{
 $('#skOrg').value='org-a';const old=ctx.loadSkills();
 $('#skOrg').value='org-b';$('#skOrg').input();
 reply=response({mode:'scoped',capabilities:{legacy_tools:false},scope:{org_id:'org-b',space_id:'s'},spaces:[{space_id:'s',title:'空间'}],has_report:false,skills:[{skill:'Safe',description:'<bad>',counts:{invocations:3,feedback:1,evolutions:0,snapshots:2},evaluation:null}],events:[]});
 await ctx.loadSkills();assert($('#skScopedView').innerHTML.includes('尚无该范围评测'));assert($('#skScopedView').innerHTML.includes('&lt;bad&gt;'));assert($('#skScopedView').innerHTML.includes('>3<'));
 pending(response({mode:'legacy',capabilities:{legacy_tools:true}}));await old;
 assert($('#skScopedView').innerHTML.includes('Safe'));assert(calls.every(x=>!x.startsWith('/api/skills')));
 reply=response({mode:'scoped',capabilities:{legacy_tools:false},scope:{org_id:'org-b',space_id:'s'},spaces:[{space_id:'s',title:'空间'}],has_report:true,skills:[{skill:'Safe',counts:{invocations:4},evaluation:{protocol_id:'p',run_id:'r',samples:2,checks:5,passed:4,pass_rate:.8}}],events:[]});
 await ctx.loadSkills();assert($('#skScopedView').innerHTML.includes('规则检查'));assert($('#skScopedView').innerHTML.includes('title="独立样本：2">4 / 5'));assert(!$('#skScopedView').innerHTML.includes('尚无该范围评测'));
 reply={ok:false,json:async()=>({ok:false,reason_code:'forbidden'})};await ctx.loadSkills();
 assert(!$('#skScopedView').innerHTML.includes('Safe'));assert($('#skScopedView').textContent.includes('权限'));assert($('#skLegacy').hidden);
 assert(calls.every(x=>!x.startsWith('/api/skills')));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script, str(frontend)], capture_output=True,
                            text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_mobile_skills_heading_does_not_overlap_navigation():
    node = shutil.which('node')
    chromium = Path('C:/Users/lenovo/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe')
    puppeteer = Path('D:/claudecode/scratch/btest/node_modules/puppeteer-core')
    if not node or not chromium.is_file() or not puppeteer.is_dir():
        pytest.skip('local Chromium and puppeteer are required for the layout check')
    frontend = Path(__file__).resolve().parents[1] / 'frontend' / 'index.html'
    script = r'''
const fs=require('fs'),puppeteer=require(process.argv[2]);
(async()=>{const b=await puppeteer.launch({headless:true,executablePath:process.argv[3],args:['--no-sandbox']});try{
 const p=await b.newPage();await p.setJavaScriptEnabled(false);await p.setViewport({width:390,height:844});
 await p.setContent(fs.readFileSync(process.argv[1],'utf8'));
 const result=await p.evaluate(()=>{document.body.dataset.view='skills';document.querySelector('.app').classList.add('side-closed');return {heading:document.querySelector('.sk-head h2').getBoundingClientRect().left,toggle:document.querySelector('#sideToggle').getBoundingClientRect().right,width:document.documentElement.scrollWidth}});
 if(result.heading<result.toggle||result.width>390)throw Error(JSON.stringify(result));
}finally{await b.close()}})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script, str(frontend), str(puppeteer), str(chromium)],
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr



def test_missing_optional_stage_hint_does_not_break_scoped_skills():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for frontend JavaScript')
    frontend = Path(__file__).resolve().parents[1] / 'frontend/index.html'
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const source=html.slice(html.indexOf('let skData=null'),html.indexOf('function setView('));
const nodes={};function $(key){if(key==='#stageTip')return null;return nodes[key]||=( {innerHTML:'stale',textContent:'stale',style:{},classList:{contains(){return false}}})}
const ctx={$,addEventListener(){},closeDrawer(){}};vm.createContext(ctx);vm.runInContext(source,ctx);ctx.clearSkills();
assert.equal(nodes['#skTable'].style.display,'none');assert.equal(nodes['#skScopedView'].innerHTML,'');
"""
    result = subprocess.run([node, '-e', script, str(frontend)], capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr


def test_navigation_displays_only_the_requested_product_view():
    node = shutil.which('node')
    chromium = Path('C:/Users/lenovo/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe')
    puppeteer = Path('D:/claudecode/scratch/btest/node_modules/puppeteer-core')
    if not node or not chromium.is_file() or not puppeteer.is_dir():
        pytest.skip('local Chromium and puppeteer are required for the layout check')
    frontend = Path(__file__).resolve().parents[1] / 'frontend/index.html'
    script = r"""
const fs=require('fs'),assert=require('assert'),puppeteer=require(process.argv[2]);
(async()=>{const b=await puppeteer.launch({headless:true,executablePath:process.argv[3],args:['--no-sandbox']});try{
const p=await b.newPage();await p.setJavaScriptEnabled(false);await p.setContent(fs.readFileSync(process.argv[1],'utf8'));
for(const [view,id] of Object.entries({home:'homeView',work:'workView',org:'orgView',goals:'goalsView',knowledge:'knowledgeView',skills:'skillsView',eval:'evalView',daily:'dailyView',chat:'chatView',approvals:'approvalView'})){
 const visible=await p.evaluate(view=>{document.body.dataset.view=view;return [...document.querySelectorAll('main>.view')].filter(e=>getComputedStyle(e).display!=='none').map(e=>e.id)},view);assert.deepEqual(visible,[id],view);
}}finally{await b.close()}})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node, '-e', script, str(frontend), str(puppeteer), str(chromium)],
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr
