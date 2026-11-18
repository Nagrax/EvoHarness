import hashlib

from agents.skill_search.feedback import (
    TraceStore, build_attribution_input, render_attribution_prompt,
)


def _input(store, consumed=None):
    return build_attribution_input(
        case={"case_id": "sample", "protocol_role": "S", "question": "Check JSON types"},
        evaluation={"case_id": "sample", "candidate_id": "seed",
                    "trace_ref": "trace://sample", "terminal_status": "skill_scored_fail"},
        trace_store=store, consumed_chunk_refs=consumed, max_chars=40,
    )


def test_prompt_exposes_consumed_event_text_and_matching_hash():
    store = TraceStore({"trace://sample": [
        {"event_id": "answer", "kind": "assistant_message", "text": '{"flag":"false"}'},
        {"event_id": "grade", "kind": "verdict", "text": "flag_must_be_boolean"},
    ]})
    payload = _input(store)
    prompt = render_attribution_prompt(payload)
    assert "flag_must_be_boolean" in prompt
    for chunk in payload["chunks"]:
        assert hashlib.sha256(chunk["text"].encode()).hexdigest() == chunk["sha256"]
    # Durable trace metadata stays small and does not duplicate raw event text.
    assert all("text" not in chunk.to_dict() for chunk in store.chunks("trace://sample"))


def test_prompt_does_not_expose_unconsumed_events():
    store = TraceStore({"trace://sample": [
        {"event_id": "visible", "kind": "verdict", "text": "observed format failure"},
        {"event_id": "unread", "kind": "tool_result", "text": "UNREAD_EVENT_SENTINEL"},
    ]})
    refs = [chunk.ref for chunk in store.chunks("trace://sample", max_chars=40)]
    assert len(refs) == 2
    payload = _input(store, refs[:1])
    prompt = render_attribution_prompt(payload)
    assert "observed format failure" in prompt
    assert "UNREAD_EVENT_SENTINEL" not in prompt
    assert payload["unconsumed_chunk_refs"] == refs[1:]


def test_replacing_trace_refreshes_every_cached_chunk_size():
    store = TraceStore({"trace://sample": [
        {"event_id": "answer", "kind": "assistant_message", "text": "old response"},
    ]})
    before = {limit: store.chunks("trace://sample", max_chars=limit) for limit in (40, 1200)}
    other = store.chunks("trace://other", max_chars=40)
    store.add("trace://sample", [
        {"event_id": "answer", "kind": "assistant_message", "text": "new corrected response"},
    ])
    for limit, old in before.items():
        current = store.chunks("trace://sample", max_chars=limit)
        assert current[0].text == "answer new corrected response"
        assert current[0].sha256 != old[0].sha256
    assert store.chunks("trace://other", max_chars=40) is other
