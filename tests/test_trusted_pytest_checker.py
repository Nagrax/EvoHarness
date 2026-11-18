"""An echo of passed counts cannot replace full parametrized execution evidence."""
import json
from pathlib import Path

import pytest

from evoharness.work.checker import MARKER, TrustedPytestRunner, parse_evidence
from evoharness.work.integrity import test_support_digests as support_digests


def test_passed_stdout_without_controller_and_junit_is_not_evidence(tmp_path):
    result = parse_evidence('1200 passed',tmp_path/'missing.xml',0)
    assert not result['valid']


def test_mismatched_junit_parametrized_inventory_is_rejected(tmp_path):
    report = tmp_path/'junit.xml'
    report.write_text('<testsuite><testcase file="test_a.py" classname="test_a" name="test_a[0]"/></testsuite>',encoding='utf-8')
    receipt = {'version':1,'collected':['test_a.py::test_a[1]'],
        'reports':[{'nodeid':'test_a.py::test_a[1]','when':'call','outcome':'passed'}],'exit_code':0}
    result = parse_evidence(MARKER+json.dumps(receipt),report,0)
    assert not result['valid'] and result['error']=='checker_junit_nodes_mismatch'


def test_fixture_and_parameter_data_changes_are_detected():
    original = {'tests/conftest.py':'VALUES=[1,2]\n',
                'tests/test_a.py':'def test_ok():\n assert True\n','pytest.ini':'[pytest]\n'}
    changed = {**original,'tests/conftest.py':'VALUES=[1]\n','pytest.ini':'[pytest]\naddopts=-k nothing\n'}
    before,after=support_digests(original),support_digests(changed)
    assert {key for key in before if before[key]!=after[key]} == {
        'test-source:tests/conftest.py','test-source:pytest.ini'}


def test_real_isolated_parameter_collection_and_source_protection(tmp_path):
    runner = TrustedPytestRunner()
    if not runner.shell_runner.available():
        pytest.skip('Linux checker image unavailable')
    directory = tmp_path/'tests'
    directory.mkdir()
    (directory/'conftest.py').write_text('VALUES=[1,2]\n',encoding='utf-8')
    (directory/'test_math.py').write_text('import pytest\nfrom conftest import VALUES\n'
        '@pytest.mark.parametrize("value",VALUES)\ndef test_value(value):\n assert value>0\n',encoding='utf-8')
    baseline=runner.prepare(tmp_path)
    assert baseline.evidence['valid'],baseline.output
    assert len(baseline.evidence['node_ids'])==2
    green=runner.run(tmp_path,'gate')
    assert green.ok and green.ran==2,green.output
    (directory/'conftest.py').write_text('VALUES=[1]\n',encoding='utf-8')
    reduced=runner.run(tmp_path,'gate')
    assert not reduced.ok
    assert reduced.evidence['removed_node_ids']==['tests/test_math.py::test_value[2]']
    assert 'test-source:tests/conftest.py' in reduced.evidence['modified_node_ids']
