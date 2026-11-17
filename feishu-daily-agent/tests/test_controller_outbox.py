"""Control notifications share the canonical durable queue and receipt semantics."""
import os
from pathlib import Path
import sys

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from integration import feishu_notify
from app.core import database
from app.domain import outbox
from app.feishu.transport import TransportResult
from app.models import Outbox
from tests.helpers import FakeTransport


@pytest.fixture
def notifier(settings, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "single_approval")
    values = {"FEISHU_APP_ID": settings.feishu_app_id, "FEISHU_APP_SECRET": "offline-test-secret",
              "FEISHU_OWNER_CHAT_ID": "oc_owner", "DATABASE_URL": os.environ["DATABASE_URL"]}
    monkeypatch.setattr(feishu_notify, "load_feishu_env", lambda: values)
    monkeypatch.setattr(feishu_notify, "feishu_live", lambda env: True)
    return values


def test_control_send_claims_only_its_own_intent_and_keeps_full_receipt(notifier, monkeypatch):
    transport = FakeTransport(results=[TransportResult("sent", receipt_id="om_complete_receipt_123456")])
    monkeypatch.setattr(outbox, "get_transport", lambda: transport)
    with database.session_scope() as session:
        unrelated = outbox.enqueue(session, intent="unrelated", tenant="default", kind="notice",
                                   target_type="chat_id", target_id="oc_unrelated", payload={"text": "do not send"})
        unrelated_id = unrelated.id
    result = feishu_notify.send_text("controller report", title="report", mode="live")
    assert result["delivered"] is True
    assert result["receipt"] == "om_complete_receipt_123456"
    assert transport.calls == 1
    assert transport.sent_text[0][0] == "oc_owner"
    with database.session_scope() as session:
        assert session.get(Outbox, unrelated_id).status == "queued"
    repeated = feishu_notify.send_text("controller report", title="report", mode="live")
    assert repeated["outboxId"] == result["outboxId"]
    assert transport.calls == 1


@pytest.mark.parametrize("remote", [TransportResult("unknown", error="timeout"), TransportResult("sent", receipt_id=""), TransportResult("retryable", error="429", retry_after=3600)])
def test_ambiguous_and_rate_limited_results_never_become_sent_or_duplicate(notifier, monkeypatch, remote):
    transport = FakeTransport(results=[remote])
    monkeypatch.setattr(outbox, "get_transport", lambda: transport)
    first = feishu_notify.send_text("stuck task", title="pm-nudge", mode="live")
    second = feishu_notify.send_text("stuck task", title="pm-nudge", mode="live")
    assert first["delivered"] is False and first["ok"] is False
    assert first["status"] in {"delivery_unknown", "retry_wait"}
    assert first["receipt"] == ""
    assert first["outboxId"] == second["outboxId"]
    assert transport.calls == 1


def test_mismatched_business_database_is_rejected_before_enqueue(notifier, monkeypatch, tmp_path):
    monkeypatch.setitem(notifier, "DATABASE_URL", "sqlite:///" + str(tmp_path / "another.db"))
    result = feishu_notify.send_text("must not enqueue", mode="live")
    assert result["status"] == "waiting_configuration"
    with database.session_scope() as session:
        assert session.scalar(select(Outbox.id)) is None
    assert not (tmp_path / "another.db").exists()
