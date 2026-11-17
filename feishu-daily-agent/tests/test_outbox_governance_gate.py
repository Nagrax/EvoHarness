"""DE-41：发送前治理重验接线（feishu outbox 适配器 _precheck）。

撤权/暂停及时传到发送层：决定被撤回或所在组织已暂停的发送意图在发送前被拦截；
无治理绑定的意图沿用既有口径；delivery_unknown 等真实回执状态不被改写。
"""
from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from evoharness.governance import authorization, decisions
from evoharness.models import OutboxStatus
from evoharness.organization import service as org
from evoharness.organization.service import AuthenticatedPrincipal
from evoharness.outbox import enqueue

from app.core.config import get_settings
from app.core.database import SessionLocal, session_scope
from app.domain import outbox as outbox_module
from app.feishu.transport import TransportResult
from app.models import Outbox as AppOutbox
from tests.helpers import FakeTransport

OWNER = AuthenticatedPrincipal(principal_id="founder", tenant_id="tn", app_id="app",
                               authenticated=True,
                               authorities=frozenset({"organization.bootstrap",
                                                      "organization.manage"}))


@pytest.fixture(autouse=True)
def _writable_mode(monkeypatch):
    """测试默认 offline（writes_enabled=False）；治理重验在核心检查之后叠加，
    这里切到 single_approval 使核心检查放行，单独观察治理判定。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "deployment_mode", "single_approval")


def _bootstrap(session) -> str:
    org_id = f"org-{uuid4().hex[:10]}"
    org.bootstrap_organization(session, OWNER, org_id=org_id, title="治理接线",
                               membership_id=f"m-{org_id}")
    return org_id


def test_precheck_passes_through_without_governance_binding():
    with session_scope() as session:
        row = enqueue(session, intent=f"gov-free-{uuid4().hex[:8]}", tenant="default",
                      kind="notice", target_type="chat_id", target_id="oc_unbound",
                      payload={"text": "无治理绑定的通知"})
        ok, reason = outbox_module._precheck(session, row)
        assert (ok, reason) == (True, "")


def test_precheck_blocks_revoked_decision_on_inflight_send():
    org_id = ""
    intent = f"gov-revoke-{uuid4().hex[:8]}"
    target = f"d-{uuid4().hex[:8]}"
    with session_scope() as session:
        org_id = _bootstrap(session)
        authorization.approve(session, "founder", org_id=org_id, target_type="draft",
                              target_id=target, target_revision=1, payload_hash="h1",
                              actions={"send_as_bot"})
        row = enqueue(session, intent=intent, tenant="default", kind="reply",
                      target_type="message", target_id=target,
                      payload={"body": "答案", "chat": "c1"})
        # 已领取在途（SENDING）：撤权取消不到它，由发送前重验拦截
        row.status = OutboxStatus.SENDING
        session.flush()
    with session_scope() as session:
        cancelled = authorization.revoke(session, "founder", target_type="draft",
                                         target_id=target, org_id=org_id)
        assert cancelled == []
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        assert row.status == OutboxStatus.SENDING
        ok, reason = outbox_module._precheck(session, row)
        assert (ok, reason) == (False, "decision_revoked")


def test_precheck_blocks_paused_organization():
    org_id = ""
    intent = f"gov-pause-{uuid4().hex[:8]}"
    target = f"d-{uuid4().hex[:8]}"
    with session_scope() as session:
        org_id = _bootstrap(session)
        authorization.approve(session, "founder", org_id=org_id, target_type="draft",
                              target_id=target, target_revision=1, payload_hash="h1",
                              actions={"send_as_bot"})
        enqueue(session, intent=intent, tenant="default", kind="reply",
                target_type="message", target_id=target,
                payload={"body": "答案", "chat": "c1"})
    with session_scope() as session:
        decisions.pause_scope(session, OWNER, org_id=org_id, kind="organization",
                              target_id=org_id, reason="紧急暂停")
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        ok, reason = outbox_module._precheck(session, row)
        assert (ok, reason) == (False, "organization_paused")


def test_unknown_receipt_not_rewritten_by_governance():
    org_id = ""
    intent = f"gov-unknown-{uuid4().hex[:8]}"
    target = f"d-{uuid4().hex[:8]}"
    with session_scope() as session:
        org_id = _bootstrap(session)
        authorization.approve(session, "founder", org_id=org_id, target_type="draft",
                              target_id=target, target_revision=1, payload_hash="h1",
                              actions={"send_as_bot"})
        row = enqueue(session, intent=intent, tenant="default", kind="reply",
                      target_type="message", target_id=target,
                      payload={"body": "答案", "chat": "c1"})
        row.status = OutboxStatus.DELIVERY_UNKNOWN
        session.flush()
    with session_scope() as session:
        authorization.revoke(session, "founder", target_type="draft",
                             target_id=target, org_id=org_id)
        decisions.pause_scope(session, OWNER, org_id=org_id, kind="organization",
                              target_id=org_id, reason="紧急暂停")
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        # 未知回执保留真实状态：治理重验不改写为成功，也不拦截改状态
        ok, reason = outbox_module._precheck(session, row)
        assert (ok, reason) == (True, "")
        session.refresh(row)
        assert row.status == OutboxStatus.DELIVERY_UNKNOWN


def test_dispatch_cancels_governance_blocked_intent(monkeypatch):
    org_id = ""
    intent = f"gov-dispatch-{uuid4().hex[:8]}"
    target = f"d-{uuid4().hex[:8]}"
    with session_scope() as session:
        org_id = _bootstrap(session)
        authorization.approve(session, "founder", org_id=org_id, target_type="draft",
                              target_id=target, target_revision=1, payload_hash="h1",
                              actions={"send_as_bot"})
        enqueue(session, intent=intent, tenant="default", kind="reply",
                target_type="message", target_id=target,
                payload={"body": "答案", "chat": "c1"})
    # 暂停组织不直接改 outbox 行（真实状态保留），由发送前治理重验拦截
    with session_scope() as session:
        decisions.pause_scope(session, OWNER, org_id=org_id, kind="organization",
                              target_id=org_id, reason="紧急暂停")
    transport = FakeTransport(results=[TransportResult("sent", receipt_id="om_should_not_send")])
    monkeypatch.setattr(outbox_module, "get_transport", lambda: transport)
    results = outbox_module.dispatch_outbox(SessionLocal, limit=5)
    blocked = [r for r in results if r.error == "organization_paused"]
    assert blocked and blocked[0].status == OutboxStatus.CANCELLED
    assert transport.calls == 0
    with session_scope() as session:
        row = session.query(AppOutbox).filter(AppOutbox.intent_key == intent).one()
        assert row.status == OutboxStatus.CANCELLED
        assert "organization_paused" in row.last_error
