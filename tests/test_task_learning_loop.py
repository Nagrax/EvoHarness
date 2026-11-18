"""Task A feedback becomes a scoped candidate and its next Worker loads v2."""

import json

from test_execution_knowledge import env
from test_worker_scoped_sop import skill
from evoharness.governance.budget_gateway import BudgetGateway
from evoharness.work.agent_adapter import FakeProvider, RestrictedAgentAdapter
from evoharness.work.worker import WorkWorker
from evoharness.work.models import ExecutionRun, SkillLearningEvidence
from agents.frontmatter import parse_frontmatter


def deliver(app, api, run_id):
    def script(context):
        if not context["history"]:
            return [
                {
                    "tool_calls": [
                        {"name": "skill", "arguments": {"skill_name": "refund"}}
                    ]
                }
            ]
        return [{}, {"content": "按退款规则给出答复。", "final": True}]

    adapter = RestrictedAgentAdapter(
        provider=FakeProvider(script), gateway=BudgetGateway(app.session_factory)
    )
    result = WorkWorker(
        app.session_factory, adapter, api._authorize_adapter()
    ).process_run(run_id)
    assert result["ok"], result


def feedback(env, tmp_path, monkeypatch):
    app, api, task_id, run_id, _ = env
    path = skill(tmp_path, monkeypatch)
    deliver(app, api, run_id)
    assert api.request_acceptance(task_id, run_id=run_id, requester="ai-support")["ok"]
    result = api.review_acceptance(
        task_id,
        run_id=run_id,
        reviewer="founder",
        verdict="pass",
        notes="先核对订单，再查询权威退款规则，保留引用。",
    )
    assert result["ok"] and result["data"]["state"] == "done", result
    row = api.get_work_item(task_id)["data"]["learning"][0]
    return path, row["evidence_id"]


def extractor(seen):
    async def query(system, user):
        seen.append((system, user))
        assert "task-B-private-secret" not in user
        if "Set Manager" in system:
            assert "Never create a new Skill" in system
            assert [skill["name"] for skill in json.loads(user)["existing_skills"]] == ["refund"]
            return json.dumps(
                {
                    "action": "revise",
                    "target_skill": "refund",
                    "revised_instructions": "先核对订单，再读取退款规则，核对来源后答复。",
                    "reason": "遵循任务责任人的明确纠正。",
                },
                ensure_ascii=False,
            )
        assert "only source of new guidance" in system
        assert json.loads(user)["authorized_review"] == "先核对订单，再查询权威退款规则，保留引用。"
        return json.dumps(
            {
                "skills": [
                    {
                        "name": "refund",
                        "description": "退款规则核对",
                        "instructions": "先核对订单，再读取退款规则，核对来源后答复。",
                        "when_to_use": "核对退款规则时",
                        "evidence": "任务责任人的明确纠正",
                    }
                ]
            },
            ensure_ascii=False,
        )

    return query


