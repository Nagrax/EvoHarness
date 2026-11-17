import hashlib
import json
from dataclasses import asdict, replace

import pytest

from agents.scoped_skill_report import (evaluate_scoped_skill, read_scoped_skill_report,
                                        write_scoped_skill_report)
from agents.skill_scope import ScopedSkillRef
from agents import skill_evolution as evo


@pytest.fixture
def setup_report(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: demo\ndescription: Answer with a conclusion\n---\nLead with a conclusion.\n", encoding="utf-8")
    ref = ScopedSkillRef("demo", "t", "s", "o", "p", 0)
    digest = hashlib.sha256(skill.read_bytes()).hexdigest()
    return tmp_path, skill, ref, digest


def test_report_requires_exact_ref_current_content_and_real_outcomes(setup_report):
    root, skill, ref, digest = setup_report
    rows = [{"sample_id": "one", "rule_id": "r", "passed": True},
            {"sample_id": "two", "rule_id": "r", "passed": False}]
    write_scoped_skill_report(ref, skill, skill_sha256=digest, protocol_id="p1",
                              run_id="run1", outcomes=rows, evolution_root=root)
    report = read_scoped_skill_report(ref, skill, evolution_root=root)
    assert (report["outcome_count"], report["passed"], report["pass_rate"]) == (2, 1, .5)
    for field, value in [("name", "other"), ("tenant", "other"), ("space", "other"),
                         ("owner", "other"), ("producer", "other"), ("source_revision", 1)]:
        assert read_scoped_skill_report(replace(ref, **{field: value}), skill, evolution_root=root) is None
    report_file = root / "scoped_eval_reports" / (ref.cache_key() + ".json")
    data = json.loads(report_file.read_text(encoding="utf-8"))
    data["pass_rate"] = 1
    report_file.write_text(json.dumps(data), encoding="utf-8")
    assert read_scoped_skill_report(ref, skill, evolution_root=root)["pass_rate"] == .5
    data["ref"]["producer"] = "other"
    report_file.write_text(json.dumps(data), encoding="utf-8")
    assert read_scoped_skill_report(ref, skill, evolution_root=root) is None
    skill.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        write_scoped_skill_report(ref, skill, skill_sha256=digest, protocol_id="p", run_id="r", outcomes=rows, evolution_root=root)


def test_absent_or_malformed_is_never_global_fallback(setup_report):
    root, skill, ref, digest = setup_report
    (root / "online_eval_report.json").write_text('{"skills": [{"name": "demo"}]}')
    assert read_scoped_skill_report(ref, skill, evolution_root=root) is None
    with pytest.raises(ValueError):
        write_scoped_skill_report(ref, skill, skill_sha256=digest, protocol_id="p", run_id="r", outcomes=[{"passed": True}], evolution_root=root)
    assert read_scoped_skill_report(replace(ref, owner=""), skill, evolution_root=root) is None


def test_producer_only_accepts_hash_and_full_ref_bound_usage(setup_report):
    root, skill, ref, digest = setup_report
    sample = {"ref": asdict(ref), "skill_sha256": digest, "scope_generation": 0,
              "messages": [{"role": "user", "content": "question"},
                           {"role": "assistant", "content": "Conclusion: yes."}]}
    for row in ({**sample, "ref": asdict(replace(ref, producer="other"))},
                {**sample, "skill_sha256": "old"}, {"messages": sample["messages"]}):
        result = evaluate_scoped_skill(ref, skill, usage_samples=[row], protocol_id="p", run_id="r", evolution_root=root)
        assert result == {"ok": False, "error": "scope_evaluation_evidence_missing"}
    result = evaluate_scoped_skill(ref, skill, usage_samples=[sample], protocol_id="p", run_id="r", evolution_root=root)
    assert result["ok"] and result["report"]["outcome_count"] > 0


def test_history_count_exact_scope_even_without_usage_log(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(tmp_path))
    scope = {"tenant": "t", "space": "s", "owner": "o", "producer": "p", "source_revision": 2}
    path = evo.history_dir(scope) / "demo.jsonl"
    path.parent.mkdir(parents=True)
    rows = [{"scope": scope}, {"scope": {**scope, "producer": "q"}},
            {"scope": {**scope, "source_revision": 3}}, {"scope": {}}, {}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\ninvalid\n", encoding="utf-8")
    assert evo.load_skill_stats_scoped(scope)["demo"]["snapshots"] == 1


def test_stats_scope_key_delimiters_and_bad_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(tmp_path))
    a = {"tenant": "a|b", "space": "c", "owner": "o", "producer": "p", "source_revision": 0}
    b = {"tenant": "a", "space": "b|c", "owner": "o", "producer": "p", "source_revision": 0}
    path = tmp_path / evo.USAGE_LOG
    rows = [None, [], {"skill": "other", "event": "invoke", "scope": b},
            {"skill": "mine", "event": "invoke", "scope": a}]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert list(evo.load_skill_stats_scoped(a)) == ["mine"]


def test_revision_type_cannot_masquerade_as_integer(setup_report):
    root, skill, ref, digest = setup_report
    ref = replace(ref, source_revision=1)
    rows = [{"sample_id": "one", "rule_id": "r", "passed": True}]
    write_scoped_skill_report(ref, skill, skill_sha256=digest, protocol_id="p", run_id="r", outcomes=rows, evolution_root=root)
    path = root / "scoped_eval_reports" / (ref.cache_key() + ".json")
    report = json.loads(path.read_text(encoding="utf-8"))
    report["ref"]["source_revision"] = True
    path.write_text(json.dumps(report), encoding="utf-8")
    assert read_scoped_skill_report(ref, skill, evolution_root=root) is None


def test_real_skill_execution_persists_loaded_content_binding(tmp_path, monkeypatch):
    from agents import skills
    import hashlib

    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(tmp_path / "evolution"))
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    skill = skill_dir / "SKILL.md"
    scope = {"tenant": "t", "space": "s", "owner": "o", "producer": "producer", "source_revision": 3}
    skill.write_text("---\nname: demo\nscope-json: " + json.dumps(scope) +
                     "\n---\nOriginal instruction\n", encoding="utf-8")
    loaded_hash = hashlib.sha256(skill.read_bytes()).hexdigest()
    parsed = skills._parse_skill_file(skill, "project", str(skill_dir))
    monkeypatch.setattr(skills, "_cached_skills", [parsed])
    ref = ScopedSkillRef("demo", "t", "s", "o", "producer", 3)
    skill.write_text("updated after loading", encoding="utf-8")
    assert "Original instruction" in skills.execute_skill("demo", "", scope_ref=ref)["prompt"]
    log = tmp_path / "evolution" / evo.USAGE_LOG
    event = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert event["ref"] == asdict(ref)
    assert event["skill_sha256"] == loaded_hash
    assert event["skill_sha256"] != hashlib.sha256(skill.read_bytes()).hexdigest()
    bad_ref = replace(ref, source_revision=True)
    skills.execute_skill("demo", "", scope_ref=bad_ref)
    event = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert "ref" not in event and "skill_sha256" not in event


