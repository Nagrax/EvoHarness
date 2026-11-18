"""Headless Worker tool rendering also works on legacy Windows pipes."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("encoding", ["cp936", "ascii", "utf-8"])
def test_tool_display_does_not_skip_execution_on_legacy_output(encoding):
    script = """
from agents.ui import print_tool_call, print_tool_result, print_assistant_text
from agents.tools import check_permission
print_tool_call('skill', {'skill_name': 'inquiry-handoff'})
assert check_permission('list_files', {}, 'default')['action'] == 'allow'
print_tool_call('read_file', {'file_path': '\\u8be2\\u4ef7\\U0001f4e6.json'})
print_tool_result('skill', '\\u4ef7\\u683c\\U0001f4e6')
print_assistant_text('\\u5b8c\\u6210\\U0001f4e6')
print('executed')
"""
    environment = dict(os.environ, PYTHONIOENCODING=encoding + ":strict")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert b"executed" in result.stdout
