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


def _enqueue_notice(session, text: str) -> None:
    settings = get_settings()
    intent = outbox_module.intent_key(
        settings.tenant_id, settings.feishu_owner_open_id, "notice",
        stable_id(text)[:24],
    )
    outbox_module.enqueue(
        session,
        intent=intent,
        tenant=settings.tenant_id,
        kind="notice",
        target_type="open_id",
        target_id=settings.feishu_owner_open_id,
        payload={"text": text},
        policy_version=settings.policy_version,
    )


def try_handle_command(incoming: IncomingMessage) -> bool:
    settings = get_settings()
    if incoming.sender_id != settings.feishu_owner_open_id:
        return False
    text = (incoming.text or "").strip()
    if not text:
        return False
    reply = ""
    with session_scope() as session:
        match = _EDIT.match(text)
        if match:
            draft_id, content = int(match.group(1)), match.group(2).strip()
            try:
                review_module.approve_draft_direct(
                    session, draft_id, actor=incoming.sender_id, edited_content=content
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
                        try:
                            review_module.reject_draft_direct(
                                session, int(match.group(1)), actor=incoming.sender_id, reason="owner 指令"
                            )
                            reply = f"草稿 #{match.group(1)} 已驳回"
                        except Exception as exc:  # noqa: BLE001
                            reply = f"草稿 #{match.group(1)} 驳回失败：{exc}"
                    else:
                        match = _CORRECT.match(text)
                        if match:
                            draft_id, content = int(match.group(1)), match.group(2).strip()
                            from app.models import DraftStatus, ReplyDraft
                            draft = session.get(ReplyDraft, draft_id)
                            if draft is None or draft.status != DraftStatus.SENT:
                                reply = f"草稿 #{draft_id} 不是已发送状态，无法纠正"
                            else:
                                had_previous = draft.experience_id is not None
                                candidate = experience_module.correct_sent_reply(
                                    session, draft_id=draft_id, corrected_answer=content,
                                    actor=incoming.sender_id, reason="owner 指令纠正",
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
            _enqueue_notice(session, reply)
    if reply:
        from app.services.reply_engine import dispatch_pending

        dispatch_pending(limit=5)
    return True
