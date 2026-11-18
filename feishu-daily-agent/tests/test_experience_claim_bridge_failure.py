"""A failed secondary projection must leave the authoritative pause committed."""
import logging

from app.core.database import session_scope
from app.memory import experience
from app.models import Experience


def test_claim_bridge_failure_logs_and_preserves_pause(tmp_path, monkeypatch, caplog):
    from evoharness.knowledge import experience_bridge

    def unavailable(*args, **kwargs):
        raise RuntimeError('synthetic claim projection unavailable')

    monkeypatch.setattr(experience_bridge, 'sync_experience', unavailable)
    monkeypatch.setenv('EVOHARNESS_SKILLS_ROOT', str(tmp_path/'skills'))
    with caplog.at_level(logging.WARNING):
        with session_scope() as session:
            item = experience.create_candidate(session, question='年假规则', answer='Synthetic answer')
            item_id = item.id
            experience.pause(session, item_id, actor='ou_owner')
    with session_scope() as session:
        assert session.get(Experience, item_id).status == 'paused'
    assert any('knowledge claim bridge failed' in record.message and
               'synthetic claim projection unavailable' in record.message
               for record in caplog.records)
