"""卡片回调：只信任事件里的 operator，卡片 value 只提供一次性 action_id。"""
from __future__ import annotations

import logging

from app.core.database import session_scope
from app.domain import review as review_module

logger = logging.getLogger(__name__)


def _result_card(content: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "批示结果"},
            "template": "green",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}}
        ],
    }


def handle_card_action_payload(
    action_id: str,
    operator_id: str,
    *,
    edited_content: str | None = None,
    reason: str = "",
) -> dict:
    if not action_id or not operator_id:
        return {"ok": False, "toast": {"type": "error", "content": "缺少动作或操作者身份"}}
    try:
        with session_scope() as session:
            outcome = review_module.handle_card_action(
                session,
                action_id=action_id,
                operator_id=operator_id,
                edited_content=edited_content,
                reason=reason,
            )
    except review_module.ReviewError as exc:
        return {"ok": False, "toast": {"type": "error", "content": f"操作被拒绝：{exc}"}}
    except Exception as exc:  # noqa: BLE001
        logger.exception("卡片回调处理失败")
        return {"ok": False, "toast": {"type": "error", "content": f"操作失败：{exc}"}}
    if outcome.outbox_id:
        from app.services.reply_engine import dispatch_pending

        dispatch_pending(limit=5)
    labels = {
        "queued": "已批准并排队发送",
        "shadow_recorded": "影子模式：只记录，不发送",
        "rejected": "已驳回",
        "published": "经验已发布",
        "paused": "已暂停",
        "rolled_back": "已回滚",
        "candidate_created": "已生成经验候选",
    }
    return {
        "ok": True,
        "outcome": outcome,
        "toast": {"type": "success", "content": labels.get(outcome.status, outcome.status)},
    }


def handle_card_action(data):
    action = data.event.action
    value = action.value or {}
    action_id = str(value.get("action_id") or "")
    operator = getattr(getattr(data.event, "operator", None), "open_id", "")
    if not operator:
        operator = getattr(getattr(getattr(data.event, "operator", None), "operator_id", None), "open_id", "")
    form = getattr(action, "form_value", None) or {}
    edited_content = None
    if isinstance(form, dict):
        edited_content = form.get("content") or form.get("corrected_answer")
    result = handle_card_action_payload(
        action_id, operator, edited_content=edited_content
    )
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )

    response = {"toast": result["toast"]}
    if result.get("ok") and result.get("outcome"):
        response["card"] = {
            "type": "raw",
            "data": _result_card(result["toast"]["content"]),
        }
    return P2CardActionTriggerResponse(response)
