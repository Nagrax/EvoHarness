"""待批草稿 TTL：超过 DRAFT_TTL_HOURS 的 pending 必须过期，避免无限堆积。"""
from __future__ import annotations

from datetime import timedelta

from app.core.clock import utcnow
from app.core.database import session_scope
from app.domain import review as review_module
from app.models import DraftStatus, ReplyDraft
from app.services import reply_engine
from tests.helpers import incoming


def test_expire_stale_pending_drafts(source):
    with session_scope() as session:
        old = reply_engine.process_incoming(
            session, incoming("请问年假怎么申请？", "om_ttl_old"), source=source
        )
        old.created_at = utcnow() - timedelta(hours=80)
        fresh = reply_engine.process_incoming(
            session, incoming("会议室怎么预订？", "om_ttl_new"), source=source
        )
        old_id, fresh_id = old.id, fresh.id

    with session_scope() as session:
        expired = review_module.expire_stale_drafts(session)

    assert expired >= 1
    with session_scope() as session:
        assert session.get(ReplyDraft, old_id).status == DraftStatus.SUPERSEDED
        assert "ttl_expired" in (session.get(ReplyDraft, old_id).note or "")
        assert session.get(ReplyDraft, fresh_id).status == DraftStatus.PENDING
