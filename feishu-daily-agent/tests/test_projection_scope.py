"""Regression inputs for permission loss and source-owned projection invalidation."""
import json
import os
from pathlib import Path
import pytest
from app.core.database import session_scope
from app.memory import experience, skill_bridge, skill_sync
from app.memory.projection_scope import ProjectionScope, default_scope, root_for
from app.models import Experience, Message, Source


@pytest.fixture
def projection(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "feishu_owner_open_id", "ou_a")
    monkeypatch.setenv("EVOHARNESS_SKILLS_ROOT", str(tmp_path / "skills"))
    return tmp_path


def test_payload_preserves_permissions_and_source_revision(projection):
    with session_scope() as session:
        item = experience.create_candidate(session, question="年假A", answer="private answer", owner="ou_a",
            visibility="private", sensitive_level="private", audience={"chat_ids": ["oc_a"], "space_id": "space-a"})
        payload = skill_sync._as_dict(item)
        assert payload["tenant"] == "default" and payload["owner"] == "ou_a"
        assert payload["visibility"] == payload["sensitive_level"] == "private"
        assert payload["audience"]["chat_ids"] == ["oc_a"]
        assert payload["space_id"] == "space-a" and payload["version"] == 1
        assert payload["producer_kind"] == "projection"


def test_private_owners_and_spaces_do_not_merge(projection):
    with session_scope() as session:
        a = experience.create_candidate(session, question="年假甲", answer="A-PRIVATE", owner="ou_a")
        b = experience.create_candidate(session, question="年假乙", answer="B-PRIVATE", owner="ou_b")
        c = experience.create_candidate(session, question="年假丙", answer="SPACE-PRIVATE", owner="ou_a", audience={"space_id": "space-other"})
        scopes = [ProjectionScope.from_payload(skill_sync._as_dict(x)) for x in (a,b,c)]
    paths = [root_for(scope) / "feishu-hr/SKILL.md" for scope in scopes]
    for path, own, other in [(paths[0], "A-PRIVATE", "B-PRIVATE"), (paths[1], "B-PRIVATE", "A-PRIVATE"), (paths[2], "SPACE-PRIVATE", "A-PRIVATE")]:
        assert own in path.read_text(encoding="utf-8")
        assert other not in path.read_text(encoding="utf-8")
    assert not paths[1].is_relative_to(projection / "skills")
    assert not paths[2].is_relative_to(projection / "skills")
    assert len(skill_bridge.list_skill_domains()) == 1


def test_binding_mismatch_blocks_read_and_write(projection, settings, monkeypatch):
    skill_bridge.sync_domain("hr", [{"id": 1, "question": "年假", "answer": "A-PRIVATE"}])
    original = (projection / "skills/feishu-hr/SKILL.md").read_bytes()
    monkeypatch.setattr(settings, "feishu_owner_open_id", "ou_b")
    assert skill_bridge.list_skill_domains() == []
    with pytest.raises(ValueError, match="scope_mismatch"):
        skill_bridge.sync_domain("hr", [{"id": 2, "question": "年假", "answer": "B-PRIVATE"}])
    assert (projection / "skills/feishu-hr/SKILL.md").read_bytes() == original


def test_source_revoke_and_delete_remove_active_content(projection):
    with session_scope() as session:
        source = Source(tenant="default", owner="ou_a", chat_id="oc_a", enabled=True)
        session.add(source);session.flush()
        session.add(Message(tenant="default", message_id="om_source", source_id=source.id,
            chat_id="oc_a", content_text="source", create_time=1))
    with session_scope() as session:
        item = experience.create_candidate(session, question="年假有几天", answer="SOURCE-PRIVATE", owner="ou_a", source_message_id="om_source")
        item_id = item.id
    path = projection / "skills/feishu-hr/SKILL.md"
    assert "SOURCE-PRIVATE" in path.read_text(encoding="utf-8")
    with session_scope() as session:
        source = session.query(Source).one()
        source.enabled = False
    assert "SOURCE-PRIVATE" not in path.read_text(encoding="utf-8")
    with session_scope() as session:
        session.query(Source).one().enabled = True
    assert "SOURCE-PRIVATE" in path.read_text(encoding="utf-8")
    with session_scope() as session:
        session.delete(session.get(Experience, item_id))
    assert "SOURCE-PRIVATE" not in path.read_text(encoding="utf-8")


def test_rollback_does_not_publish_uncommitted_answers(projection):
    with pytest.raises(RuntimeError):
        with session_scope() as session:
            experience.create_candidate(session, question="年假", answer="UNCOMMITTED", owner="ou_a")
            raise RuntimeError("rollback")
    assert not (projection / "skills/feishu-hr/SKILL.md").exists()


