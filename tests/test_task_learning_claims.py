"""A stale learner cannot overwrite a reclaimed or expired candidate lease."""

import json
from datetime import timedelta

import pytest

from test_execution_knowledge import env
from test_task_learning_loop import extractor, feedback
from evoharness.clock import utcnow
from evoharness.knowledge import task_learning
from evoharness.work.models import SkillLearningEvidence


@pytest.mark.parametrize("takeover", ["reclaimed", "completed", "expired"])
def test_candidate_completion_rechecks_claim_in_write_transaction(
    env, tmp_path, monkeypatch, takeover
):
    app, api, task_id, _, _ = env
    path, evidence_id = feedback(env, tmp_path, monkeypatch)
    before = path.read_bytes()
    seen = []
    original_context = task_learning._context
    post_model_checks = 0
    winner = {"ok": True, "proposal": {"instructions": "new claimant's result"}}

    def context(session, *args, **kwargs):
        nonlocal post_model_checks
        if len(seen) == 2:
            post_model_checks += 1
            if post_model_checks == 2:
                # Interleave after the final read-only check has committed,
                # before the old learner opens its result-writing transaction.
                with app.transaction() as competing:
                    row = competing.get(SkillLearningEvidence, evidence_id)
                    if takeover == "expired":
                        row.lease_until = utcnow() - timedelta(seconds=1)
                    else:
                        row.claim_token = "new-claimant"
                        row.lease_until = utcnow() + timedelta(minutes=5)
                        if takeover == "completed":
                            row.state = "proposed"
                            row.result_json = json.dumps(winner)
        return original_context(session, *args, **kwargs)

    monkeypatch.setattr(task_learning, "_context", context)
    result = api.learning_action(
        task_id,
        evidence_id=evidence_id,
        action="build",
        actor="founder",
        _side_query=extractor(seen),
    )
    assert post_model_checks == 2
    assert not result["ok"] and result["reason_code"] == "learning_claim_lost", result
    assert path.read_bytes() == before
    with app.transaction() as session:
        row = session.get(SkillLearningEvidence, evidence_id)
        if takeover == "completed":
            assert row.state == "proposed"
            assert json.loads(row.result_json) == winner
            assert row.claim_token == "new-claimant"
        elif takeover == "reclaimed":
            assert row.state == "creating"
            assert row.claim_token == "new-claimant"
            assert json.loads(row.result_json or "{}") == {}
        else:
            assert row.state == "failed"
            assert row.claim_token == ""
            assert row.lease_until is None
            assert json.loads(row.result_json or "{}") == {}
