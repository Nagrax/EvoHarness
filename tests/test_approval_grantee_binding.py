"""R02 回归：批准 grantee 绑定与 Run 操作者强制匹配（审计 20261114）。

真实缺陷（修复前，独立审核探针 grantee_omission / api_grantee_mismatch 复现）：
- authorization.validate 只在 row.grantee_id 与请求 grantee_id **同时非空**才比较：
  批准绑定 grantee='alice' 后，省略 grantee 的 validate 仍然通过（省略被当成通配）；
- start_run 的授权上下文不带 grantee_id：API 以 alice 批准后，
  mallory 用 start_run(actor_id='mallory') 照样启动 Run。

修复语义：
- 批准绑定了 grantee：validate 省略 → decision_grantee_required；
  不匹配 → decision_grantee_mismatch；一致 → 通过；
- 完全未绑定 grantee 的批准保留兼容行为（省略/任意 grantee 均可）；
- validate 补 org_id 绑定（主体×组织×目标×版本×载荷×动作）；
- start_run 把实际操作者作为 grantee 传入校验并绑定 Run 记录
  （run.actor_id + context.authorization.grantee_id）；
- 幂等恢复路径（request_run 命中既有 Run）同样重验授权。

调用方式与审核探针一致：open_api + authorization 服务，全部离线。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from evoharness.api import open_api  # noqa: E402
from evoharness.governance import authorization  # noqa: E402
from evoharness.governance.authorization import AuthorizationError  # noqa: E402
from evoharness.models import Base  # noqa: E402
from evoharness.work.models import ExecutionRun  # noqa: E402


@pytest.fixture()
def api(tmp_path):
    work_api = open_api("sqlite:///" + (tmp_path / "grantee-binding.db").as_posix(),
                        artifacts_dir=tmp_path / "artifacts")
    yield work_api
    work_api._app.engine.dispose()


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


def _delegated_task(api, *, org_id="org"):
    task = api.create_work_item(tenant="tenant", owner="alice", title="delegated",
                                goal="fix", acceptance=[{"kind": "code"}],
                                assignee_id="alice",
                                workflow_kind="delegated_delivery", org_id=org_id)
    assert task["ok"], task
    return task["data"]["id"]


def _approve(api, task_id, *, org_id="org", actor="alice", grantee="alice"):
    payload = api.run_request_payload(task_id)
    assert payload["ok"], payload
    approved = api.submit_approval(
        org_id=org_id, actor_id=actor, target_type="work_item",
        target_id=str(task_id), target_revision=payload["data"]["target_revision"],
        payload=payload["data"]["payload"], actions=["work.execute"],
        grantee_id=grantee)
    assert approved["ok"], approved
    return approved["data"]["decision_id"]


# ── ① 探针 grantee_omission：绑定 grantee 的批准不容省略 ─────────────────────


def test_bound_grantee_rejects_omitted_grantee(session):
    """批准绑定 grantee='alice' 后，validate 不带 grantee 必须拒绝（省略≠通配）。"""
    decision = authorization.approve(
        session, "owner", org_id="org-A", target_type="work_item",
        target_id="1", target_revision=1, payload_hash="payload",
        actions={"work.execute"}, grantee_id="alice")
    with pytest.raises(AuthorizationError, match="decision_grantee_required"):
        authorization.validate(session, target_type="work_item", target_id="1",
                               target_revision=1, payload_hash="payload",
                               action="work.execute")
    assert decision.grantee_id == "alice"


def test_bound_grantee_rejects_mismatched_grantee(session):
    authorization.approve(session, "owner", org_id="org-A", target_type="work_item",
                          target_id="1", target_revision=1, payload_hash="payload",
                          actions={"work.execute"}, grantee_id="alice")
    with pytest.raises(AuthorizationError, match="decision_grantee_mismatch"):
        authorization.validate(session, target_type="work_item", target_id="1",
                               target_revision=1, payload_hash="payload",
                               action="work.execute", grantee_id="mallory")


def test_bound_grantee_accepts_matching_grantee(session):
    authorization.approve(session, "owner", org_id="org-A", target_type="work_item",
                          target_id="1", target_revision=1, payload_hash="payload",
                          actions={"work.execute"}, grantee_id="alice")
    row = authorization.validate(session, target_type="work_item", target_id="1",
                                 target_revision=1, payload_hash="payload",
                                 action="work.execute", grantee_id="alice")
    assert row.grantee_id == "alice"


def test_unbound_grantee_keeps_caller_compat(session):
    """完全未绑定 grantee 的批准：省略/任意 grantee 均保持既有兼容行为。"""
    decision = authorization.approve(session, "owner", org_id="org-A",
                                     target_type="work_item", target_id="2",
                                     target_revision=1, payload_hash="payload",
                                     actions={"work.execute"})
    assert decision.grantee_id == ""
    row = authorization.validate(session, target_type="work_item", target_id="2",
                                 target_revision=1, payload_hash="payload",
                                 action="work.execute")
    assert row.decision_id == decision.decision_id
    row2 = authorization.validate(session, target_type="work_item", target_id="2",
                                  target_revision=1, payload_hash="payload",
                                  action="work.execute", grantee_id="anyone")
    assert row2.decision_id == decision.decision_id


def test_validate_binds_org_id(session):
    """org_id 绑定：跨组织的同目标批准不互相可用。"""
    for org in ("org-A", "org-B"):
        authorization.approve(session, "owner", org_id=org, target_type="work_item",
                              target_id="3", target_revision=1, payload_hash="payload",
                              actions={"work.execute"}, grantee_id="alice")
    row = authorization.validate(session, target_type="work_item", target_id="3",
                                 target_revision=1, payload_hash="payload",
                                 action="work.execute", grantee_id="alice",
                                 org_id="org-B")
    assert row.org_id == "org-B"
    with pytest.raises(AuthorizationError, match="no_active_decision"):
        authorization.validate(session, target_type="work_item", target_id="3",
                               target_revision=1, payload_hash="payload",
                               action="work.execute", grantee_id="alice",
                               org_id="org-C")


def test_resolve_send_identity_passes_grantee_through(session):
    from evoharness.ids import sha256_json

    authorization.approve(session, "owner", org_id="org", target_type="draft",
                          target_id="d1", target_revision=1,
                          payload_hash=sha256_json({"x": 1}),
                          actions={"send_as_user"}, grantee_id="emp1")
    with pytest.raises(AuthorizationError, match="decision_grantee_required"):
        authorization.resolve_send_identity(session, target_type="draft",
                                            target_id="d1", target_revision=1,
                                            payload={"x": 1}, requested_identity="user",
                                            user_identity_valid=True)
    decision, identity = authorization.resolve_send_identity(
        session, target_type="draft", target_id="d1", target_revision=1,
        payload={"x": 1}, requested_identity="user", user_identity_valid=True,
        grantee_id="emp1")
    assert identity == "user"


# ── ② 探针 api_grantee_mismatch：Run 操作者必须匹配批准的 grantee ────────────


def test_start_run_rejects_operator_not_matching_bound_grantee(api):
    """alice 批准 work.execute(grantee=alice) 后 mallory start_run → 拒绝。"""
    task_id = _delegated_task(api)
    _approve(api, task_id, grantee="alice")
    started = api.start_run(task_id, actor_id="mallory",
                            output_root=str(api.artifacts_dir.parent / "mallory-out"))
    assert started["ok"] is False
    assert started["reason_code"] == "decision_grantee_mismatch"
    assert started["retryable"] is False


def test_start_run_accepts_bound_operator_and_binds_run_record(api):
    task_id = _delegated_task(api)
    decision_id = _approve(api, task_id, grantee="alice")
    started = api.start_run(task_id, actor_id="alice",
                            output_root=str(api.artifacts_dir.parent / "alice-out"))
    assert started["ok"], started
    assert started["data"]["operator_id"] == "alice"
    assert started["data"]["decision_id"] == decision_id
    with api._session() as session:
        run = session.get(ExecutionRun, started["data"]["operation_id"])
        assert run.actor_id == "alice"
        context = json.loads(run.context_json)
        # Run 记录绑定操作者：主体×组织×目标×版本×载荷×动作 落在 context.authorization
        assert context["authorization"]["grantee_id"] == "alice"
        assert context["authorization"]["org_id"] == "org"
        assert context["authorization"]["target_id"] == str(task_id)


def test_start_run_unbound_approval_keeps_any_operator(api):
    """未绑定 grantee 的批准：操作者不匹配也保持既有兼容行为。"""
    task_id = _delegated_task(api)
    _approve(api, task_id, grantee="")
    started = api.start_run(task_id, actor_id="mallory",
                            output_root=str(api.artifacts_dir.parent / "any-out"))
    assert started["ok"], started
    assert started["data"]["operator_id"] == "mallory"


def test_resume_path_revalidates_operator_binding(api):
    """幂等恢复（暂停后重启）同样受 grantee 绑定约束：换操作者 → 拒绝。"""
    task_id = _delegated_task(api)
    _approve(api, task_id, grantee="alice")
    started = api.start_run(task_id, actor_id="alice",
                            output_root=str(api.artifacts_dir.parent / "resume-out"))
    run_id = started["data"]["operation_id"]
    paused = api.pause_run(run_id)
    assert paused["ok"], paused
    other = api.start_run(task_id, actor_id="mallory",
                          output_root=str(api.artifacts_dir.parent / "resume-out"))
    assert other["ok"] is False
    assert other["reason_code"] == "run_intent_conflict"  # 上下文含 grantee，意图不一致
    resumed = api.start_run(task_id, actor_id="alice",
                            output_root=str(api.artifacts_dir.parent / "resume-out"))
    assert resumed["ok"], resumed
    assert resumed["data"]["operation_id"] == run_id
    assert resumed["data"]["state"] == "queued"


# ── ③ 旧批准重放：payload / revision 变化即失效 ─────────────────────────────


def test_old_approval_replay_rejected_on_payload_change(api):
    task_id = _delegated_task(api)
    _approve(api, task_id, grantee="alice")
    payload = api.run_request_payload(task_id)
    replay = api.start_run(task_id, actor_id="alice",
                           brief={**payload["data"]["payload"], "goal": "被篡改的目标"},
                           output_root=str(api.artifacts_dir.parent / "replay-out"))
    assert replay["ok"] is False
    assert replay["reason_code"] == "decision_payload_changed"


def test_old_approval_replay_rejected_on_revision_change(api):
    from evoharness.work import service as work_service

    task_id = _delegated_task(api)
    _approve(api, task_id, grantee="alice")
    with api._session() as session:
        work_service.revise_contract(session, task_id, actor="alice",
                                     goal="修订后的目标", expected_revision=1,
                                     reason="范围变更")
    replay = api.start_run(task_id, actor_id="alice",
                           output_root=str(api.artifacts_dir.parent / "replay-rev"))
    assert replay["ok"] is False
    assert replay["reason_code"] == "decision_target_revision_changed"


def test_no_approval_start_run_still_waits_authorization(api):
    task_id = _delegated_task(api)
    started = api.start_run(task_id, actor_id="alice",
                            output_root=str(api.artifacts_dir.parent / "noauth"))
    assert started["ok"] is False
    assert started["reason_code"] == "no_active_decision"
    assert started["retryable"] is True
    assert started["next_action"] == "submit_approval"
