"""EVO-02（DE-12）工具层重验契约回归：批准载荷/撤权在工具调用时刻的确定性重验。

真实接点（既有接线，不新造）：
- Worker 工具执行门 execute() 起点：``RestrictedAgentAdapter._validate_authorization``
  从 context["authorization"] 取主体(grantee)×组织×目标×版本×载荷，调
  ``governance.authorization.validate`` 以 Decision 表为唯一裁决（R02）；
- 工具调用时刻：``ToolExecutor.run`` 每次调用前执行 ``_pre_check``（独立短事务：
  Run 活性 → Task 状态 → Decision 重验 → 知识快照），拒绝即留痕
  （``ToolExecutor.refusals``/``events`` → ``adapter.last_refusals``）；
- Run 终态留痕：模型调用被拒 → 异常传播 → ``_finish_failed`` →
  ``execution.abort`` fenced abort，Run 行 error 携带真实触发码。

四条路径（STATE.json EVO-02 验收口径）：
① 批准有效且主体匹配 → 工具放行；
② 批准对应任务版本已变更（revision 前进）→ 拒绝且留痕；
③ 批准后授权被撤销 → 拒绝且留痕；
④ 批准载荷被篡改（决策不存在/版本不匹配/主体不符/载荷不符）→ 拒绝且留痕。

断言以服务端状态为准：Decision 行（revoked_at/superseded_by/target_revision）、
ExecutionRun 终态与 error、Task.revision、adapter.last_refusals；
不以 HTTP 200 / toast 为准。全部离线确定性（文件 SQLite、无网络、无真实模型）。

范围注记：组织任职 offboard 在 Worker 链不级联撤销 Decision（DE-12
IMPLEMENTATION-20261113.md 未完成清单 #3 的应用层组装缝），本轮"撤权"
口径为 Decision 撤销（authorization.revoke / api.revoke_approval），不虚构闭合。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
from evoharness.governance.budget_gateway import BudgetGateway


GRANTEE = "ai-support"
TURN_TOKENS = 60


def _turn_write() -> dict:
    return {"tool_calls": [{"name": "write_file", "arguments": {
        "path": "src/contract.py", "content": "VERSION = 1\n"}}],
        "content": "写入契约文件", "usage_tokens": TURN_TOKENS}


def _turn_read() -> dict:
    return {"tool_calls": [{"name": "read_file", "arguments": {
        "path": "src/contract.py"}}],
        "content": "复读契约文件", "usage_tokens": TURN_TOKENS}


def _turn_final() -> dict:
    return {"content": "完成", "final": True, "usage_tokens": 20}


def _build_chain(tmp_path: Path, hook=None) -> dict:
    """真实链路装配：init 组织 → 建委派任务 → 批准（绑定载荷+主体）→ 起 Run。

    hook(probe) 在第 2 幕模型调用返回后、工具执行前触发（N02 并发短事务通道），
    probe 携带 api/app/task_id/revision0，供各场景在执行中注入治理变更。
    """
    from evoharness.__main__ import main as root_main
    from evoharness.api import WorkApi
    from evoharness.application import Application
    from evoharness.governance import budget

    tmp = tmp_path
    app = Application.open("sqlite:///" + (tmp / "reverify.db").as_posix())
    api = WorkApi(app, artifacts_dir=tmp / "artifacts")
    out = io.StringIO()
    assert root_main(["--db", "sqlite:///" + (tmp / "reverify.db").as_posix(),
                      "--json", "init", "--org-id", "org-demo"],
                     stdout=out) == 0
    with app.transaction() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org-demo",
                         limit_tokens=100000)
        budget.set_limit(session, scope_kind="space", scope_id="space-support",
                         limit_tokens=50000)
    created = api.create_work_item(
        tenant="local", owner="founder", title="工具层重验契约", action="fix", goal="g",
        acceptance=[{"kind": "code", "criteria": "c"}],
        workflow_kind="delegated_delivery", org_id="org-demo",
        space_id="space-support")
    task_id = created["data"]["id"]
    assert api.transition_work_item(task_id, to_state="queued", actor="founder")["ok"]
    payload = api.run_request_payload(task_id)["data"]
    revision0 = int(payload["target_revision"])
    approved = api.submit_approval(
        org_id="org-demo", actor_id="founder", target_type="work_item",
        target_id=str(task_id), target_revision=revision0,
        payload=payload["payload"], actions=["work.execute"], grantee_id=GRANTEE)
    assert approved["ok"], approved
    decision_id = approved["data"]["decision_id"]
    started = api.start_run(task_id, actor_id=GRANTEE, output_root=str(tmp / "out"))
    assert started["ok"], started
    run_id = started["data"]["operation_id"]
    # 批准证据链：start_run 回执携带命中的 decision_id（服务端留痕，非 toast）
    assert started["data"]["decision_id"] == decision_id
    probe = {"api": api, "app": app, "task_id": task_id, "run_id": run_id,
             "revision0": revision0, "decision_id": decision_id,
             "payload": payload}
    state = {"calls": 0}

    def script(context):
        state["calls"] += 1
        if state["calls"] >= 2 and hook is not None and not state.get("hooked"):
            state["hooked"] = True
            hook(probe)
        return [_turn_write(), _turn_read(), _turn_final()]

    return {"app": app, "api": api, "probe": probe, "script": script,
            "tmp": tmp, "decision_id": decision_id, "run_id": run_id}


def _worker_for(chain: dict) -> "tuple[RestrictedAgentAdapter, object]":
    from evoharness.work.worker import WorkWorker, WorkerConfig
    from evoharness.workflows.code_fix import ScriptTestRunner

    adapter = RestrictedAgentAdapter(
        provider=FakeProvider(chain["script"]),
        gateway=BudgetGateway(chain["app"].session_factory),
        runner=ScriptTestRunner([sys.executable, "-c", "print('checker')"]),
        # 本回归只固化治理重验契约：检查器不依赖容器隔离（隔离矩阵归
        # test_tool_isolation），离线确定性优先。
        isolate_checks=False)
    worker = WorkWorker(chain["app"].session_factory, adapter,
                        chain["api"]._authorize_adapter(),
                        config=WorkerConfig(worker_id="w-reverify"))
    return adapter, worker


def _decision_rows(chain: dict) -> "list[object]":
    from evoharness.governance.models import Decision
    with chain["app"].transaction() as session:
        rows = list(session.query(Decision).filter(
            Decision.target_type == "work_item",
            Decision.target_id == str(chain["probe"]["task_id"])).all())
        for row in rows:
            session.expunge(row)
    return rows


# ── ① 批准有效且主体匹配 → 工具放行 ─────────────────────────────────────────


def test_valid_decision_and_matching_grantee_let_tools_run(tmp_path):
    """①：批准绑定载荷+主体（grantee=操作者），工具调用全部放行并留痕 ok。"""
    chain = _build_chain(tmp_path)
    adapter, worker = _worker_for(chain)
    try:
        result = worker.process_run(chain["run_id"])
        assert result.get("ok") is True, result
        assert result.get("run_state") == "waiting_acceptance", result
        # 放行不是静默：工具调用以 ok 事件留痕
        assert adapter.last_refusals == [], adapter.last_refusals
        tool_events = [entry for entry in (adapter.last_history or [])
                       if entry.get("role") == "tool"]
        assert any(entry.get("name") == "write_file" and entry.get("ok") is True
                   for entry in tool_events), tool_events
        rows = _decision_rows(chain)
        assert len(rows) == 1
        assert rows[0].revoked_at is None and rows[0].superseded_by == ""
        assert rows[0].grantee_id == GRANTEE
    finally:
        chain["app"].engine.dispose()


# ── ② 批准对应任务版本已变更（revision 前进）→ 拒绝且留痕 ────────────────────


def test_task_revision_advance_midrun_rejects_next_tool_call_with_trace(tmp_path):
    """②：执行中 revise_contract（revision 前进，批准仍绑旧版本）→
    下一工具调用被拒并留痕，Run 不允许以旧版本批准"假完成"。"""
    from evoharness.models import Task
    from evoharness.work import service as work_service
    from evoharness.work.models import ExecutionRun

    def revise_midrun(probe):
        with probe["app"].transaction() as session:
            revised = work_service.revise_contract(
                session, probe["task_id"], actor="founder",
                goal="范围变更后的目标", expected_revision=probe["revision0"],
                reason="范围变更")
            assert revised.revision == probe["revision0"] + 1

    chain = _build_chain(tmp_path, hook=revise_midrun)
    adapter, worker = _worker_for(chain)
    try:
        result = worker.process_run(chain["run_id"])
        # 前提：批准绑定的仍是旧版本，任务已前进（"批准对应任务版本已变更"）
        with chain["app"].transaction() as session:
            task = session.get(Task, chain["probe"]["task_id"])
            run = session.get(ExecutionRun, chain["run_id"])
            assert task.revision == chain["probe"]["revision0"] + 1
            assert run.task_revision == chain["probe"]["revision0"]
        rows = _decision_rows(chain)
        assert rows[0].target_revision == chain["probe"]["revision0"]
        assert rows[0].revoked_at is None  # 版本前进≠撤权：走版本重验路径
        # 工具调用时刻拒绝并留痕（refusals + 工具事件）
        assert "read_file:work_revision_changed" in adapter.last_refusals, \
            adapter.last_refusals
        # Run 不得以旧版本批准跑到完成态
        assert result.get("ok") is False, result
        assert result.get("run_state") == "failed", result
        with chain["app"].transaction() as session:
            run = session.get(ExecutionRun, chain["run_id"])
            assert run.state == "failed"
            assert "work_revision_changed" in (run.error or ""), run.error
    finally:
        chain["app"].engine.dispose()


# ── ③ 批准后授权被撤销 → 拒绝且留痕 ─────────────────────────────────────────


def test_decision_revocation_midrun_rejects_next_tool_call_with_trace(tmp_path):
    """③：执行中撤回批准（api.revoke_approval → Decision.revoked_at 落库）→
    下一工具调用被拒（no_active_decision）并留痕，Run 受控终止。"""
    from evoharness.work.models import ExecutionRun

    def revoke_midrun(probe):
        revoked = probe["api"].revoke_approval(
            org_id="org-demo", actor_id="founder", target_type="work_item",
            target_id=str(probe["task_id"]))
        assert revoked.get("ok") is True, revoked
        assert revoked["data"]["revoked_decisions"] >= 1

    chain = _build_chain(tmp_path, hook=revoke_midrun)
    adapter, worker = _worker_for(chain)
    try:
        result = worker.process_run(chain["run_id"])
        rows = _decision_rows(chain)
        assert any(row.revoked_at is not None for row in rows), \
            "撤权未落库（revoked_at）"
        assert "read_file:no_active_decision" in adapter.last_refusals, \
            adapter.last_refusals
        assert result.get("ok") is False, result
        assert result.get("run_state") == "failed", result
        with chain["app"].transaction() as session:
            run = session.get(ExecutionRun, chain["run_id"])
            assert run.state == "failed"
            assert "no_active_decision" in (run.error or ""), run.error
    finally:
        chain["app"].engine.dispose()


# ── ④ 批准载荷被篡改 → Worker 工具执行门拒绝 ────────────────────────────────


def _gate_contexts(chain: dict) -> "list[tuple[str, dict, str]]":
    """伪造的批准载荷声明（决策不存在/版本不匹配/主体不符/载荷不符）。

    上游校验被假设绕过（错配的 validate_context / 直接构造）时，Worker 门
    是最后一道防线：每次执行都必须以 Decision 表重验，伪造声明不得放行。
    """
    payload = chain["probe"]["payload"]
    revision0 = chain["probe"]["revision0"]
    task_id = chain["probe"]["task_id"]
    base_auth = {"target_type": "work_item", "target_id": str(task_id),
                 "target_revision": revision0,
                 "payload_hash": payload["payload_hash"],
                 "grantee_id": GRANTEE, "org_id": "org-demo"}
    base = {"task_id": str(task_id), "revision": revision0, "mode": "live",
            "brief": payload["payload"]}
    cases = [
        # 决策 id 不存在：声明的目标没有任何 Decision
        ("nonexistent_decision", "no_active_decision",
         {**base, "authorization": {**base_auth, "target_id": "999999"}}),
        # decision 版本不匹配：声明版本超出批准绑定的版本
        ("revision_mismatch", "decision_target_revision_changed",
         {**base, "authorization": {**base_auth,
                                    "target_revision": revision0 + 1}}),
        # 主体不符：批准绑定 grantee=ai-support，声明换成他人（省略≠通配）
        ("grantee_mismatch", "decision_grantee_mismatch",
         {**base, "authorization": {**base_auth, "grantee_id": "mallory"}}),
        # 载荷被篡改：payload_hash 与批准绑定的载荷不符
        ("payload_forged", "decision_payload_changed",
         {**base, "authorization": {**base_auth,
                                    "payload_hash": "forged-payload-hash"}}),
    ]
    return cases


def test_tampered_payload_claims_rejected_at_worker_tool_gate(tmp_path):
    """④：伪造批准声明在 Worker 工具执行门被拒——错误码精确、
    不产生任何模型调用/工具执行（authorize 相位 fail-closed）。"""
    from evoharness.workflows.code_fix import ScriptTestRunner

    chain = _build_chain(tmp_path)
    app = chain["app"]
    workdir = chain["tmp"] / "gate-ws"
    workdir.mkdir(parents=True, exist_ok=True)
    provider = FakeProvider([{"content": "不应被执行", "final": True,
                              "usage_tokens": 1}])
    adapter = RestrictedAgentAdapter(
        provider=provider, gateway=BudgetGateway(app.session_factory),
        runner=ScriptTestRunner([sys.executable, "-c", "print('checker')"]),
        isolate_checks=False)
    try:
        adapter.bind_run(run_id="gate-probe", org_id="org-demo",
                         space_id="space-support", principal_id=GRANTEE,
                         work_item_id=chain["probe"]["task_id"], session=None)
        for label, expected_code, context in _gate_contexts(chain):
            result = adapter.execute(workdir, context,
                                     frozenset({"write_file", "read_file"}),
                                     lambda p: p)
            assert result.ok is False, (label, result)
            assert result.error == f"authorization_invalid:{expected_code}", \
                (label, result.error)
            assert result.checkpoint.get("phase") == "authorize", (label, result)
            # fail-closed：伪造声明未放行任何付费动作/工具
            assert provider.calls == 0, (label, provider.calls)
    finally:
        app.engine.dispose()


# ── ④（工具调用时刻）载荷与活跃决定不再匹配 → 拒绝且留痕 ────────────────────


def test_payload_superseded_midrun_rejects_next_tool_call_with_trace(tmp_path):
    """④工具时刻：执行中按同版本但不同载荷重批准 → 旧批准被替代（superseded_by
    落库），Run 携带的冻结载荷与活跃决定不再匹配 → 下一工具调用被拒
    （decision_payload_changed）并留痕。"""
    from evoharness.work.models import ExecutionRun

    def reapprove_midrun(probe):
        payload = probe["payload"]
        reapproved = probe["api"].submit_approval(
            org_id="org-demo", actor_id="founder", target_type="work_item",
            target_id=str(probe["task_id"]), target_revision=probe["revision0"],
            payload={**payload["payload"], "goal": "重批准：正文已变更"},
            actions=["work.execute"], grantee_id=GRANTEE)
        assert reapproved.get("ok") is True, reapproved
        assert reapproved["data"]["decision_id"] != probe["decision_id"]

    chain = _build_chain(tmp_path, hook=reapprove_midrun)
    adapter, worker = _worker_for(chain)
    try:
        result = worker.process_run(chain["run_id"])
        rows = sorted(_decision_rows(chain), key=lambda r: r.created_at)
        assert len(rows) == 2
        assert rows[0].decision_id == chain["decision_id"]
        # 旧批准保留 supersede 链（不物理删除），替代者是新批准
        assert rows[0].superseded_by == rows[1].decision_id
        assert rows[0].revoked_at is None  # 替代≠撤回：载荷重验路径
        assert rows[1].superseded_by == ""
        assert "read_file:decision_payload_changed" in adapter.last_refusals, \
            adapter.last_refusals
        assert result.get("ok") is False, result
        assert result.get("run_state") == "failed", result
        with chain["app"].transaction() as session:
            run = session.get(ExecutionRun, chain["run_id"])
            assert run.state == "failed"
            assert "decision_payload_changed" in (run.error or ""), run.error
    finally:
        chain["app"].engine.dispose()
