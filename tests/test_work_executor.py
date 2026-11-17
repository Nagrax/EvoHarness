"""DE-11：受限执行器——从任务真实运行到产物，成功不等于验收。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.models import Base, Task, TaskStatus
from evoharness.work import acceptance, execution, executor, service


def _allow(s, c, a):
    return True


@pytest.fixture()
def env(tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bug.txt").write_text("broken", encoding="utf-8")
    with factory.begin() as s:
        task = Task(task_key="k1", tenant="t", owner="alice", chat_id="chat", title="修复",
                    action="fix", status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                    goal="修复 bug.txt 并补回归", acceptance_json='[{"kind": "code", "criteria": "回归通过"}]',
                    revision=1, assignee_id="bob")
        s.add(task)
        s.flush()
        run = execution.request_run(s, task_id=task.id, context={"g": 1}, actor_id="runner-1",
                                    output_root=str(tmp_path / "out"), idempotency_key="i1",
                                    validate_context=_allow)
        s.flush()
        service.transition_to(s, task.id, to_state="queued", actor="bob")
        yield s, task, run, tmp_path, repo
    engine.dispose()


class SyntheticFixer:
    """合成执行器：在隔离工作区内真实产出补丁与测试报告文件。"""
    kind = "synthetic"

    def execute(self, workdir, context, allowed_tools, guard):
        assert "write_file" in allowed_tools
        target = Path(guard(str(Path(workdir) / "bug.txt")))
        target.write_text("fixed", encoding="utf-8")
        diff = Path(guard(str(Path(workdir) / "diff.patch")))
        diff.write_text("--- bug.txt\n+++ bug.txt\n@@ -1 +1 @@\n-broken\n+fixed\n", encoding="utf-8")
        report = Path(guard(str(Path(workdir) / "test_report.txt")))
        report.write_text("3 passed", encoding="utf-8")
        return executor.AdapterResult(
            ok=True, claimed_done=True,
            artifacts=[("diff", "diff.patch"), ("test_report", "test_report.txt")],
            checkpoint={"done": ["复现", "修复", "回归"], "todo": [],
                        "diff": "bug.txt", "next": "提交验收",
                        "side_effects": []})


def test_synthetic_defect_runs_to_accepted_patch(env):
    s, task, run, tmp_path, repo = env
    result = executor.execute_run(s, run.id, adapter=SyntheticFixer(), worker="w1",
                                  source_tree=repo,
                                  validate_context=_allow)
    assert result["claimed"] and result["ok"]
    assert set(result["artifacts"]) == {"diff", "test_report"}
    # 隔离工作区：源树被复制、原仓库不被改动
    assert (repo / "bug.txt").read_text(encoding="utf-8") == "broken"
    workdir = Path(result["workspace"])
    assert (workdir / "bug.txt").read_text(encoding="utf-8") == "fixed"
    # 检查点固化版本与结构化进展（不只是对话摘要）
    session_run = s.get(type(run), run.id)
    checkpoint = __import__("json").loads(session_run.checkpoint_json)
    assert checkpoint["frozen"]["task_revision"] == 1
    assert checkpoint["done"] == ["复现", "修复", "回归"]
    # Run 成功 ≠ 业务 done：停在 waiting_acceptance
    assert session_run.state == "waiting_acceptance"
    assert task.status != TaskStatus.DONE
    # 验收通过后才 done（唯一入口）
    acceptance.request_acceptance(s, task.id, run_id=run.id, requester="bob")
    acceptance.review(s, task.id, run_id=run.id, reviewer="alice",
                      verdict="pass", notes="差异与回归齐备")
    assert task.status == TaskStatus.DONE


def test_qa_run_has_no_write_permission(env):
    s, task, run, tmp_path, repo = env
    task.workflow_kind = "qa"
    s.flush()

    class FakeQaWriter:
        kind = "qa"
        def execute(self, workdir, context, allowed_tools, guard):
            assert "write_file" not in allowed_tools and "run_shell" not in allowed_tools
            with pytest.raises(PermissionError):
                guard(str(Path(workdir) / "out.txt"))
            return executor.AdapterResult(ok=True, claimed_done=True,
                                           artifacts=[("answer", "answer.md")])

    class QaAdapter(FakeQaWriter):
        def execute(self, workdir, context, allowed_tools, guard):
            super().execute(workdir, context, allowed_tools, guard)
            Path(workdir, "answer.md").write_text("答案：见 evidence", encoding="utf-8")
            return executor.AdapterResult(ok=True, claimed_done=True,
                                           artifacts=[("answer", "answer.md")])

    result = executor.execute_run(s, run.id, adapter=QaAdapter(), worker="w1",
                                  validate_context=_allow)
    assert result["ok"]


def test_workspace_escape_rejected(env):
    s, task, run, tmp_path, repo = env
    assert pytest.raises(PermissionError, lambda: executor.guard_path(
        [str(tmp_path / "exec")], str(tmp_path / "outside.txt"))).value.args[0] == "write_outside_roots"
    assert executor.guard_path([str(tmp_path / "exec")], str(tmp_path / "exec" / "a.txt"))


def test_claimed_done_without_artifacts_fails_visibly(env):
    s, task, run, tmp_path, repo = env

    class Liar:
        kind = "liar"
        def execute(self, workdir, context, allowed_tools, guard):
            return executor.AdapterResult(ok=True, claimed_done=True, artifacts=[])

    result = executor.execute_run(s, run.id, adapter=Liar(), worker="w1",
                                  validate_context=_allow)
    assert not result["ok"] and result["error"] == "claimed_done_without_artifacts"
    session_run = s.get(type(run), run.id)
    assert session_run.state == "failed"
    assert task.status != TaskStatus.DONE


def test_adapter_crash_records_failure(env):
    s, task, run, tmp_path, repo = env

    class Crasher:
        kind = "crasher"
        def execute(self, workdir, context, allowed_tools, guard):
            raise RuntimeError("model timeout")

    result = executor.execute_run(s, run.id, adapter=Crasher(), worker="w1",
                                  validate_context=_allow)
    assert not result["ok"] and "model timeout" in result["error"]
    session_run = s.get(type(run), run.id)
    assert session_run.state == "failed"
    checkpoint = __import__("json").loads(session_run.checkpoint_json)
    assert checkpoint["error"]


def test_parallel_runs_use_separate_workspaces(env):
    s, task, run, tmp_path, repo = env
    task2 = Task(task_key="k2", tenant="t2", owner="carol", chat_id="chat2", title="任务2",
                 action="fix2", status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                 goal="另一个空间的任务", acceptance_json='[{"kind": "code"}]', revision=1)
    s.add(task2)
    s.flush()
    run2 = execution.request_run(s, task_id=task2.id, context={"g": 2}, actor_id="runner-2",
                                 output_root=str(tmp_path / "out2"), idempotency_key="j1",
                                 validate_context=_allow)
    service.transition_to(s, task2.id, to_state="queued", actor="carol")
    first = executor.execute_run(s, run.id, adapter=SyntheticFixer(), worker="w1",
                                 source_tree=repo,
                                 validate_context=_allow)
    second = executor.execute_run(s, run2.id, adapter=SyntheticFixer(), worker="w2",
                                  source_tree=repo,
                                  validate_context=_allow)
    assert first["workspace"] != second["workspace"]
    assert Path(first["workspace"]) != Path(second["workspace"])
