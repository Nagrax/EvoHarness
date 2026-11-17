"""端到端验证：数字分身以本人身份发出回复。

流程与生产完全一致：草稿 → 批准 → outbox → transport（带 user token）→ 真实回执。
唯一差别是回复文本标明这是自测，避免误认。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.database import session_scope  # noqa: E402
from app.domain import admission, outbox as outbox_module, review  # noqa: E402
from app.services import reply_engine  # noqa: E402
from app.services.reply_engine import process_incoming  # noqa: E402
from tests.helpers import incoming  # noqa: E402


def main() -> int:
    settings = get_settings()
    chat = settings.feishu_owner_chat_id
    owner = settings.feishu_owner_open_id

    # 单批准模式：草稿必须人工批准，与生产一致
    object.__setattr__(settings, "deployment_mode", "single_approval")
    object.__setattr__(settings, "shadow_mode", False)
    object.__setattr__(settings, "auto_reply_enabled", False)

    stamp = int(time.time())
    with session_scope() as session:
        source = admission.ensure_source(
            session,
            tenant=settings.tenant_id,
            owner=owner,
            chat_id=chat,
            source_type="p2p",
            reply_allowed=True,
        )
        reply_engine.upsert_chat(session, chat, "p2p", "数字分身自测")
        draft = process_incoming(
            session,
            incoming(
                # 用与经验库**完全无关**的问句：命中低分旧经验会被 policy 降级成
                # shadow（below_draft_threshold），那条路径与"分身身份"无关，
                # 会把要验证的东西掩盖掉。这里只在"无命中"分支下验证发送身份。
                f"分身连通性检查 {stamp}：今天适合出门吗",
                f"om_twin_selftest_{stamp}",
                chat_id=chat,
                sender=owner,
            ),
            source=source,
        )
        if draft is None:
            print("未生成草稿")
            return 2
        draft_id = draft.id
        print(f"草稿 #{draft_id} status={draft.status} approver={draft.approver_id[-8:]}")

    with session_scope() as session:
        outcome = review.approve_draft_direct(session, draft_id, actor=owner)
        print(f"批准: status={outcome.status} outbox={outcome.outbox_id}")

    from app.core.database import SessionLocal

    results = outbox_module.dispatch_outbox(SessionLocal, limit=3)
    for item in results:
        print(f"投递: id={item.outbox_id} status={item.status} "
              f"receipt={item.receipt_id[-8:] or '-'} err={item.error[:80]}")

    with session_scope() as session:
        from app.models import Outbox, ReplyDraft

        row = session.query(Outbox).filter(Outbox.kind == "reply").order_by(
            Outbox.id.desc()
        ).first()
        draft = session.get(ReplyDraft, draft_id)
        print(f"草稿最终状态: {draft.status} 回执={draft.sent_message_id[-8:] if draft.sent_message_id else '-'}")
        print(f"outbox: {row.status if row else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
