"""Audit tools must inspect the checkout containing the tool, including worktrees."""
import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize('name', [
    'api_cases_depth', 'api_coverage', 'assess_long_functions', 'audit_test_isolation',
    'curve_summary', 'eval_skills_cli', 'extract_flaky_pool', 'frontend_api_map',
    'git_preflight', 'make_difficulty_ladder', 'make_longdoc_task',
    'make_reflection_tasks', 'make_reflection_tasks_v2', 'make_trap_tasks',
    'paired_mcnemar', 'power_analysis', 'probe_readfile_usage', 'reflection_learn_ab',
])
def test_audit_root_tracks_the_owning_checkout(name, tmp_path):
    root = Path(__file__).resolve().parents[1]
    file = root / 'eval' / (name + '.py')
    tree = ast.parse(file.read_text(encoding='utf-8'))
    value = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'REPO' for t in n.targets))
    relocated = tmp_path / 'another-checkout' / 'eval' / file.name
    actual = eval(compile(ast.Expression(value), str(file), 'eval'),
                  {'Path': Path, '__file__': str(relocated)})
    assert actual == relocated.resolve().parents[1]


def test_evolution_override_isolation_does_not_mask_direct_cwd_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from eval import audit_test_isolation as audit
    monkeypatch.setattr(audit, 'REPO', tmp_path)
    monkeypatch.setattr(audit, 'TESTS', tmp_path)
    (tmp_path / 'test_evolution.py').write_text(
        'monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(tmp_path))\nset_tier("x", "core")\n',
        encoding='utf-8')
    (tmp_path / 'test_direct.py').write_text(
        'monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(tmp_path))\nPath.cwd().joinpath("x").write_text("x")\n',
        encoding='utf-8')
    (tmp_path / 'test_memory.py').write_text(
        'monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(tmp_path))\nget_memory_dir().joinpath("x").write_text("x")\n',
        encoding='utf-8')
    assert [row['file'] for row in audit.audit()] == ['test_direct.py', 'test_memory.py']