def test_scoped_feedback_adoption_and_next_run(env, tmp_path, monkeypatch):
    app, api, task_id, run_id, _ = env
    path, evidence_id = feedback(env, tmp_path, monkeypatch)
    before = path.read_bytes()
    unrelated = api.create_work_item(
        tenant="local",
        owner="founder",
        title="task-B-private-secret",
        goal="task-B-private-secret",
        acceptance=[{"kind": "qa"}],
        workflow_kind="delegated_delivery",
        org_id="org-demo",
        space_id="space-support",
    )
    assert unrelated["ok"] and unrelated["data"]["id"] != task_id
    seen = []
    built = api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        actor="founder",
        _side_query=extractor(seen),
    )
    assert built["ok"] and built["data"]["result"].get("proposal"), built
    assert path.read_bytes() == before  # Extraction never edits the active SOP.
    assert len(seen) == 2
    replay = api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        actor="founder",
        _side_query=extractor(seen),
    )
    assert replay["ok"] and len(seen) == 2
    adopted = api.learning_action(
        task_id, evidence_id=evidence_id, action="adopt", actor="founder"
    )
    assert adopted["ok"], adopted
    from agents.skill_tiers import get_tier_scoped

    assert (
        get_tier_scoped(
            "refund", dict(tenant="local", space="space-support", owner="ai-support")
        )
        == "experimental"
    )
    assert "先核对订单" in path.read_text(encoding="utf-8")
    assert (
        parse_frontmatter(path.read_text(encoding="utf-8")).meta["version"] == "1.0.1"
    )
    repeated = api.learning_action(
        task_id, evidence_id=evidence_id, action="adopt", actor="founder"
    )
    assert repeated == adopted

    created = api.create_work_item(
        tenant="local",
        owner="founder",
        title="再次核对退款规则",
        action="answer",
        goal="请按退款规则解答",
        workflow_kind="delegated_delivery",
        acceptance=[{"kind": "qa", "required_artifacts": ["answer", "evidence"]}],
        org_id="org-demo",
        space_id="space-support",
    )
    next_task = created["data"]["id"]
    assert api.transition_work_item(next_task, to_state="queued", actor="founder")["ok"]
    payload = api.run_request_payload(next_task)["data"]
    assert api.submit_approval(
        org_id="org-demo",
        actor_id="founder",
        target_type="work_item",
        target_id=str(next_task),
        target_revision=payload["target_revision"],
        payload=payload["payload"],
        actions=["work.execute"],
        grantee_id="ai-support",
    )["ok"]
    started = api.start_run(
        next_task, actor_id="ai-support", output_root=str(tmp_path / "next")
    )
    assert started["ok"], started
    next_run = started["data"]["operation_id"]
    deliver(app, api, next_run)
    with app.transaction() as session:
        loaded = json.loads(session.get(ExecutionRun, next_run).checkpoint_json)[
            "skills"
        ]["loaded"][0]
        assert (
            loaded["file_sha256"] == adopted["data"]["result"]["publication"]["sha256"]
        )
        assert session.get(SkillLearningEvidence, evidence_id).state == "adopted"


def test_wrong_actor_and_holdout_cannot_extract(env, tmp_path, monkeypatch):
    app, api, task_id, _, _ = env
    path, evidence_id = feedback(env, tmp_path, monkeypatch)
    seen = []
    rejected = api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        actor="stranger",
        _side_query=extractor(seen),
    )
    assert not rejected["ok"] and seen == []
    monkeypatch.setenv("EVOHARNESS_HOLDOUT_READONLY", "1")
    rejected = api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        actor="founder",
        _side_query=extractor(seen),
    )
    assert (
        not rejected["ok"] and rejected["reason_code"] == "learning_holdout_read_only"
    ), rejected
    assert (
        seen == []
        and parse_frontmatter(path.read_text(encoding="utf-8")).meta["version"]
        == "1.0.0"
    )


def test_replaced_source_cannot_receive_old_task_feedback(env, tmp_path, monkeypatch):
    app, api, task_id, _, _ = env
    path, evidence_id = feedback(env, tmp_path, monkeypatch)
    path.write_text(path.read_text(encoding="utf-8") + "\n新决定。", encoding="utf-8")
    seen = []
    result = api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        actor="founder",
        _side_query=extractor(seen),
    )
    assert not result["ok"] and result["reason_code"] == "skill_context_changed", result
    assert seen == []


def test_hosted_actor_name_does_not_cross_application_namespace(
    env, tmp_path, monkeypatch
):
    app, api, task_id, _, _ = env
    _, evidence_id = feedback(env, tmp_path, monkeypatch)
    seen = []
    result = api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        principal=dict(
            subject="founder", org_ids=["org-demo"], tenant_id="local", app_id="other"
        ),
        _side_query=extractor(seen),
    )
    assert (
        not result["ok"] and result["reason_code"] == "principal_namespace_mismatch"
    ), result
    assert seen == []


