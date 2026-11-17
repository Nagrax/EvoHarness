"""R07 回归：验收不复核已登记产物——文件被改写/删除/旧代际后不得 review pass。

覆盖审核探针 acceptance_artifact_integrity 的负例与修复要求：
- 探针负例（登记后改写 diff + 删除 test_report）→ 拒绝 pass，Task 不 done；
- 单独删除/篡改产物 → artifact_missing / hash_mismatch；
- Run generation 前进后旧产物拒绝（generation_stale），重登记后可恢复；
- 失败/暂停 Run 不允许验收（run_not_verifiable），取消任务不可验收；
- 无回执 test_report 在 enforce 下拒绝（receipt_missing）；合法带回执通过；
- 回执与产物失绑一律拒绝（receipt_unbound，grace 下同样生效）；
- 同 ID 重登记 hash 复核：内容已变必须反映当前事实，不得复用旧 hash；
- 正常未篡改流程仍可 done（正向不破坏）。
全部离线、确定性。
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from evoharness.models import Base, Task, TaskEvent, TaskStatus
from evoharness.work import acceptance, execution, service
from evoharness.work.acceptance import AcceptanceError
from evoharness.work.models import Artifact
from evoharness.ids import sha256_text


def _allow(s, c, a):
    return True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # 回执策略默认 grace：仅 enforce 专项用例显式开启
    monkeypatch.delenv(acceptance.RECEIPT_ENFORCE_ENV, raising=False)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    out_root = tmp_path / "run-out"
    out_root.mkdir()
    with factory.begin() as s:
        task = Task(task_key="k1", tenant="t", owner="alice", chat_id="chat", title="修复",
                    action="fix", status=TaskStatus.OPEN, workflow_kind="delegated_delivery",
                    goal="修复缺陷", acceptance_json='[{"kind": "code", "criteria": "回归通过"}]',
                    revision=1, assignee_id="bob")
        s.add(task)
        s.flush()
        run = execution.request_run(s, task_id=task.id, context={"g": 1}, actor_id="runner-1",
                                    output_root=str(out_root), idempotency_key="i1",
                                    validate_context=_allow)
        s.flush()
        yield s, task, run, out_root
    engine.dispose()


def _run_to_review(s, task, run, actor="bob"):
    service.transition_to(s, task.id, to_state="queued", actor=actor)
    claimed = execution.claim(s, run.id, worker="w1", validate_context=_allow)
    service.transition_to(s, task.id, to_state="running", actor=actor)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_allow)
    execution.transition(s, run.id, worker="w1", generation=claimed.generation,
                         state="waiting_acceptance", validate_context=_allow)
    acceptance.request_acceptance(s, task.id, run_id=run.id, requester=actor)


def _register(s, run, out_root, name, content, **kwargs):
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / name
    path.write_text(content, encoding="utf-8")
    return acceptance.register_artifact(s, run, kind=name.rsplit(".", 1)[0],
                                        path=str(path), **kwargs)


def _second_run(s, task, run, out_root, key, worker, root_name):
    """同一任务的第二个 Run：推进到 running 并登记齐证据（用于失败/暂停 Run 负例）。"""
    run2 = execution.request_run(s, task_id=task.id, context={"g": key}, actor_id=f"runner-{key}",
                                 output_root=str(out_root.parent / root_name),
                                 idempotency_key=key, validate_context=_allow)
    claimed = execution.claim(s, run2.id, worker=worker, validate_context=_allow)
    service.transition_to(s, task.id, to_state="running", actor="bob")
    execution.transition(s, run2.id, worker=worker, generation=claimed.generation,
                         state="running", validate_context=_allow)
    root2 = out_root.parent / root_name
    _register(s, run2, root2, "diff.txt", f"patch-{key}")
    _register(s, run2, root2, "test_report.txt", f"pass-{key}")
    return run2, claimed


def _events(s, task_id):
    return [e.event_type for e in s.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task_id))]


def _blocked_detail(s, task_id):
    rows = [e for e in s.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task_id,
                                TaskEvent.event_type == "acceptance_blocked"))]
    assert rows, "拒绝通过必须留下 acceptance_blocked 事件"
    return rows[-1].detail


def test_probe_negative_tampered_and_deleted_artifacts_cannot_pass(env):
    """审核探针原样负例：登记后改写 diff + 删除 test_report，review pass 必须失效。"""
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "original verified content")
    _register(s, run, out_root, "test_report.txt", "original verified content")
    # 登记后篡改与删除（探针动作）
    (out_root / "diff.txt").write_text("changed after registration", encoding="utf-8")
    (out_root / "test_report.txt").unlink()
    # 修复后：review 不再静默通过，而是拒绝（blocked 事件）且 Task 不 done
    result = acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert result.status == "in_review"
    assert task.status != TaskStatus.DONE
    detail = _blocked_detail(s, task.id)
    assert "hash_mismatch:diff" in detail
    assert "artifact_missing:test_report" in detail
    assert "acceptance_passed" not in _events(s, task.id)
    # 修复现场后重新验收才可能 done（恢复路径不被堵死）
    (out_root / "diff.txt").write_text("original verified content", encoding="utf-8")
    (out_root / "test_report.txt").write_text("original verified content", encoding="utf-8")
    done = acceptance.review(s, task.id, run_id=run.id, reviewer="alice",
                             verdict="pass", notes="现场已复原")
    assert done.status == TaskStatus.DONE


def test_deleted_artifact_blocks_pass(env):
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    (out_root / "test_report.txt").unlink()
    result = acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert result.status == "in_review" and task.status != TaskStatus.DONE
    assert "artifact_missing:test_report" in _blocked_detail(s, task.id)


def test_tampered_artifact_blocks_pass(env):
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    (out_root / "diff.txt").write_text("malicious patch after registration", encoding="utf-8")
    result = acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert result.status == "in_review" and task.status != TaskStatus.DONE
    assert "hash_mismatch:diff" in _blocked_detail(s, task.id)


def test_stale_generation_artifacts_rejected_until_reregistered(env):
    """Run generation 前进后旧代产物拒绝；按当前代重登记后才能通过。"""
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    # 代际前进（recover_expired / reacquire 恢复续跑都会使 generation 递增，
    # 这里直接推进以与 R05 恢复编排细节解耦，聚焦验收侧的拒绝语义）
    old_generation = run.generation
    run.generation += 1
    s.flush()
    result = acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert result.status == "in_review" and task.status != TaskStatus.DONE
    detail = _blocked_detail(s, task.id)
    assert "generation_stale:diff" in detail and "generation_stale:test_report" in detail
    # 按当前代重登记（文件未变）：hash 复核后产物绑定当前 generation，验收可通过
    acceptance.register_artifact(s, run, kind="diff", path=str(out_root / "diff.txt"))
    acceptance.register_artifact(s, run, kind="test_report",
                                 path=str(out_root / "test_report.txt"))
    done = acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert done.status == TaskStatus.DONE
    assert run.generation == old_generation + 1


def test_failed_run_cannot_be_verified(env):
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    # 同任务的另一个 Run 已失败：即使它持有登记产物，也不允许作为验收依据
    run2, claimed = _second_run(s, task, run, out_root, key="i2", worker="w2", root_name="out2")
    execution.transition(s, run2.id, worker="w2", generation=claimed.generation,
                         state="failed", validate_context=_allow)
    assert run2.state == "failed"
    # 与既有返工测试相同的显式 in_review 通路（聚焦 Run 层拒绝语义）
    task.status = "in_review"
    s.flush()
    result = acceptance.review(s, task.id, run_id=run2.id, reviewer="alice", verdict="pass")
    assert result.status == "in_review" and task.status != TaskStatus.DONE
    assert "run_not_verifiable:failed" in _blocked_detail(s, task.id)


def test_paused_run_cannot_be_verified(env):
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    run2, claimed = _second_run(s, task, run, out_root, key="i3", worker="w3", root_name="out3")
    execution.transition(s, run2.id, worker="w3", generation=claimed.generation,
                         state="paused", validate_context=_allow)
    task.status = "in_review"
    s.flush()
    result = acceptance.review(s, task.id, run_id=run2.id, reviewer="alice", verdict="pass")
    assert result.status == "in_review" and task.status != TaskStatus.DONE
    assert "run_not_verifiable:paused" in _blocked_detail(s, task.id)


def test_cancelled_task_cannot_be_marked_done_by_review(env):
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    service.transition_to(s, task.id, to_state="cancelled", actor="alice", reason="作废")
    with pytest.raises(AcceptanceError, match="task_not_in_review"):
        acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert task.status == "cancelled" and task.status != TaskStatus.DONE


def test_receiptless_test_report_rejected_under_enforce(env, monkeypatch):
    """enforce 策略：无回执的纯文本 test_report 不作为通过依据。"""
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "enforce")
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")   # 无 checker_receipt
    result = acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert result.status == "in_review" and task.status != TaskStatus.DONE
    assert "receipt_missing:test_report" in _blocked_detail(s, task.id)


def test_valid_receipt_passes_under_enforce(env, monkeypatch):
    """合法带回执（checker id + 时间 + 结果摘要 hash）→ enforce 下仍可通过。"""
    monkeypatch.setenv(acceptance.RECEIPT_ENFORCE_ENV, "enforce")
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed",
              checker_receipt={"checker": "pytest-8.2",
                               "checked_at": "2026-11-14T00:00:00",
                               "summary_sha256": sha256_text("3 passed")})
    done = acceptance.review(s, task.id, run_id=run.id, reviewer="alice",
                             verdict="pass", notes="报告与回执绑定一致")
    assert done.status == TaskStatus.DONE


def test_unbound_receipt_rejected_even_in_grace_mode(env):
    """已登记的回执始终校验绑定：报告改写重登记后旧回执失绑即拒绝。"""
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed",
              checker_receipt={"checker": "pytest-8.2",
                               "summary_sha256": sha256_text("3 passed")})
    # 报告被改写并重登记（hash 复核更新为当前事实），旧回执不再绑定当前内容
    (out_root / "test_report.txt").write_text("forged: all 99 passed", encoding="utf-8")
    acceptance.register_artifact(s, run, kind="test_report",
                                 path=str(out_root / "test_report.txt"))
    result = acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert result.status == "in_review" and task.status != TaskStatus.DONE
    assert "receipt_unbound:test_report" in _blocked_detail(s, task.id)


def test_grace_mode_keeps_legacy_receiptless_flow_green(env):
    """兼容策略：grace（默认）下既有未带回执调用方不被静默拒绝（显式升级开关）。"""
    assert acceptance.receipts_enforced() is False
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    done = acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert done.status == TaskStatus.DONE


def test_same_id_reregister_rehashes_instead_of_reusing_stale_record(env):
    """同 ID 重登记必须 hash 复核：内容已变时更新为当前事实，不得复用旧记录。"""
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    first = _register(s, run, out_root, "diff.txt", "patch-v1")
    stale_hash = first.sha256
    (out_root / "diff.txt").write_text("patch-v2", encoding="utf-8")
    second = acceptance.register_artifact(s, run, kind="diff",
                                          path=str(out_root / "diff.txt"))
    assert second.id == first.id                      # 幂等键相同
    assert second.sha256 == sha256_text("patch-v2")   # 但 hash 已复核为当前内容
    assert second.sha256 != stale_hash
    count = s.scalar(select(func.count()).select_from(Artifact).where(
        Artifact.run_id == run.id, Artifact.kind == "diff"))
    assert count == 1                                 # 不产生影子重复行
    _register(s, run, out_root, "test_report.txt", "3 passed")
    done = acceptance.review(s, task.id, run_id=run.id, reviewer="alice", verdict="pass")
    assert done.status == TaskStatus.DONE


def test_idempotent_reregister_of_unchanged_content_reuses_record(env):
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    first = _register(s, run, out_root, "diff.txt", "patch-v1")
    digest_before = first.sha256
    created_before = first.created_at
    again = acceptance.register_artifact(s, run, kind="diff",
                                         path=str(out_root / "diff.txt"))
    assert again.id == first.id and again.sha256 == digest_before
    assert again.created_at == created_before         # 内容未变：真正幂等
    count = s.scalar(select(func.count()).select_from(Artifact).where(
        Artifact.run_id == run.id, Artifact.kind == "diff"))
    assert count == 1


def test_normal_untampered_flow_still_reaches_done(env):
    """正向不破坏：未篡改流程照常 done，且通过路径原子留痕。"""
    s, task, run, out_root = env
    _run_to_review(s, task, run)
    _register(s, run, out_root, "diff.txt", "patch")
    _register(s, run, out_root, "test_report.txt", "3 passed")
    done = acceptance.review(s, task.id, run_id=run.id, reviewer="alice",
                             verdict="pass", notes="回归与差异齐备")
    assert done.status == TaskStatus.DONE
    events = _events(s, task.id)
    assert "acceptance_passed" in events and "acceptance_blocked" not in events
