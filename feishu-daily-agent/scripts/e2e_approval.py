"""联调：把「审批 → 发送 → 经验沉淀」拆成两段分别取证。

为什么要拆：
- 卡片回调要 1.0.1 过审才生效，但「批准草稿 → 入队 → 发送 → 沉淀候选」是
  与回调通道无关的产品逻辑，可以直接驱动；
- 回复必须挂在一个**真实存在**的飞书消息上才会拿到回执，而真实消息
  （日报卡片）不是 question，走不进拟稿分支。

因此：
① 审批/沉淀段：用合成触发消息，验证审核逻辑与候选沉淀（不发送）；
② 发送段：用真实 message_id 调用 transport，验证回执与幂等键。
两段都通过，才说明"人批了之后能发出去、并且沉淀成经验"这条链是通的。
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from app.core.config import get_settings  # noqa: E402
from app.core.database import session_scope  # noqa: E402
from app.domain import review as review_module  # noqa: E402
from app.feishu.transport import get_transport  # noqa: E402
from app.models import Experience, ReplyDraft  # noqa: E402
from app.services.reply_engine import process_incoming  # noqa: E402
from tests.helpers import incoming  # noqa: E402


def main() -> int:
    settings = get_settings()
    chat = settings.feishu_owner_chat_id
    real_message = sys.argv[1]
    owner = settings.feishu_owner_open_id or "ou_selfcheck"
    trigger_id = f"om_selfcheck_{int(__import__('time').time())}"

    # 审批模式：草稿必须人工批准才发（不是自动回复）
    object.__setattr__(settings, "deployment_mode", "single_approval")
    object.__setattr__(settings, "shadow_mode", False)
    object.__setattr__(settings, "auto_reply_enabled", False)

    # ① 拟稿 → 批准 → 沉淀候选
    with session_scope() as session:
        draft = process_incoming(
            session,
            incoming("自检：审批链路是否打通？", trigger_id, chat_id=chat, sender=owner),
        )
        if draft is None:
            print("FAIL 未生成草稿")
            return 2
        draft_id = draft.id
        print(f"① 草稿 #{draft_id} status={draft.status} mode={draft.mode}")

    with session_scope() as session:
        outcome = review_module.approve_draft_direct(session, draft_id, actor=owner)
        print(f"① 批准: status={outcome.status} outbox_id={outcome.outbox_id}")

    with session_scope() as session:
        draft = session.get(ReplyDraft, draft_id)
        candidates = (
            session.query(Experience)
            .filter(Experience.status == "candidate")
            .order_by(Experience.id.desc())
            .all()
        )
        print(f"① 草稿状态={draft.status}（trigger={trigger_id[-6:]} 不存在于飞书，故本条不入队发送）")
        print(
            "① 经验候选: "
            + str([(c.id, str(c.status), c.canonical_answer[:24]) for c in candidates])
        )
        ok_sediment = bool(candidates)

    # ② 回复发送：用真实 message_id 拿真实回执
    transport = get_transport()
    receipt = transport.reply_text(
        real_message,
        "自检回执：EvoHarness 回复通道可用（可忽略本条）。",
        idempotency_key=f"selfcheck-{int(__import__('time').time())}",
    )
    print(f"② 发送: outcome={receipt.outcome} receipt={receipt.receipt_id[-8:] or '-'} err={receipt.error[:100]}")
    ok_send = receipt.outcome == "sent" and bool(receipt.receipt_id)

    print(f"\n结论: 审批/沉淀={'通' if ok_sediment else '断'}  发送={'通' if ok_send else '断'}")
    return 0 if (ok_sediment and ok_send) else 1


if __name__ == "__main__":
    raise SystemExit(main())
