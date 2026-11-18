"""The search port consumes product Worker/acceptance facts, not model claims."""

import hashlib
import pytest

from agents.skill_evolution import skill_content_hash
from evoharness.work.agent_adapter import FakeProvider
from evoharness.work.tool_isolation import DockerShellRunner
from eval.worker_run import run_case


def request(kind="qa"):
    snapshot = dict(
        name="refund",
        description="退款规则核对",
        when_to_use="退款问题",
        instructions="先读取规则，再按证据答复。",
    )
    return dict(
        candidate=dict(
            candidate_id="candidate-1",
            snapshot=snapshot,
            content_hash=skill_content_hash(snapshot),
        ),
        question="核对退款规则",
        kind=kind,
        input_files=[],
    )


def test_actual_load_and_independent_product_acceptance(tmp_path):
    provider = FakeProvider(
        [
            {"tool_calls": [{"name": "skill", "arguments": {"skill_name": "refund"}}]},
            {"content": "按规则完成答复。", "final": True},
        ]
    )
    result = run_case(request(), tmp_path, provider=provider)
    assert result["artifact_verdict"]["verified"], result
    assert result["loaded_snapshot"] == request()["candidate"]["snapshot"]
    assert result["tokens"]["calls"] == 2 and result["tokens"]["used_tokens"] == 240
    assert result["provider_kind"] == "synthetic"
    assert any(event["tool"] == "skill" for event in result["tool_events"])


def test_no_load_never_gets_candidate_loading_receipt(tmp_path):
    result = run_case(
        request(),
        tmp_path,
        provider=FakeProvider([{"content": "答复。", "final": True}]),
    )
    assert result["loaded_snapshot"] == {}


def test_frozen_input_cannot_reference_a_host_path(tmp_path):
    item = request()
    content = "x"
    item["input_files"] = [
        dict(
            path="../outside.txt",
            content=content,
            sha256=hashlib.sha256(content.encode()).hexdigest(),
        )
    ]
    with pytest.raises(ValueError, match="eval_input_path_invalid"):
        run_case(item, tmp_path, provider=FakeProvider([]))
    assert not (tmp_path.parent / "outside.txt").exists()


def test_real_checker_refuses_false_success(tmp_path):
    if not DockerShellRunner(image="evoharness-checker:latest").available():
        pytest.skip("Linux Docker checker is required")
    item = request("code")
    text = "def test_failing():\n    assert 1 == 2\n"
    item["input_files"] = [
        dict(
            path="tests/test_original.py",
            content=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
    ]
    provider = FakeProvider(
        [
            {"tool_calls": [{"name": "skill", "arguments": {"skill_name": "refund"}}]},
            {"content": "All tests passed.", "final": True},
        ]
    )
    result = run_case(item, tmp_path, provider=provider)
    assert not result["artifact_verdict"]["verified"], result
    assert result["result"]["run_state"] == "failed"
    assert result["result"]["error"] == "check_failed"
    assert result["loaded_snapshot"] == item["candidate"]["snapshot"]


def test_real_code_execution_preserves_tests_and_imports_project(tmp_path):
    if not DockerShellRunner(image="evoharness-checker:latest").available():
        pytest.skip("Linux Docker checker is required")
    item=request('code')
    files={'calc.py':'def add(a,b):\n    return a-b\n',
           'tests/test_calc.py':'from calc import add\n\ndef test_add():\n    assert add(2,3)==5\n'}
    item['input_files']=[dict(path=n,content=v,sha256=hashlib.sha256(v.encode()).hexdigest())
                         for n,v in files.items()]
    provider=FakeProvider([
        {'tool_calls':[{'name':'skill','arguments':{'skill_name':'refund'}}]},
        {'tool_calls':[{'name':'edit_file','arguments':{'path':'calc.py','old':'return a-b','new':'return a+b'}}]},
        {'content':'Fixed the implementation.','final':True}])
    result=run_case(item,tmp_path,provider=provider)
    assert result['artifact_verdict']['verified'],result
    assert result['result']['run_state']=='waiting_acceptance'
    assert {row['kind'] for row in result['artifacts']}=={'diff','test_report'}
