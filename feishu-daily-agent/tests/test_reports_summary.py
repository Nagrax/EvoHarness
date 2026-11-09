"""日报摘要：待批与影子必须分开计数，影子不是 owner 的待办。"""
from __future__ import annotations

from datetime import date

from app.core.clock import utcnow
from app.core.database import session_scope
from app.domain import reports
from app.models import DraftStatus, ReplyDraft


def test_report_summary_splits_pending_and_shadow(source):
    with session_scope() as session:
        session.add_all([
            ReplyDraft(
                tenant="default", message_id="om_split_draft", chat_id=source.chat_id,
                draft_content="正常回复", content_hash="h1", status=DraftStatus.PENDING,
                mode="draft", shadow=False, created_at=utcnow(),
            ),
            ReplyDraft(
                tenant="default", message_id="om_split_shadow", chat_id=source.chat_id,
                draft_content="影子回复", content_hash="h2", status=DraftStatus.PENDING,
                mode="shadow", shadow=True, created_at=utcnow(),
            ),
        ])
        session.flush()

    with session_scope() as session:
        content, _ = reports.build_content(
            session,
            tenant="default",
            owner="ou_owner",
            target_date=date.today(),
            timezone="Asia/Shanghai",
        )

    assert content["summary"]["pending"] == 1
    assert content["summary"]["shadow_pending"] == 1