def test_agent_turn_record_and_scoped_evaluation(tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime, timezone, timedelta
    from agents import skills
    from agents.agent import Agent
    from agents.scoped_skill_report import evaluate_scoped_skill_from_log

    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(tmp_path / "state"))
    directory = tmp_path / "demo"
    directory.mkdir()
    skill = directory / "SKILL.md"
    scope = {"tenant": "t", "space": "s", "owner": "o", "producer": "p", "source_revision": 1}
    skill.write_text("---\nname: demo\ndescription: conclusion\nscope-json: " +
                     json.dumps(scope) + "\n---\nLead with a conclusion.\n", encoding="utf-8")
    parsed = skills._parse_skill_file(skill, "project", str(directory))
    monkeypatch.setattr(skills, "_cached_skills", [parsed])
    agent = Agent(api_base="http://127.0.0.1:9/v1", api_key="fake", model="fake")
    agent.skill_scope = scope
    agent._mcp_initialized = True
    agent._augment_user_message_with_skill_context = lambda text: (text, None)
    agent._pop_pending_skill_extraction_window = lambda text: None
    agent._set_pending_skill_extraction_window = lambda **kwargs: None
    agent._schedule_background_skill_task = lambda coro: coro.close()
    agent._finalize_staged_chat = lambda: None
    agent._auto_save = lambda: None
    async def fake_chat(message):
        if message == "first":
            await agent._execute_skill_tool({"skill_name": "demo"})
        agent._turn_output_buffer.append("Conclusion: " + message)
    agent._chat_openai = fake_chat
    asyncio.run(agent.chat("first"))
    asyncio.run(agent.chat("second"))
    ref = ScopedSkillRef("demo", **scope)
    root = tmp_path / "state"
    log = root / "scoped_eval_samples" / (ref.cache_key() + ".jsonl")
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["messages"][0]["content"] == "first"
    assert rows[0]["messages"][1]["content"] == "Conclusion: first"
    result = evaluate_scoped_skill_from_log(ref, skill, protocol_id="v1", run_id="run")
    assert result["ok"] and result["report"]["outcome_count"] > 0
    assert evaluate_scoped_skill_from_log(ref, skill, protocol_id="v1", run_id="r2", scope_generation=2)["error"] == "scope_evaluation_evidence_missing"
    assert read_scoped_skill_report(ref, skill, scope_generation=2) is None
    skill.write_text(skill.read_text(encoding="utf-8") + "changed", encoding="utf-8")
    assert evaluate_scoped_skill_from_log(ref, skill, protocol_id="v1", run_id="r4")["error"] == "scope_evaluation_evidence_missing"