def test_revoked_actor_cannot_replay_adoption_receipt(env, tmp_path, monkeypatch):
    from sqlalchemy import select
    from evoharness.organization.models import Membership

    app, api, task_id, _, _ = env
    _, evidence_id = feedback(env, tmp_path, monkeypatch)
    assert api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        actor="founder",
        _side_query=extractor([]),
    )["ok"]
    assert api.learning_action(
        task_id, evidence_id=evidence_id, action="adopt", actor="founder"
    )["ok"]
    with app.transaction() as session:
        session.scalar(
            select(Membership).where(Membership.principal_id == "founder")
        ).status = "revoked"
    result = api.learning_action(
        task_id, evidence_id=evidence_id, action="adopt", actor="founder"
    )
    assert (
        not result["ok"] and result["reason_code"] == "learning_actor_not_authorized"
    ), result


def test_adoption_recovers_file_publication_after_database_rollback(
    env, tmp_path, monkeypatch
):
    import pytest
    from evoharness.knowledge.task_learning import adopt_candidate

    app, api, task_id, _, _ = env
    path, evidence_id = feedback(env, tmp_path, monkeypatch)
    assert api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        actor="founder",
        _side_query=extractor([]),
    )["ok"]
    with pytest.raises(RuntimeError, match="lost database commit"):
        with app.transaction() as session:
            adopt_candidate(session, evidence_id, actor="founder")
            raise RuntimeError("lost database commit")
    published = path.read_bytes()
    assert parse_frontmatter(published.decode("utf-8")).meta["version"] == "1.0.1"
    retried = api.learning_action(
        task_id, evidence_id=evidence_id, action="adopt", actor="founder"
    )
    assert retried["ok"], retried
    assert path.read_bytes() == published
    with app.transaction() as session:
        assert session.get(SkillLearningEvidence, evidence_id).state == "adopted"


def test_prepared_adoption_retries_after_file_switch_failure(
    env, tmp_path, monkeypatch
):
    from evoharness.knowledge import release

    app, api, task_id, _, _ = env
    path, evidence_id = feedback(env, tmp_path, monkeypatch)
    assert api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        actor="founder",
        _side_query=extractor([]),
    )["ok"]
    before = path.read_bytes()
    original = release.os.replace
    failed = []

    def replace(source, target):
        if str(target) == str(path) and not failed:
            failed.append(True)
            raise OSError("injected switch failure")
        return original(source, target)

    monkeypatch.setattr(release.os, "replace", replace)
    first = api.learning_action(
        task_id, evidence_id=evidence_id, action="adopt", actor="founder"
    )
    assert not first["ok"] and path.read_bytes() == before
    retried = api.learning_action(
        task_id, evidence_id=evidence_id, action="adopt", actor="founder"
    )
    assert retried["ok"], retried
    assert (
        parse_frontmatter(path.read_text(encoding="utf-8")).meta["version"] == "1.0.1"
    )


def test_recovery_does_not_overwrite_an_intervening_source_edit(
    env, tmp_path, monkeypatch
):
    import pytest
    from evoharness.knowledge.task_learning import adopt_candidate

    app, api, task_id, _, _ = env
    path, evidence_id = feedback(env, tmp_path, monkeypatch)
    assert api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        actor="founder",
        _side_query=extractor([]),
    )["ok"]
    with pytest.raises(RuntimeError):
        with app.transaction() as session:
            adopt_candidate(session, evidence_id, actor="founder")
            raise RuntimeError("lost commit")
    path.write_text(
        path.read_text(encoding="utf-8") + "\n责任人的新决定。", encoding="utf-8"
    )
    newer = path.read_bytes()
    result = api.learning_action(
        task_id, evidence_id=evidence_id, action="adopt", actor="founder"
    )
    assert not result["ok"] and result["reason_code"] == "skill_context_changed", result
    assert path.read_bytes() == newer