def test_paused_latest_version_does_not_resurrect_old_answer(projection):
    with session_scope() as session:
        first = experience.create_candidate(session, question="年假", answer="OLD", owner="ou_a")
        second = experience.create_candidate(session, question="年假", answer="NEW", owner="ou_a", intent_id=first.intent_id)
        experience.pause(session, second.id, actor="ou_a")
    text = (projection / "skills/feishu-hr/SKILL.md").read_text(encoding="utf-8")
    assert "OLD" not in text and "NEW" not in text


def test_projection_metadata_idempotency_no_loop_and_no_environment_switch(projection):
    before = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE", "EVOHARNESS_EVOLUTION_DIR")}
    payload = [{"id": 1, "question": "年假", "answer": "ANSWER", "status": "candidate"}]
    skill_bridge.sync_domain("hr", payload)
    path = projection / "skills/feishu-hr/SKILL.md"
    first = path.read_bytes()
    assert skill_bridge.sync_domain("hr", payload).action == "unchanged"
    assert path.read_bytes() == first
    meta = json.loads((path.parent / ".projection.json").read_text(encoding="utf-8"))
    assert meta["managed_by"] == "experience.skill_sync" and meta["producer_kind"] == "projection"
    assert meta["source_revisions"][0]["status"] == "candidate"
    assert skill_bridge.import_skill_as_candidates("feishu-hr", tenant="default")["created"] == []
    assert skill_bridge.tier_of("hr") == "experimental"
    assert {key: os.environ.get(key) for key in before} == before


def test_failed_projection_queues_without_answer_then_rebuilds(projection, monkeypatch):
    original = skill_bridge.sync_domain
    monkeypatch.setattr(skill_bridge, "sync_domain", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied")))
    with session_scope() as session:
        experience.create_candidate(session, question="年假", answer="QUEUE-PRIVATE", owner="ou_a")
    queue = list(projection.rglob(".projection-rebuild/*.json"))
    assert queue
    assert all("QUEUE-PRIVATE" not in x.read_text(encoding="utf-8") for x in queue)
    monkeypatch.setattr(skill_bridge, "sync_domain", original)
    with session_scope() as session:
        skill_sync.sync_all(session)
    assert all(not x.exists() for x in queue)
    assert "QUEUE-PRIVATE" in (projection / "skills/feishu-hr/SKILL.md").read_text(encoding="utf-8")


def test_unknown_scope_and_path_traversal_are_rejected(projection):
    with pytest.raises(ValueError, match="scope_missing"):
        skill_bridge.sync_domain("hr", [{"tenant": "default", "owner": "", "question": "secret", "answer": "secret"}])
    assert skill_bridge.import_skill_as_candidates("../elsewhere", tenant="default")["ok"] is False


def test_foreign_owner_source_cannot_supply_private_projection(projection):
    with session_scope() as session:
        source = Source(tenant="default", owner="ou_b", chat_id="oc_b", enabled=True)
        session.add(source);session.flush()
        session.add(Message(tenant="default", message_id="om_other", source_id=source.id,
            chat_id="oc_b", content_text="B source", create_time=1))
    with session_scope() as session:
        experience.create_candidate(session, question="年假", answer="B-SOURCE-SECRET",
            owner="ou_a", source_message_id="om_other")
    assert "B-SOURCE-SECRET" not in (projection / "skills/feishu-hr/SKILL.md").read_text(encoding="utf-8")


def test_native_evolution_cannot_rewrite_source_managed_projection(projection):
    skill_bridge.sync_domain("hr", [{"id": 1, "question": "年假", "answer": "ORIGINAL"}])
    path = projection / "skills/feishu-hr/SKILL.md"
    before = path.read_bytes()
    native = skill_bridge._import_evolution()
    result = native.evolve_skill_file(skill_name="feishu-hr", lesson="candidate change", instructions="REWRITE",
        target="active", active_dir=str(path.parent))
    assert result == {"ok": False, "error": "managed_projection_source_owned"}
    assert path.read_bytes() == before


def test_holdout_does_not_even_bind_projection_root(projection, monkeypatch):
    monkeypatch.setenv("EVOHARNESS_HOLDOUT_READONLY", "1")
    result = skill_bridge.sync_domain("hr", [{"id": 1, "question": "年假", "answer": "HELDOUT"}])
    assert result.action == "skipped"
    assert not (projection / "skills").exists()


def test_explicit_sync_rollback_does_not_change_committed_projection(projection):
    with session_scope() as session:
        item = experience.create_candidate(session, question="年假", answer="COMMITTED", owner="ou_a")
        item_id = item.id
    path = projection / "skills/feishu-hr/SKILL.md"
    before = path.read_bytes()
    with pytest.raises(RuntimeError):
        with session_scope() as session:
            session.get(Experience, item_id).canonical_answer = "ROLLBACK"
            session.flush()
            skill_sync.sync_all(session)
            raise RuntimeError("rollback")
    assert path.read_bytes() == before