def test_report_distinguishes_samples_from_checks_and_rejects_duplicates(setup_report):
    root, skill, ref, digest = setup_report
    rows = [{"sample_id": "s", "rule_id": "r1", "passed": True},
            {"sample_id": "s", "rule_id": "r2", "passed": False}]
    write_scoped_skill_report(ref, skill, skill_sha256=digest, protocol_id="p", run_id="r", outcomes=rows, evolution_root=root)
    report = read_scoped_skill_report(ref, skill, evolution_root=root)
    assert (report["samples"], report["check_count"], report["passed"], report["pass_rate"]) == (1, 2, 1, .5)
    with pytest.raises(ValueError):
        write_scoped_skill_report(ref, skill, skill_sha256=digest, protocol_id="p", run_id="r", outcomes=rows + [rows[0]], evolution_root=root)


def test_turn_collection_rejects_wrong_producer_plan_and_abort(tmp_path, monkeypatch):
    import asyncio
    from agents import skills
    from agents.agent import Agent
    root = tmp_path / "state"
    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(root))
    scope = {"tenant": "t", "space": "s", "owner": "o", "producer": "p", "source_revision": 1}
    directory = tmp_path / "demo"
    directory.mkdir()
    file = directory / "SKILL.md"
    file.write_text("---\nname: demo\nscope-json: " + json.dumps(scope) + "\n---\nanswer", encoding="utf-8")
    monkeypatch.setattr(skills, "_cached_skills", [skills._parse_skill_file(file, "project", str(directory))])
    agent = Agent(api_base="http://127.0.0.1:9/v1", api_key="fake", model="fake")
    agent._mcp_initialized = True
    agent._augment_user_message_with_skill_context = lambda text: (text, None)
    agent._pop_pending_skill_extraction_window = lambda text: None
    agent._set_pending_skill_extraction_window = lambda **kwargs: None
    agent._schedule_background_skill_task = lambda coro: coro.close()
    agent._finalize_staged_chat = lambda: None
    agent._auto_save = lambda: None
    async def run(message):
        await agent._execute_skill_tool({"skill_name": "demo"})
        agent._turn_output_buffer.append("answer")
        if message == "aborted":
            raise asyncio.CancelledError()
    agent._chat_openai = run
    agent.skill_scope = {**scope, "producer": "different"}
    asyncio.run(agent.chat("wrong producer"))
    agent.skill_scope = scope
    agent.permission_mode = "plan"
    asyncio.run(agent.chat("plan"))
    agent.permission_mode = "default"
    asyncio.run(agent.chat("aborted"))
    assert not (root / "scoped_eval_samples").exists()


def test_holdout_does_not_write_samples_or_reports(setup_report, monkeypatch):
    from agents.scoped_skill_report import record_scoped_turn
    root, skill, ref, digest = setup_report
    rows = [{"sample_id": "s", "rule_id": "r", "passed": True}]
    write_scoped_skill_report(ref, skill, skill_sha256=digest, protocol_id="p",
                              run_id="before", outcomes=rows, evolution_root=root)
    report_file = root / "scoped_eval_reports" / (ref.cache_key() + ".json")
    before = report_file.read_bytes()
    monkeypatch.setenv("EVOHARNESS_HOLDOUT_READONLY", "1")
    result = write_scoped_skill_report(ref, skill, skill_sha256=digest, protocol_id="p",
                                       run_id="after", outcomes=rows, evolution_root=root)
    assert result == {"ok": False, "error": "skill_state_read_only"}
    assert record_scoped_turn([{"ref": asdict(ref), "skill_sha256": digest}],
                              user_text="question", assistant_text="answer",
                              evolution_root=root) == 0
    assert report_file.read_bytes() == before
    assert not (root / "scoped_eval_samples").exists()
    sample = {"ref": asdict(ref), "skill_sha256": digest, "scope_generation": 0,
              "messages": [{"role": "user", "content": "question"},
                           {"role": "assistant", "content": "Conclusion: yes"}]}
    evaluated = evaluate_scoped_skill(ref, skill, usage_samples=[sample], protocol_id="p",
                                      run_id="read-only", evolution_root=root)
    assert evaluated["error"] == "skill_state_read_only" and evaluated["outcomes"]
    assert report_file.read_bytes() == before
