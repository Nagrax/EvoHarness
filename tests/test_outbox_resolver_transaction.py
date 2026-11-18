"""Token rotation runs without an outbox writer lock and authority is rechecked."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest

from test_send_identity_freeze import POLICY, Transport
from evoharness import outbox
from evoharness.models import Base, Outbox, SystemState


@pytest.mark.parametrize('revoke', [False, True])
def test_resolver_commits_before_dispatch_and_cannot_send_after_revoke(tmp_path, revoke):
    engine = create_engine('sqlite:///' + (tmp_path / 'outbox.db').as_posix(),
                           connect_args={'timeout': 0.1})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    transport = Transport()
    with factory.begin() as session:
        row = outbox.enqueue(session, intent='test-rotation', tenant='tenant-a', kind='reply',
            target_type='message', target_id='message-a', chat_id='chat-a',
            payload={'text': 'test', 'twin_open_id': 'alice',
                     '_delivery_scope': {'tenant': 'attacker'}})
        row_id = row.id

    def resolver(sessions, payload):
        assert payload['_delivery_scope'] == {'tenant': 'tenant-a'}
        with sessions.begin() as session:
            session.add(SystemState(key='rotation', value='revoked' if revoke else 'refreshed'))
        return 'rotated-token'

    def precheck(session, row):
        state = session.get(SystemState, 'rotation')
        return (False, 'source_revoked') if state and state.value == 'revoked' else (True, '')

    results = outbox.dispatch_outbox(factory, transport=transport, outbox_id=row_id,
        limit=1, policy=POLICY, precheck=precheck, user_token_resolver=resolver)
    with factory() as session:
        assert session.get(SystemState, 'rotation') is not None
        row = session.get(Outbox, row_id)
        if revoke:
            assert row.status == 'cancelled' and not transport.calls
        else:
            assert row.status == 'sent' and results[0].receipt_id
            assert transport.calls[0]['user_access_token'] == 'rotated-token'
    engine.dispose()
