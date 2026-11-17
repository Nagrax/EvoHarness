"""Delayed approval responses must not change the organization of an action."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_approval_response_and_revoke_are_bound_to_current_scope():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for frontend JavaScript')
    frontend = Path(__file__).resolve().parents[1] / 'frontend/index.html'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const source=html.slice(html.indexOf('let approvalRequestSequence='),html.indexOf('/* ═════════ work view'));
const input={value:'org-A',listeners:{},addEventListener(name,fn){this.listeners[name]=fn}};
const box={innerHTML:'',textContent:'',querySelectorAll(){
 const matches=[...this.innerHTML.matchAll(/data-type="([^"]+)" data-target="([^"]+)"/g)];
 this.buttons=matches.map(m=>({dataset:{type:m[1],target:m[2]},disabled:false}));
 return this.buttons;
}};
const refresh={},pending=[],ops=[],messages=[];
const $=id=>({'#approvalOrg':input,'#approvalResults':box,'#approvalRefresh':refresh}[id]);
const context={$,localStorage:{getItem(){return ''}},esc2:x=>String(x??''),
 readWorkJson:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),
 workIdentity:()=>({owner:'reviewer'}),failText:x=>x.reason_code,toast:x=>messages.push(x),
 opCall:async(op,params)=>{ops.push({op,params});return {ok:true,data:{count:1}}}};
vm.createContext(context);vm.runInContext(source,context);
const row=id=>({target_type:'work_item',target_id:id,actions:['work.execute'],target_revision:1});
(async()=>{
 const a=context.loadApprovalView();
 input.value='org-B';const b=context.loadApprovalView();
 pending[1].resolve({approvals:[row('B')]});await b;
 const buttonB=box.buttons[0];
 pending[0].resolve({approvals:[row('A')]});await a;
 assert.match(box.innerHTML,/work_item#B/);assert.doesNotMatch(box.innerHTML,/work_item#A/);
 await buttonB.onclick();assert.equal(ops.length,1);assert.equal(ops[0].params.org_id,'org-B');
 // Even a retained DOM/event reference must not act after an unqueried edit.
 context.readWorkJson=async()=>({approvals:[row('A')]});
 input.value='org-A';await context.loadApprovalView();const stale=box.buttons[0];
 input.value='org-B';await stale.onclick();assert.equal(ops.length,1);
 assert.match(messages.at(-1),/组织已变更/);
 input.listeners.input();assert.match(box.textContent,/重新查询/);
 // A stale error response must not replace the new organization's records.
 context.readWorkJson=url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject}));
 input.value='org-A';const failed=context.loadApprovalView();const old=pending.at(-1);
 input.value='org-B';const newest=context.loadApprovalView();
 pending.at(-1).resolve({approvals:[row('B')]});await newest;
 old.reject(new Error('old organization error'));await failed;
 assert.match(box.innerHTML,/work_item#B/);
 assert.doesNotMatch(box.textContent,/old organization error/);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script, str(frontend)], capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr



def test_task_board_approval_response_and_actions_preserve_scope():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for frontend JavaScript')
    frontend = Path(__file__).resolve().parents[1] / 'frontend/index.html'
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const source=html.slice(html.indexOf('let workApprovalSequence='),html.indexOf('let workDrawerTask='));
let org='A';const pending=[],ops=[],messages=[];
const box={innerHTML:'',querySelectorAll(){this.buttons=[...this.innerHTML.matchAll(/data-type="([^"]+)" data-target="([^"]+)"/g)].map(m=>({dataset:{type:m[1],target:m[2]},addEventListener(n,fn){this[n]=fn}}));return this.buttons}};
const ctx={$:()=>box,workOrg:()=>org,workIdentity:()=>({owner:'reviewer'}),esc2:x=>String(x??''),toast:x=>messages.push(x),
 apiFetch:url=>new Promise(resolve=>pending.push({url,resolve})),opCall:async(op,params)=>{ops.push({op,params});return {ok:true,data:{count:1}}}};
vm.createContext(ctx);vm.runInContext(source,ctx);
const response=id=>({json:async()=>({ok:true,data:{approvals:[{decision_id:id,target_type:'work_item',target_id:id,actions:['work.execute'],target_revision:1}]}})});
(async()=>{const a=ctx.loadApprovals('A');org='B';const b=ctx.loadApprovals('B');pending[1].resolve(response('B'));await b;
 const button=box.buttons[0];pending[0].resolve(response('A'));await a;assert.match(box.innerHTML,/work_item#B/);assert.doesNotMatch(box.innerHTML,/work_item#A/);
 org='A';await button.click();assert.equal(ops.length,0);assert.match(messages[0],/组织已变更/);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node, '-e', script, str(frontend)], capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
