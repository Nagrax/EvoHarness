import pytest
from evoharness.work.agent_adapter import _translate_tool_input
from evoharness.work.tool_isolation import DockerShellRunner


def test_native_shell_syntax_is_preserved_for_the_container():
    command='printf one && printf two > answer.txt'
    assert _translate_tool_input('run_shell',{'command':command})['argv']==['sh','-lc',command]
    assert _translate_tool_input('run_shell',{'argv':['python','-V']})['argv']==['python','-V']


def test_native_command_chaining_executes_inside_the_isolated_workspace(tmp_path):
    runner=DockerShellRunner(image='python:3.13-slim')
    if not runner.available():
        pytest.skip('Linux Docker required')
    command='printf one > first.txt && cat first.txt | tr o O > second.txt'
    result=runner.run(tmp_path,_translate_tool_input('run_shell',{'command':command})['argv'])
    assert result.ok,result.detail
    assert (tmp_path/'first.txt').read_text()=='one'
    assert (tmp_path/'second.txt').read_text()=='One'
