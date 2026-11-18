"""A user-identity retry uses the same platform idempotency key as its outbox."""
from app.feishu.transport import FeishuTransport


def test_user_send_transmits_idempotency_and_does_not_reuse_it(monkeypatch):
    import requests
    payloads = []
    class Response:
        def json(self):
            return {'code': 0, 'data': {'message_id': 'om_test'}}
    def post(url, **kwargs):
        payloads.append(kwargs['json'])
        return Response()
    monkeypatch.setattr(requests, 'post', post)
    transport = FeishuTransport()
    for key in ['outbox-stable-key', 'outbox-stable-key', '']:
        result = transport.reply_text('om_original', 'test', key,
            user_access_token='fake-token', user_chat_id='oc_test', send_identity='user')
        assert result.outcome == 'sent'
    assert payloads[0]['uuid'] == payloads[1]['uuid'] == 'outbox-stable-key'
    assert 'uuid' not in payloads[2]
