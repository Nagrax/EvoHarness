import shutil
import subprocess
from pathlib import Path

import pytest


def test_work_drawer_shows_business_labels_and_preserves_technical_evidence():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for frontend JavaScript')
    frontend = Path(__file__).resolve().parents[1] / 'frontend' / 'index.html'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const labels=html.slice(html.indexOf('function employeeStatusLabel('),html.indexOf('async function readWorkJson('));
const drawer=html.slice(html.indexOf('let workDrawerTask=null;'),html.indexOf('async function approveAndStart('));
const nodes={};
function $(s){return nodes[s] ||= {classList:{add(){}},addEventListener(){}}}
const data={id:3,title:'任务',revision:2,workflow_kind:'delegated_delivery',goal:'目标',
 display:{reason_code:'ready_to_execute',label:'待启动',next_action:'claim_run'},waiting_reason:'private reason',
 acceptance:[{kind:'code',criteria:'回归全部通过',description:'次要描述'},{kind:'code'}],runs:[{run_id:'run-secret',state:'queued',mode:'live',attempts:2,generation:4}],
 allowed_transitions:['paused','cancelled'],receipts:[]};
const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const ctx={$,esc,esc2:esc,badge:(_,label)=>label,apiFetch:async()=>({json:async()=>({ok:true,data})})};
vm.createContext(ctx);vm.runInContext(labels+drawer,ctx);
(async()=>{await ctx.openWorkDrawer(3);const body=nodes['#workDrawerBody'].innerHTML;
 const [visible,technical]=body.split('<details class="wk-details" id="wdTechnical">');
 assert(visible.includes('下一步：等待员工接单'));assert(visible.includes('执行记录'));
 assert(visible.includes('回归全部通过'));assert(!visible.includes('次要描述'));assert(visible.includes('代码验收'));
 assert(visible.includes('等待接单'));assert(visible.includes('执行次数：2'));
 for(const code of ['claim_run','queued','live','run-secret','private reason','attempt']) assert(!visible.includes(code),code);
 for(const code of ['claim_run','queued','live','run-secret','private reason','generation']) assert(technical.includes(code),code);
 assert(technical.startsWith('<summary>技术详情</summary>'));
 for(const id of ['wdApprove','wdPause','wdCancel','wdNote','wdEvidence','wdCost']) assert(body.includes(id),id);
 assert(!html.includes('ehFooterVer'));assert(!html.includes('EvoHarness · 本地闭环版'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script, str(frontend)], capture_output=True,
                            text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
