"""DE-40 回归：跨单元交接包、接收方核验与责任切换。

覆盖 tasks.json DE-40 的验收场景 2 与 4（全部离线：内存 SQLite、fake 授权端口、
tmp_path 产物，无网络、无外部服务）：

场景 2a：跨单元权限不足返回具体缺口（space_not_authorized / capability_not_granted），
         被裁剪内容不泄露（无路径/摘要），缺口未清前不能接受；拒绝必须带原因；
场景 2b：重复交接幂等——同一张未决交接单、不多建工作、通知意图只发一次；
场景 4a：更换执行人保持原 Task/验收与交接证据（任务主键/目标/验收/revision
         不变，position_handoff 事件留痕，旧岗位在途 Run 取消传播 + 回执对账）；
         无固定岗位流水线、无重复目标库。

另覆盖：能力不匹配的交接在提案入口被拒并给具体原因（DE-38 接线）、
责任岗位 vacant 时调度走同一"等待负责人"路径、不增殖 Agent。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from evoharness.governance import budget as budget_svc
from evoharness.models import Base, Outbox, Task, TaskEvent, TaskStatus
from evoharness.organization import service as org
from evoharness.organization.models import Assignment, PositionHandoff, TaskCrewMember
from evoharness.organization.service import AuthenticatedPrincipal, OrganizationError
from evoharness.work import execution, handoffs, scheduling, service, supplements
from evoharness.work.acceptance import register_artifact
from evoharness.work.models import ExecutionRun
from evoharness.work.objectives import OrgObjective

OWNER = AuthenticatedPrincipal(principal_id="founder", tenant_id="tn", app_id="app",
                               authenticated=True,
                               authorities=frozenset({"organization.bootstrap",
                                                      "organization.manage",
                                                      "work.execute", "knowledge.read"}))


def _principal(pid: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(principal_id=pid, tenant_id="tn", app_id="app",
                                  authenticated=True, authorities=frozenset())


def _org_validator(principal: AuthenticatedPrincipal):
    def validate(session, context, action):
        try:
            org.validate_context(session, org.ResolvedContext(context, principal), action)
            return True
        except org.OrganizationError:
            return False

    return validate


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def org_setup(session):
    """单组织两单元 + 跨单元观察者 carol（只有 reviewer 任职、verify 空间）。"""
    org.bootstrap_organization(session, OWNER, org_id="org", title="EvoHarness",
                               membership_id="m0", budget_limit=100)
    org.create_unit(session, OWNER, org_id="org", unit_id="dev", title="研发")
    org.create_unit(session, OWNER, org_id="org", unit_id="qa", title="质量")
    org.create_space(session, OWNER, org_id="org", space_id="support", title="支持")
    org.create_space(session, OWNER, org_id="org", space_id="verify", title="验收")
    org.bind_space(session, OWNER, org_id="org", unit_id="dev", space_id="support")
    org.bind_space(session, OWNER, org_id="org", unit_id="qa", space_id="verify")
    org.bind_space(session, OWNER, org_id="org", unit_id="qa", space_id="support")
    org.create_position(session, OWNER, org_id="org", unit_id="dev", position_id="swe",
                        title="工程师", mission="修复缺陷", actions={"work.execute"},
                        budget_limit=10, capability_refs=["code.fix"])
    org.create_position(session, OWNER, org_id="org", unit_id="qa", position_id="reviewer",
                        title="审阅岗", mission="审阅补丁", actions={"knowledge.read"},
                        capability_refs=["code.review"], budget_limit=5)
    org.create_position(session, OWNER, org_id="org", unit_id="qa", position_id="qa_eng",
                        title="测试工程师", mission="验证交付", actions={"work.execute"},
                        capability_refs=["test.verify"], budget_limit=8)
    org.add_member(session, OWNER, org_id="org", membership_id="m1", principal_id="alice",
                   tenant_id="tn", app_id="app", grants={"work.execute", "knowledge.read"},
                   principal_kind="ai_service")
    org.add_member(session, OWNER, org_id="org", membership_id="m2", principal_id="bob",
                   tenant_id="tn", app_id="app", grants={"work.execute", "knowledge.read"},
                   principal_kind="human")
    org.add_member(session, OWNER, org_id="org", membership_id="m4", principal_id="carol",
                   tenant_id="tn", app_id="app", grants={"knowledge.read"},
                   principal_kind="human")
    org.create_employee(session, OWNER, org_id="org", employee_id="emp1",
                        membership_id="m1", display_name="数字员工A")
    org.create_employee(session, OWNER, org_id="org", employee_id="emp2",
                        membership_id="m2", display_name="员工B")
    org.create_employee(session, OWNER, org_id="org", employee_id="emp4",
                        membership_id="m4", display_name="观察者C")
    org.assign(session, OWNER, org_id="org", assignment_id="a1", position_id="swe",
               employee_id="emp1", space_ids={"support"}, actions={"work.execute"},
               budget_limit=5)
    org.assign(session, OWNER, org_id="org", assignment_id="a3", position_id="qa_eng",
               employee_id="emp2", space_ids={"verify"}, actions={"work.execute"},
               budget_limit=8)
    org.assign(session, OWNER, org_id="org", assignment_id="a5", position_id="reviewer",
               employee_id="emp4", space_ids={"verify"}, actions={"knowledge.read"})
    budget_svc.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=100000)
    budget_svc.set_limit(session, scope_kind="space", scope_id="support", limit_tokens=50000)
    budget_svc.set_limit(session, scope_kind="space", scope_id="verify", limit_tokens=50000)
    return {"alice": _principal("alice"), "bob": _principal("bob"),
            "carol": _principal("carol")}


def _delegated_task(session, *, task_key: str, space_id: str = "support",
                    position: str = "swe", goal="修复登录缺陷") -> Task:
    task = Task(task_key=task_key, tenant="tn", owner="founder", chat_id="chat",
                title=goal[:20], action=goal, status=TaskStatus.OPEN,
                workflow_kind="delegated_delivery", goal=goal,
                acceptance_json='["回归通过"]', org_id="org", space_id=space_id,
                assigned_position=position)
    session.add(task)
    session.flush()
    session.add(TaskEvent(task_id=task.id, event_type="created", actor="system",
                          detail="组织任务建立"))
    session.flush()
    service.transition_to(session, task.id, to_state="queued", actor="founder")
    return task


def _run_with_artifact(session, task: Task, principals, tmp_path: Path) -> ExecutionRun:
    """原岗位的在途 Run：已认领运行、登记产物、留下检查点未完成项。"""
    context = org.resolve_context(session, principals["alice"], org_id="org",
                                  space_id=task.space_id, assignment_id="a1").values
    run = execution.request_run(session, task_id=task.id, context=context,
                                actor_id="alice", output_root=str(tmp_path / "out"),
                                idempotency_key=f"ho|{task.task_key}",
                                validate_context=_org_validator(principals["alice"]))
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    report = out_root / "report.txt"
    report.write_text("regression report", encoding="utf-8")
    claimed = execution.claim(session, run.id, worker="w1",
                              validate_context=_org_validator(principals["alice"]))
    service.transition_to(session, task.id, to_state="running", actor="w1")
    execution.transition(session, run.id, worker="w1", generation=claimed.generation,
                         state="running", validate_context=_org_validator(principals["alice"]),
                         checkpoint={"done": ["复现缺陷"], "todo": ["补全回归用例"]})
    register_artifact(session, run, kind="test_report", path=str(report))
    session.flush()
    return run


# ── 场景 2b：重复交接幂等，不多建工作、不多发消息 ─────────────────────────────


def test_prepare_handoff_is_idempotent_no_duplicate_work_or_notice(session, org_setup,
                                                                   tmp_path):
    task = _delegated_task(session, task_key="k-idem")
    _run_with_artifact(session, task, org_setup, tmp_path)
    tasks_before = session.scalar(select(func.count()).select_from(Task))
    handoffs_before = session.scalar(select(func.count()).select_from(PositionHandoff))

    first = handoffs.prepare_handoff(session, org_setup["alice"], org_id="org",
                                     task_id=task.id, from_assignment_id="a1",
                                     to_position_id="qa_eng",
                                     requested_capability="test.verify")
    assert first["duplicate"] is False
    assert handoffs.verify_package_hash(first["package"]) is True
    package = first["package"]
    assert package["acceptance_revision"] == task.revision
    assert [a["kind"] for a in package["artifacts"]] == ["test_report"]
    assert package["unfinished"] == [{"source": "checkpoint", "item": "补全回归用例"}]
    assert package["source_space_ids"] == ["support"]

    # 重复交接：同一张未决交接单，不建新工作、不再发通知
    second = handoffs.prepare_handoff(session, org_setup["alice"], org_id="org",
                                      task_id=task.id, from_assignment_id="a1",
                                      to_position_id="qa_eng",
                                      requested_capability="test.verify")
    assert second["duplicate"] is True
    assert second["handoff"].handoff_id == first["handoff"].handoff_id
    assert session.scalar(select(func.count()).select_from(PositionHandoff)) == handoffs_before + 1
    assert session.scalar(select(func.count()).select_from(Task)) == tasks_before
    notices = list(session.scalars(select(Outbox).where(
        Outbox.intent_key.like("handoff|%"))))
    assert len(notices) == 1  # 通知意图只发一次

    # 能力不匹配的交接在提案入口被拒并给具体原因；不留提案与通知
    with pytest.raises(OrganizationError, match="handoff_capability_gap"):
        handoffs.prepare_handoff(session, org_setup["alice"], org_id="org",
                                 task_id=task.id, from_assignment_id="a1",
                                 to_position_id="reviewer",
                                 requested_capability="test.verify")
    assert session.scalar(select(func.count()).select_from(PositionHandoff)) == handoffs_before + 1
    assert len(list(session.scalars(select(Outbox).where(
        Outbox.intent_key.like("handoff|%"))))) == 1


# ── 场景 2a：权限不足返回具体缺口，不泄露被裁剪内容 ───────────────────────────


def test_review_redacts_unauthorized_content_and_reports_specific_gaps(session, org_setup,
                                                                       tmp_path):
    task = _delegated_task(session, task_key="k-gap")
    _run_with_artifact(session, task, org_setup, tmp_path)
    prepared = handoffs.prepare_handoff(session, org_setup["alice"], org_id="org",
                                        task_id=task.id, from_assignment_id="a1",
                                        to_position_id="qa_eng",
                                        requested_capability="test.verify")
    handoff_id = prepared["handoff"].handoff_id

    # carol 只有 reviewer 任职、只覆盖 verify 空间：缺口具体、内容裁剪
    review = handoffs.review_package(session, org_setup["carol"], org_id="org",
                                     handoff_id=handoff_id)
    codes = {g["code"] for g in review["gaps"]}
    assert codes == {"space_not_authorized", "capability_not_granted"}
    assert {"code": "space_not_authorized", "space_id": "support"} in review["gaps"]
    assert {"code": "capability_not_granted", "capability": "test.verify"} in review["gaps"]
    assert review["redacted"] is True
    assert review["artifacts"] == [{"kind": "test_report", "redacted": True}]
    assert all("path" not in a and "sha256" not in a for a in review["artifacts"])
    assert review["unfinished"] == [{"redacted": True, "count": 1}]
    # 验收合同始终可见：那是接收方必须满足的条件，不是被裁剪的来源内容
    assert review["acceptance"] == ["回归通过"]

    # 缺口未清前不能接受
    with pytest.raises(handoffs.HandoffError, match="accept_blocked_by_gaps"):
        handoffs.decide_from_review(session, org_setup["carol"], org_id="org",
                                    handoff_id=handoff_id, decision="accept")

    # 有因拒绝：拒绝必须给出具体缺口原因
    row = handoffs.decide_from_review(session, org_setup["carol"], org_id="org",
                                      handoff_id=handoff_id, decision="reject",
                                      reason="space_not_authorized: 无 support 空间授权")
    assert row.status == "rejected"
    assert "space_not_authorized" in row.decision_reason
    # 任务责任岗位未被改动
    assert session.get(Task, task.id).assigned_position == "swe"


# ── 场景 4a：更换执行人保持原 Task/验收与交接证据 ─────────────────────────────


def test_accept_switches_position_and_preserves_task_acceptance_and_evidence(
        session, org_setup, tmp_path):
    task = _delegated_task(session, task_key="k-accept")
    run = _run_with_artifact(session, task, org_setup, tmp_path)
    before = (task.id, task.goal, task.acceptance_json, task.revision,
              task.workflow_kind, task.org_id, task.space_id)
    tasks_before = session.scalar(select(func.count()).select_from(Task))
    objectives_before = session.scalar(select(func.count()).select_from(OrgObjective))
    crew_before = session.scalar(select(func.count()).select_from(TaskCrewMember))

    prepared = handoffs.prepare_handoff(session, org_setup["alice"], org_id="org",
                                        task_id=task.id, from_assignment_id="a1",
                                        to_position_id="qa_eng",
                                        requested_capability="test.verify")
    handoff_id = prepared["handoff"].handoff_id

    # bob 先只覆盖 verify（a3）：缺口可见，不能接受
    with pytest.raises(handoffs.HandoffError, match="accept_blocked_by_gaps"):
        handoffs.decide_from_review(session, org_setup["bob"], org_id="org",
                                    handoff_id=handoff_id, decision="accept")

    # 授予 bob 在 qa_eng 岗位对 support 空间的任职后：交接包完整可见
    org.assign(session, OWNER, org_id="org", assignment_id="a7", position_id="qa_eng",
               employee_id="emp2", space_ids={"verify", "support"},
               actions={"work.execute"}, budget_limit=8)
    review = handoffs.review_package(session, org_setup["bob"], org_id="org",
                                     handoff_id=handoff_id)
    assert review["gaps"] == [] and review["redacted"] is False
    assert [a["path"] for a in review["artifacts"]]  # 产物路径可见
    assert review["unfinished"] == [{"source": "checkpoint", "item": "补全回归用例"}]

    row = handoffs.decide_from_review(session, org_setup["bob"], org_id="org",
                                      handoff_id=handoff_id, decision="accept",
                                      reason="capability verified")
    assert row.status == "accepted"

    # 原 Task 身份、目标、验收、版本、历史全部保持；只切责任岗位
    after = session.get(Task, task.id)
    assert (after.id, after.goal, after.acceptance_json, after.revision,
            after.workflow_kind, after.org_id, after.space_id) == before
    assert after.assigned_position == "qa_eng"
    # 不多建工作、不建第二目标库、不强制固定流水线（组队行数不变）
    assert session.scalar(select(func.count()).select_from(Task)) == tasks_before
    assert objectives_before == 0
    assert session.scalar(select(func.count()).select_from(OrgObjective)) == objectives_before
    assert crew_before == 0
    assert session.scalar(select(func.count()).select_from(TaskCrewMember)) == crew_before

    # 交接证据：任务上的 position_handoff 事件带产物/未完成项/能力/来源范围
    history = handoffs.handoff_history(session, org_id="org", task_id=task.id)
    assert [h["status"] for h in history["handoffs"]] == ["accepted"]
    assert len(history["events"]) == 1
    evidence = history["events"][0]["evidence"]
    assert evidence["capability"] == "test.verify"
    assert [a["kind"] for a in evidence["artifacts"]] == ["test_report"]
    assert evidence["unfinished"] == [{"source": "checkpoint", "item": "补全回归用例"}]
    assert evidence["source_space_ids"] == ["support"]

    # 旧岗位在途 Run：取消传播 + 在途回执对账单（未知回执不自动写成功）
    assert supplements.has_pending_cancellation(session, task.id)
    cancellations = [r for r in supplements.pending(session, task.id)
                     if r.event_kind == "cancellation"]
    assert any(json.loads(r.payload_json)["run_id"] == run.id for r in cancellations)
    receipts = org.open_receipts(session, org_id="org")
    assert any(r.target_id == run.id and r.status == "open" for r in receipts)

    # 责任索引随交接生效：调度按新岗位合格任职派工（qa_eng 覆盖 support）
    qualified = scheduling.qualified_assignments(session, task)
    assert [q["assignment_id"] for q in qualified] == ["a7"]


# ── 责任岗位 vacant 时同样等待负责人（无合格岗位路径，不增殖 Agent）────────────


def test_vacant_assigned_position_waits_instead_of_spawning(session, org_setup):
    task = _delegated_task(session, task_key="k-vacant")
    assignment = session.get(Assignment, "a1")
    org.transition(session, OWNER, org_id="org", kind="assignment", target_id="a1",
                   status="ended", expected_revision=assignment.revision)
    assert org.position_state(session, org_id="org", position_id="swe") == "vacant"

    report = scheduling.schedule_pending(session, org_id="org",
                                         context_factory=_context_factory_noop())
    wait = next((e for e in report["waiting"] if e["task_key"] == task.task_key), None)
    assert wait is not None and wait["reason"] == "no_qualified_position"
    refreshed = session.get(Task, task.id)
    assert refreshed.status == TaskStatus.NEEDS_CONFIRMATION
    assert "no_qualified_position" in (refreshed.waiting_reason or "")
    # 无 Run、无新任务、无新 Agent：等待负责人指派
    assert session.scalar(select(func.count()).select_from(ExecutionRun).where(
        ExecutionRun.task_id == task.id)) == 0
    assert session.scalar(select(func.count()).select_from(Task)) == 1


def _context_factory_noop():
    """岗位 vacant 时调度不会走到授权装配；此 factory 仅在误派工时才会被调用。"""

    def factory(session, task, executor):  # pragma: no cover — 误派工时显式失败
        raise AssertionError("vacant position must not reach authorization assembly")

    return factory
