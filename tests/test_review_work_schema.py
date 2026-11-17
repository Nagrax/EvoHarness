import json
from pathlib import Path
import subprocess


def test_work_operation_checks_the_server_contract_before_mutation():
    html = (Path(__file__).resolve().parents[1] / 'frontend/index.html').read_text(encoding='utf-8')
    fn = html.split('async function opCall(op,params){', 1)[1].split('function failText', 1)[0]
    fn = 'async function opCall(op,params){' + fn
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const calls=[];
const ctx={apiFetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>url==='/api/work/schema'?{ok:true,data:{operations:{pause_work_item:{}}}}:{ok:true}}}};
vm.createContext(ctx);vm.runInContext(SOURCE,ctx);
(async()=>{
 const denied=await ctx.opCall('unknown',{task_id:1});
 assert.equal(denied.reason_code,'unknown_operation');assert.equal(calls.length,1);
 const allowed=await ctx.opCall('pause_work_item',{task_id:2});
 assert(allowed.ok);assert.equal(calls.length,3);
 assert.equal(calls[2].url,'/api/work/op');
 assert.deepEqual(JSON.parse(calls[2].options.body),{op:'pause_work_item',params:{task_id:2}});
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node','-e',script.replace('SOURCE',json.dumps(fn))],
        capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
