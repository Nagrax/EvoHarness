"""DE-13 可信检查回执：strict 策略核对 exit/执行数/超时/被测 bundle。

复审要求（DE-13）：组织委派代码交付必须验证检查器 exit、实际执行数与被测
bundle，不能接受 echo 或任意自报统计；缺回执、ran=0、假统计、检查后源码
被改写、回执移植到别处都不得 done。

- enforce：无回执拒绝（既有契约）；
- strict：在 enforce 之上要求 exit_code==0、非超时、executions>=1、
  被测 bundle 摘要与当前 Run 工作区源码一致；
- grace（默认）：本地单用户演示兼容，既有流程不变。
全部离线、确定性。
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

CODE_ACCEPTANCE = '[{"kind": "code", "criteria": "回归通过"}]'


def _allow(s, c, a):
    return True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(acceptance.RECEIPT_ENFORCE_ENV, raising=False)
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
        run.checkpoint_json = json.dumps({"frozen": {"workspace": str(workspace)}})
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
    run.checkpoint_json = json.dumps({"frozen": {"workspace": str(workspace)}})
    s.flush()
    acceptance.request_acceptance(s, task.id, run_id=run.id, requester=actor)


def _receipt(workspace, **overrides):
    receipt = {
        "checker": "pytest-8.2",
        "checked_at": "2026-11-15T00:00:00",
        "summary_sha256": "a" * 64,
        "exit_code": 0,
        "timed_out": False,
        "executions": 1,
        "ran": 3,
        "node_ids": ["tests/test_mod.py::test_ok"],
        "bundle_sha256": integrity.workspace_bundle_digest(workspace),
    }
    receipt.update(overrides)
    return receipt


def _register(s, run, out_root, name, content, receipt=None):
    path = out_root / name
    path.write_text(content, encoding="utf-8")
    return acceptance.register_artifact(s, run, kind=name.rsplit(".", 1)[0],
                                        path=str(path), checker_receipt=receipt)


def _blocked_detail(s, task_id):
    rows = [e for e in s.scalars(select(TaskEvent).where(
        TaskEvent.task_id == task_id,
        TaskEvent.event_type == "acceptance_blocked"))]
    assert rows, "拒绝通过必须留下 acceptance_blocked 事件"
    return rows[-1].detail


def test_strict_accepts_enriched_receipt_and_done(env, monkeypatch):
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "strict")
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed",
              receipt=_receipt(workspace))
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert task.status == TaskStatus.DONE


def test_strict_rejects_nonzero_exit(env, monkeypatch):
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "strict")
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed",
              receipt=_receipt(workspace, exit_code=1))
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert task.status != TaskStatus.DONE
    assert "checker_exit_nonzero:test_report" in _blocked_detail(s, task.id)


@pytest.mark.parametrize("reported_ran", [0, None])
def test_strict_rejects_successful_process_without_executed_tests(env, reported_ran):
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    receipt = _receipt(workspace, ran=0, node_ids=[])
    if reported_ran is None:
        receipt.pop("ran")
    _register(s, run, out_root, "test_report.txt", "no tests ran", receipt=receipt)
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass", policy="strict")
    assert task.status != TaskStatus.DONE
    assert "checker_no_tests_ran:test_report" in _blocked_detail(s, task.id)


def test_strict_rejects_timeout_and_zero_executions(env, monkeypatch):
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "strict")
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "timeout",
              receipt=_receipt(workspace, timed_out=True, executions=0,
                               exit_code=None, ran=0))
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    detail = _blocked_detail(s, task.id)
    assert "checker_exit_unknown:test_report" in detail
    assert task.status != TaskStatus.DONE


def test_strict_rejects_bundle_changed_after_check(env, monkeypatch):
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "strict")
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed",
              receipt=_receipt(workspace))
    # 检查后源码被改写：旧检查结论不能证明当前 bundle
    (workspace / "src" / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert "checker_bundle_changed:test_report" in _blocked_detail(s, task.id)
    assert task.status != TaskStatus.DONE


def test_strict_rejects_removed_tests(env, monkeypatch):
    """检查过程中测试整条被删：检查通过不能代表原测试集通过。"""
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "strict")
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "1 passed",
              receipt=_receipt(workspace, baseline_tests=2, current_tests=1,
                               removed_node_ids=["tests/test_mod.py::test_gone"]))
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert "checker_tests_removed:test_report" in _blocked_detail(s, task.id)
    assert task.status != TaskStatus.DONE


def test_strict_rejects_modified_tests(env, monkeypatch):
    """原测试被改写（弱化断言/换实现）：改写后的通过不算原测试集通过。"""
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "strict")
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed",
              receipt=_receipt(workspace, baseline_tests=3, current_tests=3,
                               modified_node_ids=["tests/test_mod.py::test_ok"]))
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert "checker_tests_modified:test_report" in _blocked_detail(s, task.id)
    assert task.status != TaskStatus.DONE


def test_strict_accepts_receipt_with_no_removed_tests(env, monkeypatch):
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "strict")
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed",
              receipt=_receipt(workspace, baseline_tests=3, current_tests=3,
                               removed_node_ids=[]))
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert task.status == TaskStatus.DONE


def test_strict_rejects_legacy_receipt_without_execution_facts(env, monkeypatch):
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "strict")
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed",
              receipt={"checker": "legacy", "summary_sha256": "b" * 64})
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert "checker_exit_unknown:test_report" in _blocked_detail(s, task.id)
    assert task.status != TaskStatus.DONE


def test_enforce_without_strict_keeps_legacy_receipt_compatible(env, monkeypatch):
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "enforce")
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed",
              receipt={"checker": "legacy", "summary_sha256": "b" * 64})
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass")
    assert task.status == TaskStatus.DONE


def test_policy_parameter_overrides_env_without_global_mutation(env, monkeypatch):
    """部署装配按实例传 strict，不改环境变量（同进程测试不被污染）。"""
    monkeypatch.delenv(acceptance.RECEIPT_ENFORCE_ENV, raising=False)
    s, task, run, out_root, workspace = env
    _to_review(s, task, run, workspace)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed",
              receipt={"checker": "legacy", "summary_sha256": "b" * 64})
    assert not acceptance.receipts_enforced()
    acceptance.review(s, task.id, run_id=run.id, reviewer="bob", verdict="pass",
                      policy="strict")
    assert task.status != TaskStatus.DONE
    assert "checker_exit_unknown:test_report" in _blocked_detail(s, task.id)


def test_invalid_receipt_types_rejected():
    """回执字段类型错误在任何策略下都拒绝（不能把假统计写进库）。"""
    with pytest.raises(acceptance.AcceptanceError):
        acceptance._bind_checker_receipt(
            None, None, "test_report", None,
            {"checker": "x", "summary_sha256": "a" * 64, "exit_code": "0"})
    with pytest.raises(acceptance.AcceptanceError):
        acceptance._bind_checker_receipt(
            None, None, "test_report", None,
            {"checker": "x", "summary_sha256": "a" * 64,
             "removed_node_ids": "tests/test_mod.py::test_gone"})
    with pytest.raises(acceptance.AcceptanceError):
        acceptance._bind_checker_receipt(
            None, None, "test_report", None,
            {"checker": "x", "summary_sha256": "a" * 64,
             "modified_node_ids": "tests/test_mod.py::test_ok"})
