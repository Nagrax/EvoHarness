"""Owner 指令：改 / 验证 / 停用 / 驳回 / 完成任务。"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import session_scope
from app.core.ids import stable_id
from app.domain import outbox as outbox_module
from app.domain import review as review_module
from app.domain import tasks as tasks_module
from app.feishu.messages import IncomingMessage
from app.memory import experience as experience_module
from app.models import Message, TaskStatus

logger = logging.getLogger(__name__)

_EDIT = re.compile(r"^改\s*#?(\d+)\s+(.+)$", re.S)
_VERIFY = re.compile(r"^验证\s*#?(\d+)$")
_DISABLE = re.compile(r"^停用\s*#?(\d+)$")
_REJECT = re.compile(r"^驳回\s*#?(\d+)$")
_DONE = re.compile(r"^完成\s*#?(\d+)$")
_REOPEN = re.compile(r"^重开\s*#?(\d+)$")
_CORRECT = re.compile(r"^纠正\s*#?(\d+)\s+(.+)$", re.S)


def _enqueue_notice(session, text: str, *, reply_to: str = "", chat_id: str = "") -> None:
    """把指令回执投递给**发起指令的人**。

    历史缺陷：回执固定发给全局 owner，同事发「订阅」/「批准」后收不到任何回应，
    体验上像"发了没反应"。现在优先回到发指令的会话。
    """
    settings = get_settings()
    if reply_to:
        # 优先用 chat_id：p2p 与群聊都能直接定位，且不依赖 open_id 命名空间
        target_type = "chat_id" if (chat_id or "").startswith("oc_") else "open_id"
        target_id = chat_id if target_type == "chat_id" else reply_to
    else:
        target_type, target_id = settings.owner_target()
    if not target_id:
        logger.warning("指令回执无投递目标，只落库不发送")
        return
    intent = outbox_module.intent_key(
        settings.tenant_id, target_id, "notice",
        stable_id(text)[:24],
    )
    outbox_module.enqueue(
        session,
        intent=intent,
        tenant=settings.tenant_id,
        kind="notice",
        target_type=target_type,
        target_id=target_id,
        payload={"text": text},
        policy_version=settings.policy_version,
    )


def _send_reply(incoming: IncomingMessage, text: str) -> None:
    """直接回复到原会话（订阅类回执不走 outbox 审批链）。"""
    from app.services.reply_engine import dispatch_pending

    with session_scope() as session:
        message = session.scalar(
            select(Message).where(Message.message_id == incoming.message_id)
        )
        if message is not None:
            message.handled = True
        _enqueue_notice(
            session, text,
            reply_to=incoming.sender_id, chat_id=incoming.chat_id,
        )
    dispatch_pending(limit=5)


def _can_operate(session, draft, actor: str) -> bool:
    """谁能处理这条草稿：被提问者本人，或管理员兜底。"""
    settings = get_settings()
    approver = (getattr(draft, "approver_id", "") or "").strip()
    if approver and actor == approver:
        return True
    return actor in {
        (settings.feishu_owner_open_id or "").strip(),
        (settings.feishu_owner_chat_id or "").strip(),
    }


def try_handle_command(incoming: IncomingMessage) -> bool:
    settings = get_settings()
    actor = incoming.sender_id
    text = (incoming.text or "").strip()
    if not text:
        return False

    # 订阅/退订人人可用：这是产品入口，不该只服务管理员。
    from app.core.database import session_scope as _scope
    from app.services import subscription

    with _scope() as session:
        reply = subscription.handle_subscribe_command(
            session,
            text=text,
            open_id=actor,
            chat_id=incoming.chat_id,
            name=incoming.sender_name or "",
        )
    if reply:
        _send_reply(incoming, reply)
        return True

    # 其余指令（改/验证/停用/驳回/完成/重开/纠正）仍是管理动作。
    # 被提问者对**自己的**草稿也要能用，因此不再一刀切要求全局 owner，
    # 具体权限在各分支内按 _can_operate 判定。
    is_admin = actor in {
        (settings.feishu_owner_open_id or "").strip(),
        (settings.feishu_owner_chat_id or "").strip(),
    }
    reply = ""
    with session_scope() as session:
        match = _EDIT.match(text)
        if match:
            draft_id, content = int(match.group(1)), match.group(2).strip()
            from app.models import ReplyDraft

            draft = session.get(ReplyDraft, draft_id)
            if draft is not None and not _can_operate(session, draft, actor):
                reply = f"草稿 #{draft_id} 不属于你，无法修改"
            else:
                try:
                    review_module.approve_draft_direct(
                        session, draft_id, actor=actor, edited_content=content
                    )
                    reply = f"已按修改内容排队发送草稿 #{draft_id}"
                except review_module.ReviewError as exc:
                    reply = f"草稿 #{draft_id} 处理失败：{exc}"
        else:
            match = _VERIFY.match(text)
            if match:
                try:
                    experience = experience_module.publish(
                        session, int(match.group(1)), actor=incoming.sender_id, reason="owner 指令"
                    )
                    reply = f"经验 #{experience.id} v{experience.version} 已验证"
                except Exception as exc:  # noqa: BLE001
                    reply = f"经验 #{match.group(1)} 验证失败：{exc}"
            else:
                match = _DISABLE.match(text)
                if match:
                    try:
                        experience = experience_module.pause(
                            session, int(match.group(1)), actor=incoming.sender_id, reason="owner 指令"
                        )
                        reply = f"经验 #{experience.id} 已停用"
                    except Exception as exc:  # noqa: BLE001
                        reply = f"经验 #{match.group(1)} 停用失败：{exc}"
                else:
                    match = _REJECT.match(text)
                    if match:
                        from app.models import ReplyDraft

                        reject_id = int(match.group(1))
                        target = session.get(ReplyDraft, reject_id)
                        if target is not None and not _can_operate(session, target, actor):
                            reply = f"草稿 #{reject_id} 不属于你，无法驳回"
                        else:
                            try:
                                review_module.reject_draft_direct(
                                    session, reject_id, actor=actor, reason="owner 指令"
                                )
                                reply = f"草稿 #{reject_id} 已驳回"
                            except Exception as exc:  # noqa: BLE001
                                reply = f"草稿 #{reject_id} 驳回失败：{exc}"
                    else:
                        match = _CORRECT.match(text)
                        if match:
                            draft_id, content = int(match.group(1)), match.group(2).strip()
                            from app.models import DraftStatus, ReplyDraft
                            draft = session.get(ReplyDraft, draft_id)
                            if draft is None or draft.status != DraftStatus.SENT:
                                reply = f"草稿 #{draft_id} 不是已发送状态，无法纠正"
                            elif not _can_operate(session, draft, actor):
                                reply = f"草稿 #{draft_id} 不属于你，无法纠正"
                            else:
                                had_previous = draft.experience_id is not None
                                candidate = experience_module.correct_sent_reply(
                                    session, draft_id=draft_id, corrected_answer=content,
                                    actor=actor, reason="指令纠正",
                                )
                                if candidate is None:
                                    reply = "纠正失败"
                                else:
                                    reply = f"已生成经验候选 #{candidate.id}"
                                    if had_previous:
                                        reply += "，旧经验已暂停"
                        else:
                            match = _DONE.match(text)
                            if match:
                                try:
                                    task = tasks_module.set_status(
                                        session, int(match.group(1)),
                                        status=TaskStatus.DONE, actor=incoming.sender_id,
                                        detail="owner 指令", evidence_message_ids=[incoming.message_id],
                                    )
                                    reply = f"任务 #{match.group(1)} 已标记完成" if task else "任务不存在"
                                except Exception as exc:  # noqa: BLE001
                                    reply = f"任务 #{match.group(1)} 操作失败：{exc}"
                            else:
                                match = _REOPEN.match(text)
                                if match:
                                    task = tasks_module.set_status(
                                        session, int(match.group(1)),
                                        status=TaskStatus.OPEN, actor=incoming.sender_id,
                                        detail="owner 指令重开",
                                    )
                                    reply = f"任务 #{match.group(1)} 已重开" if task else "任务不存在"
                                else:
                                    return False
        if reply:
            message = session.scalar(
                select(Message).where(Message.message_id == incoming.message_id)
            )
            if message is not None:
                message.handled = True
            # 回执回到发指令的会话，而不是固定发给全局 owner
            _enqueue_notice(
                session, reply,
                reply_to=incoming.sender_id, chat_id=incoming.chat_id,
            )
    if reply:
        from app.services.reply_engine import dispatch_pending

        dispatch_pending(limit=5)
    return True
