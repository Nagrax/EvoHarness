"""EVO-03（DE-13）：参数化变体保护接入真实 pytest 全量 node 集。

缺口（deMapping DE-13）：参数化变体缩减（pytest_generate_tests / parametrize
常量被改）时测试函数自身 AST digest 不变，静态清点漏报。本回归固化：

- 回执记录真实收集的基线 node 集（检查器证据或适配器侧真实收集；无真实
  收集时回落静态清点并标记 ``static``）；
- strict 验收用真实 pytest --collect-only（实际收集器）对冻结工作区重新
  收集，与回执基线 node 集做差集：整条删除、变体缩减 → 明确拒绝；
- 收集器自身失败 → fail-closed（``checker_collection_failed``），不静默放行。

全部离线、确定性：真实收集器以子进程 pytest 运行，不依赖 Docker。
"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.models import Base, Task, TaskEvent, TaskStatus
from evoharness.work import acceptance, execution, integrity, service
from evoharness.work.agent_adapter import RestrictedAgentAdapter, _snapshot_workdir
from evoharness.work.models import ArtifactReceipt

CODE_ACCEPTANCE = '[{"kind": "code", "criteria": "回归通过"}]'


def _allow(s, c, a):
    return True


def _frozen(workspace) -> str:
    return json.dumps({"frozen": {"workspace": str(workspace)}})


@pytest.fixture()
def env(tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    out_root = tmp_path / "run-out"
    out_root.mkdir()
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    with factory.begin() as s:
        task = Task(task_key="k1", tenant="t", owner="alice", chat_id="chat",
                    title="修复", action="fix", status=TaskStatus.OPEN,
                    workflow_kind="delegated_delivery", goal="修复缺陷",
                    acceptance_json=CODE_ACCEPTANCE, revision=1, assignee_id="bob")
        s.add(task)
        s.flush()
        run = execution.request_run(s, task_id=task.id, context={"g": 1},
                                    actor_id="runner-1", output_root=str(out_root),
                                    idempotency_key="i1", validate_context=_allow)
        s.flush()
        run.checkpoint_json = _frozen(workspace)
        s.flush()
        yield s, task, run, out_root, workspace
    engine.dispose()


def _to_review(s, task, run, workspace, actor="bob"):
    service.transition_to(s, task.id, to_state="queued", actor=actor)
    claimed = execution.claim(s, run.id, worker="w1", validate_context=_allow)
    service.transition_to(s, task.id, to_state="running", actor=actor)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_allow)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="waiting_acceptance", validate_context=_allow)
    # 状态转换会重写 checkpoint_json；冻结工作区按最终检查点写入（生产同口径）
    run.checkpoint_json = _frozen(workspace)
    s.flush()
    acceptance.request_acceptance(s, task.id, run_id=run.id, requester=actor)


def _register(s, run, out_root, name, content, receipt=None):
    path = out_root / name
    path.write_text(content, encoding="utf-8")
    return acceptance.register_artifact(s, run, kind=name.rsplit(".", 1)[0],
                                        path=str(path), checker_receipt=receipt)


def _blocked_detail(s, task_id):
    rows = [e for e in s.scalars(select(TaskEvent).where(
        TaskEvent.task_id == task_id,
        TaskEvent.event_type == "acceptance_blocked"))]
    assert rows, "拒绝路径必须留下 acceptance_blocked 事件（旧检查在此静默放行）"
    return rows[-1].detail


def _variant_workspace(root, cases: str) -> None:
    """pytest_generate_tests + 模块级常量的参数化工作区：改常量不动测试函数。"""
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "conftest.py").write_text(
        "CASES = " + cases + "\n\n\n"
        "def pytest_generate_tests(metafunc):\n"
        "    metafunc.parametrize('v', CASES)\n",
        encoding="utf-8")
    (root / "tests" / "test_gen.py").write_text(
        "def test_v(v):\n    assert v\n",
        encoding="utf-8")


def _collect(workspace) -> list:
    result = integrity.collect_test_nodes(workspace)
    assert result["ok"], result
    return result["nodes"]


def _receipt(workspace, baseline_nodes, current_nodes, **overrides):
    """检查期回执：真实收集的基线/现状 node 集，差集事实按回执记录口径为空。"""
    receipt = {
        "checker": "pytest-9.1",
        "checked_at": "2026-11-21T00:00:00",
        "summary_sha256": "a" * 64,
        "exit_code": 0,
        "timed_out": False,
        "executions": 1,
        "ran": len(current_nodes),
        "node_ids": list(current_nodes),
        "baseline_tests": len(baseline_nodes),
        "current_tests": len(current_nodes),
        "removed_node_ids": [],
        "modified_node_ids": [],
        "baseline_node_ids": list(baseline_nodes),
        "baseline_node_source": "pytest",
        "bundle_sha256": integrity.workspace_bundle_digest(workspace),
    }
    receipt.update(overrides)
    return receipt


# ── ① 变体缩减但测试函数 AST 未变：旧逻辑放行、新检查拒绝 ──────────────────────


def test_strict_rejects_variant_reduction_when_function_ast_unchanged(env):
    s, task, run, out_root, workspace = env
    _variant_workspace(workspace, "['a', 'b', 'c']")
    baseline_nodes = _collect(workspace)
    assert len(baseline_nodes) == 3
    # 代理改动：pytest_generate_tests 的模块级常量缩减为 2 个变体；
    # tests/test_gen.py 的 AST 完全不变 → 静态 digest 清点漏报。
    _variant_workspace(workspace, "['a', 'b']")
    current_nodes = _collect(workspace)
    assert len(current_nodes) == 2
    assert {integrity.base_node_id(n) for n in baseline_nodes} == \
        {integrity.base_node_id(n) for n in current_nodes}
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "2 passed",
              receipt=_receipt(workspace, baseline_nodes, current_nodes))
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass",
                      policy="strict")
    assert "checker_variant_reduction:test_report" in _blocked_detail(s, task.id)
    assert task.status != TaskStatus.DONE


def test_strict_rejects_whole_test_removal_via_real_node_diff(env):
    """回执漏报被删事实时，真实收集差集独立拒绝整条删除。"""
    s, task, run, out_root, workspace = env
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n",
        encoding="utf-8")
    baseline_nodes = _collect(workspace)
    assert len(baseline_nodes) == 2
    (workspace / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8")
    current_nodes = _collect(workspace)
    assert len(current_nodes) == 1
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "1 passed",
              receipt=_receipt(workspace, baseline_nodes, current_nodes,
                               removed_node_ids=[]))
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass",
                      policy="strict")
    assert "checker_tests_removed:test_report" in _blocked_detail(s, task.id)
    assert task.status != TaskStatus.DONE


# ── ② 正常新增/不变：通过 ──────────────────────────────────────────────────────


def test_strict_accepts_when_nodes_unchanged_or_added(env):
    s, task, run, out_root, workspace = env
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8")
    baseline_nodes = _collect(workspace)
    # 新增一个测试文件：基线 node 全部仍在 → 差集为空，不拒绝。
    (workspace / "tests" / "test_b.py").write_text(
        "def test_b():\n    assert True\n", encoding="utf-8")
    current_nodes = _collect(workspace)
    assert set(baseline_nodes) < set(current_nodes)
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "2 passed",
              receipt=_receipt(workspace, baseline_nodes, current_nodes))
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass",
                      policy="strict")
    assert task.status == TaskStatus.DONE


# ── ③ 收集器自身失败：fail-closed 拒绝 ─────────────────────────────────────────


def test_strict_fails_closed_when_collector_errors(env):
    s, task, run, out_root, workspace = env
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (workspace / "tests" / "test_broken.py").write_text(
        "def test_broken(:\n", encoding="utf-8")
    result = integrity.collect_test_nodes(workspace)
    assert not result["ok"] and result["error"]
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "1 passed",
              receipt=_receipt(workspace, ["tests/test_ok.py::test_ok"],
                               ["tests/test_ok.py::test_ok"]))
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass",
                      policy="strict")
    assert "checker_collection_failed:test_report" in _blocked_detail(s, task.id)
    assert task.status != TaskStatus.DONE


def test_strict_fails_closed_on_unreadable_baseline_nodes(env):
    """基线 node 集损坏（非 JSON）：fail-closed，不当作无基线放行。"""
    s, task, run, out_root, workspace = env
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8")
    nodes = _collect(workspace)
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "1 passed",
              receipt=_receipt(workspace, nodes, nodes))
    receipt_row = s.scalar(select(ArtifactReceipt))
    receipt_row.baseline_node_ids_json = "not-json{"
    s.flush()
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass",
                      policy="strict")
    assert "checker_baseline_unreadable:test_report" in _blocked_detail(s, task.id)
    assert task.status != TaskStatus.DONE


# ── ④ 回执管道：真实收集基线进入回执 ───────────────────────────────────────────


def test_invalid_baseline_receipt_types_rejected():
    """基线 node 集/来源字段类型错误在任何策略下都拒绝（不能写进库）。"""
    with pytest.raises(acceptance.AcceptanceError):
        acceptance._bind_checker_receipt(
            None, None, "test_report", None,
            {"checker": "x", "summary_sha256": "a" * 64,
             "baseline_node_ids": "tests/test_a.py::test_a"})
    with pytest.raises(acceptance.AcceptanceError):
        acceptance._bind_checker_receipt(
            None, None, "test_report", None,
            {"checker": "x", "summary_sha256": "a" * 64,
             "baseline_node_source": "vibe"})


class _Report:
    ok = True
    exit_code = 0
    timed_out = False
    output = "2 passed in 0.01s"
    evidence: dict = {}


class _Runner:
    kind = "fake-checker"

    def __init__(self, evidence=None):
        self.evidence = evidence or {}

    def run(self, workdir, phase):
        report = _Report()
        report.evidence = self.evidence
        return report


def test_receipt_records_real_baseline_nodes_from_checker_evidence(tmp_path):
    """检查器证据带真实收集基线（TrustedPytestRunner 语义）→ 原样进回执。"""
    workdir = tmp_path / "ws"
    _variant_workspace(workdir, "['a', 'b', 'c']")
    baseline_nodes = _collect(workdir)
    current_nodes = baseline_nodes[:2]
    evidence = {"valid": True, "node_ids": current_nodes, "ran": 2,
                "baseline_nodes": baseline_nodes, "removed_node_ids": [],
                "modified_node_ids": [], "skipped_node_ids": [],
                "unexpected_skips": []}
    adapter = RestrictedAgentAdapter(gateway=object(), runner=_Runner(evidence))
    result = adapter._collect_result(
        workdir, {"brief": {"acceptance": [{"kind": "code"}]}},
        _snapshot_workdir(workdir), "done", [], None)
    assert result.ok is True
    receipt = adapter.last_receipts["test_report"]
    assert receipt["baseline_node_source"] == "pytest"
    assert receipt["baseline_node_ids"] == baseline_nodes
    assert receipt["baseline_tests"] == 3
    assert receipt["current_tests"] == 2


def test_receipt_records_real_baseline_nodes_from_adapter_collection(tmp_path):
    """注入检查器无基线能力 → 适配器侧真实收集结果进回执（变体级）。"""
    workdir = tmp_path / "ws"
    _variant_workspace(workdir, "['a', 'b', 'c']")
    adapter = RestrictedAgentAdapter(gateway=object(), runner=_Runner())
    # execute() 对无 prepare 的检查器做同款真实收集（EVO-03 管道）。
    adapter.last_baseline_collection = integrity.collect_test_nodes(workdir)
    result = adapter._collect_result(
        workdir, {"brief": {"acceptance": [{"kind": "code"}]}},
        _snapshot_workdir(workdir), "done", [], None)
    assert result.ok is True
    receipt = adapter.last_receipts["test_report"]
    assert receipt["baseline_node_source"] == "pytest"
    assert len(receipt["baseline_node_ids"]) == 3
    assert all("[" in node.rpartition("::")[2] for node in receipt["baseline_node_ids"])


def test_receipt_falls_back_to_static_inventory_without_real_collection(tmp_path):
    """无真实收集（旧路径）→ 回落静态清点并标记 static，事实不编造。"""
    workdir = tmp_path / "ws"
    _variant_workspace(workdir, "['a', 'b', 'c']")
    adapter = RestrictedAgentAdapter(gateway=object(), runner=_Runner())
    result = adapter._collect_result(
        workdir, {"brief": {"acceptance": [{"kind": "code"}]}},
        _snapshot_workdir(workdir), "done", [], None)
    assert result.ok is True
    receipt = adapter.last_receipts["test_report"]
    assert receipt["baseline_node_source"] == "static"
    assert receipt["baseline_node_ids"] == ["tests/test_gen.py::test_v"]
    assert receipt["baseline_tests"] == 1
