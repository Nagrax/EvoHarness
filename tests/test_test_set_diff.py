"""DE-13：被删测试不得 done——测试集差集清点与回执事实（离线、确定性）。

回执除 exit/执行数/bundle 外，再记录检查前基线 node 数、检查后现状与
被删 node id；strict 验收据此拒绝。静态清点不执行测试，只证明「整条测试
消失」，参数化变体不在差集口径内（不冒充全部弱化检测）。
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness.work import integrity
from evoharness.work.agent_adapter import RestrictedAgentAdapter, _snapshot_workdir


def test_node_digests_detect_body_change_but_not_reformat():
    before = {"tests/test_a.py": "def test_one():\n    assert 1 + 1 == 2\n"}
    reformatted = {"tests/test_a.py": "def test_one():\n\n    # 注释不影响 AST\n    assert 1 + 1 == 2\n"}
    weakened = {"tests/test_a.py": "def test_one():\n    assert True\n"}
    base = integrity.test_node_digests(before)
    assert base == integrity.test_node_digests(reformatted)
    assert base != integrity.test_node_digests(weakened)


def test_inventory_counts_module_and_class_tests():
    snap = {
        "tests/test_a.py": "def test_one():\n    assert True\n\n"
                           "def helper():\n    pass\n\n"
                           "class TestGroup:\n"
                           "    def test_two(self):\n        pass\n"
                           "    def not_a_test(self):\n        pass\n",
        "tests/helper.py": "def test_hidden():\n    pass\n",
        "src/app.py": "def test_not_collected():\n    pass\n",
        "tests/test_broken.py": "def test_broken(:\n",
    }
    assert integrity.test_node_inventory(snap) == [
        "tests/test_a.py::test_one",
        "tests/test_a.py::TestGroup::test_two",
    ]


class _Report:
    ok = True
    exit_code = 0
    timed_out = False
    output = "3 passed in 0.01s"


class _Runner:
    kind = "fake-checker"

    def run(self, workdir, phase):
        return _Report()


def test_adapter_receipt_records_removed_nodes(tmp_path):
    workdir = tmp_path / "ws"
    (workdir / "tests").mkdir(parents=True)
    (workdir / "src").mkdir()
    (workdir / "src" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workdir / "tests" / "test_mod.py").write_text(
        "def test_keep():\n    pass\n\ndef test_gone():\n    pass\n",
        encoding="utf-8")
    baseline = _snapshot_workdir(workdir)
    (workdir / "tests" / "test_mod.py").write_text(
        "def test_keep():\n    pass\n", encoding="utf-8")
    adapter = RestrictedAgentAdapter(gateway=object(), runner=_Runner())
    result = adapter._collect_result(
        workdir, {"brief": {"acceptance": [{"kind": "code"}]}},
        baseline, "done", ["edit_file:ok:tests/test_mod.py"], None)
    assert result.ok is True
    receipt = adapter.last_receipts["test_report"]
    assert receipt["baseline_tests"] == 2
    assert receipt["current_tests"] == 1
    assert receipt["removed_node_ids"] == ["tests/test_mod.py::test_gone"]


def test_adapter_receipt_records_modified_nodes(tmp_path):
    workdir = tmp_path / "ws"
    (workdir / "tests").mkdir(parents=True)
    (workdir / "tests" / "test_mod.py").write_text(
        "def test_keep():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    baseline = _snapshot_workdir(workdir)
    (workdir / "tests" / "test_mod.py").write_text(
        "def test_keep():\n    assert True\n", encoding="utf-8")
    adapter = RestrictedAgentAdapter(gateway=object(), runner=_Runner())
    adapter._collect_result(
        workdir, {"brief": {"acceptance": [{"kind": "code"}]}},
        baseline, "done", ["edit_file:ok:tests/test_mod.py"], None)
    receipt = adapter.last_receipts["test_report"]
    assert receipt["baseline_tests"] == 1
    assert receipt["current_tests"] == 1
    assert receipt["removed_node_ids"] == []
    assert receipt["modified_node_ids"] == ["tests/test_mod.py::test_keep"]


def test_adapter_receipt_empty_when_tests_added(tmp_path):
    workdir = tmp_path / "ws"
    (workdir / "tests").mkdir(parents=True)
    (workdir / "tests" / "test_mod.py").write_text(
        "def test_a():\n    pass\n", encoding="utf-8")
    baseline = _snapshot_workdir(workdir)
    (workdir / "tests" / "test_mod.py").write_text(
        "def test_a():\n    pass\n\ndef test_b():\n    pass\n", encoding="utf-8")
    adapter = RestrictedAgentAdapter(gateway=object(), runner=_Runner())
    adapter._collect_result(
        workdir, {"brief": {"acceptance": [{"kind": "code"}]}},
        baseline, "done", ["edit_file:ok:tests/test_mod.py"], None)
    receipt = adapter.last_receipts["test_report"]
    assert receipt["baseline_tests"] == 1
    assert receipt["current_tests"] == 2
    assert receipt["removed_node_ids"] == []
