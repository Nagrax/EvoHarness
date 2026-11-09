"""EH01：pending skill extraction window 落盘、恢复、去重与失败重试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.agent as agent_mod
import agents.extraction_window as extraction_store
import agents.session as session_store


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("EVOHARNESS_HOLDOUT_READONLY", raising=False)
    monkeypatch.setenv("EVOHARNESS_AUTO_SKILL_EVOLUTION", "1")
    monkeypatch.setattr(session_store, "SESSION_DIR", tmp_path / "sessions")
    return tmp_path


def _agent(session_id: str = "sess-eh01"):
    agent = agent_mod.Agent(
        permission_mode="default",
        model="stub-model",
        api_base="http://127.0.0.1:9/v1",
        api_key="stub-key",
    )
    agent.session_id = session_id
    return agent


def _seed(agent):
    agent._anthropic_messages = [
        {"role": "user", "content": "固定格式导出"},
        {"role": "assistant", "content": "已按模板导出"},
    ]
    agent._openai_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "固定格式导出"},
        {"role": "assistant", "content": "已按模板导出"},
    ]
    agent._set_pending_skill_extraction_window(
        original_user_message="固定格式导出",
        assistant_text="已按模板导出",
        retrieved_reference=None,
    )
    return agent._pending_skill_extraction_window


def test_pending_window_survives_auto_save_and_restore(isolated):
    agent = _agent()
    window = _seed(agent)
    assert window and window["window_id"]
    record = extraction_store.load_window(agent.session_id)
    assert record["status"] == "pending"

    agent._auto_save()

    restored = _agent(agent.session_id)
    restored.restore_session({})
    assert restored._pending_skill_extraction_window is not None
    assert restored._pending_skill_extraction_window["latest_user"] == "固定格式导出"


def test_cli_resume_keeps_original_session_id(isolated, monkeypatch):
    import agents.main as main_mod

    class FakeAgent:
        session_id = "new-session"
        restored = None

        def restore_session(self, payload):
            self.restored = payload

    agent = FakeAgent()
    monkeypatch.setattr(main_mod, "get_latest_session_id", lambda: "sess-cli-old")
    monkeypatch.setattr(
        main_mod,
        "load_session",
        lambda sid: {"metadata": {"protocol": "anthropic"}, "anthropicMessages": []},
    )
    assert main_mod._resume_latest_session(agent) is True
    assert agent.session_id == "sess-cli-old"
    assert agent.restored is not None


def test_old_session_payload_without_window_still_works(isolated):
    agent = _agent("sess-old-payload")
    agent.restore_session({"anthropicMessages": [{"role": "user", "content": "legacy"}]})
    assert agent._pending_skill_extraction_window is None
    assert agent.last_skill_extraction_status is None


def test_claim_is_exclusive_and_pop_is_idempotent(isolated):
    agent = _agent()
    window = _seed(agent)
    claimed = agent._pop_pending_skill_extraction_window("下一次反馈")
    assert claimed is not None
    assert claimed["next_user_feedback"] == "下一次反馈"
    record = extraction_store.load_window(agent.session_id)
    assert record["status"] == "claimed"
    assert record["attempts"] == 1

    assert agent._pop_pending_skill_extraction_window("重复领取") is None
    assert extraction_store.claim_window(agent.session_id, window["window_id"]) is None


def test_failed_extraction_can_be_retried_and_completed_blocks_replay(isolated):
    agent = _agent()
    window = _seed(agent)
    assert agent._pop_pending_skill_extraction_window("") is not None
    extraction_store.mark_failed(agent.session_id, window["window_id"], error="provider timeout")
    record = extraction_store.load_window(agent.session_id)
    assert record["status"] == "failed"
    assert record["lastError"] == "provider timeout"

    retried = extraction_store.claim_window(agent.session_id, window["window_id"])
    assert retried is not None
    assert retried["attempts"] == 2
    extraction_store.mark_completed(agent.session_id, window["window_id"], summary="none")
    assert extraction_store.claim_window(agent.session_id, window["window_id"]) is None


def test_restore_completed_window_is_visible_and_not_requeued(isolated):
    agent = _agent()
    window = _seed(agent)
    assert agent._pop_pending_skill_extraction_window("") is not None
    extraction_store.mark_completed(agent.session_id, window["window_id"], summary="none")

    restored = _agent(agent.session_id)
    restored.restore_session({})
    assert restored._pending_skill_extraction_window is None
    assert restored.last_skill_extraction_status["status"] == "completed"


def test_extract_now_reads_persisted_window(isolated, monkeypatch):
    agent = _agent()
    window = _seed(agent)
    reopened = _agent(agent.session_id)

    import agents.online_skill_evolution as online_evolution

    async def fake_ingest(**kwargs):
        return {"ok": True, "action": "none"}

    monkeypatch.setattr(online_evolution, "online_ingest", fake_ingest)
    monkeypatch.setattr(reopened, "_build_side_query", lambda **kwargs: object())

    result = asyncio.run(reopened.extract_now())
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert extraction_store.load_window(agent.session_id)["status"] == "completed"
    assert extraction_store.load_window(agent.session_id)["windowId"] == window["window_id"]


def test_restore_reclaims_fresh_claim_after_process_restart(isolated):
    agent = _agent()
    window = _seed(agent)
    assert extraction_store.claim_window(agent.session_id, window["window_id"]) is not None
    record = extraction_store.load_window(agent.session_id)
    record["claimedBy"] = "previous-process"
    extraction_store._atomic_record(agent.session_id, record)

    restored = _agent(agent.session_id)
    restored.restore_session({})
    assert restored._pending_skill_extraction_window is not None
    record = extraction_store.load_window(agent.session_id)
    assert record["status"] == "failed"
    assert "claim_interrupted" in record["lastError"]


def test_same_process_claim_is_not_reclaimed_on_restore(isolated):
    agent = _agent()
    window = _seed(agent)
    assert extraction_store.claim_window(agent.session_id, window["window_id"]) is not None

    restored = _agent(agent.session_id)
    restored.restore_session({})
    assert restored._pending_skill_extraction_window is None
    assert restored.last_skill_extraction_status["status"] == "claimed"


def test_store_unavailable_does_not_crash_main_path(isolated, monkeypatch):
    blocked = isolated / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr(session_store, "SESSION_DIR", blocked)

    agent = _agent("sess-unwritable")
    agent._pending_skill_extraction_window = {
        "window_id": "w1",
        "messages": [{"role": "user", "content": "q"}],
        "latest_user": "q",
        "latest_assistant": "a",
    }
    assert agent._pop_pending_skill_extraction_window("") is None
    assert agent.last_save_error

    restored = _agent("sess-unwritable")
    restored.restore_session({})
    assert restored._pending_skill_extraction_window is None


def test_holdout_readonly_does_not_claim_or_write(isolated, monkeypatch):
    agent = _agent()
    _seed(agent)
    monkeypatch.setenv("EVOHARNESS_HOLDOUT_READONLY", "1")
    assert agent._pop_pending_skill_extraction_window("") is None
    assert extraction_store.load_window(agent.session_id)["status"] == "pending"


def test_expired_claim_is_recovered_for_retry(isolated):
    agent = _agent()
    window = _seed(agent)
    claimed = extraction_store.claim_window(agent.session_id, window["window_id"])
    assert claimed is not None
    stale = claimed["claimedAt"] - extraction_store.CLAIM_LEASE_SECONDS - 1
    record = extraction_store.load_window(agent.session_id)
    record["claimedAt"] = stale
    extraction_store._atomic_record(agent.session_id, record)

    recovered = extraction_store.recover_window(agent.session_id)
    assert recovered["status"] == "failed"
    assert "claim_expired" in recovered["lastError"]

    restored = _agent(agent.session_id)
    restored.restore_session({})
    assert restored._pending_skill_extraction_window is not None