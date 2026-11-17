import shutil
import subprocess
from pathlib import Path

import pytest


def test_workbench_labels_translate_known_codes_and_hide_unknown_codes():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for frontend JavaScript')
    frontend = Path(__file__).resolve().parents[1] / 'frontend' / 'index.html'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const source=html.slice(html.indexOf('function employeeStatusLabel('),html.indexOf('async function readWorkJson('));
const ctx={};vm.createContext(ctx);vm.runInContext(source,ctx);
assert.equal(ctx.employeeStatusLabel('active'),'在职');assert.equal(ctx.employeeStatusLabel('paused'),'已暂停');
assert.equal(ctx.employeeStatusLabel('revoked'),'已撤权');assert.equal(ctx.employeeStatusLabel('ended'),'已结束');
assert.equal(ctx.employeeStatusLabel('retired'),'已离任');
assert.equal(ctx.nextActionLabel('start_run'),'启动执行');assert.equal(ctx.nextActionLabel('claim_run'),'等待员工接单');
assert.equal(ctx.nextActionLabel('request_acceptance'),'提交验收');assert.equal(ctx.nextActionLabel('unknown_code'),'待处理');
'''
    result = subprocess.run([node, '-e', script, str(frontend)], capture_output=True,
                            text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
