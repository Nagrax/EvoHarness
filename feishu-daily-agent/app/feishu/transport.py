"""飞书网络适配器：只有这里可以接触真实 SDK/网络。

发送结果必须区分：
- sent       : 拿到真实 message_id
- retryable  : 明确失败且可安全重试（限流/5xx/连接建立失败）
- permanent  : 不可重试（鉴权、权限、参数）
- unknown    : 超时等“远端可能已成功”的模糊状态，只能对账，禁止盲重发
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.domain.intake import Page
from app.feishu.messages import IncomingMessage, from_history_item
from app.feishu.parser import clean_mention_placeholders, parse_message_content

logger = logging.getLogger(__name__)

IDEMPOTENCY_WINDOW_SECONDS = 3600


def chat_type_of(item: Any) -> str:
    """从 ListChat 对象/字典推导会话类型。

    真实 SDK 的 ListChat 没有 chat_type 属性（曾导致 AttributeError），
    p2p 会话带 p2p_target_id，群聊没有。
    """
    if isinstance(item, dict):
        declared = item.get("chat_type")
        if declared:
            return str(declared)
        target = item.get("p2p_target_id")
    else:
        declared = getattr(item, "chat_type", None)
        if declared:
            return str(declared)
        target = getattr(item, "p2p_target_id", None)
    return "p2p" if target else "group"


def receive_id_type_of(target: str) -> str:
    """按目标前缀推导 receive_id_type。

    实机发现（2026-11-07）：对 oc_ 群会话用 open_id 类型发送会被飞书拒绝
    （code=99992361 open_id cross app），日报此前只发给 owner 单聊所以未暴露。
    """
    value = (target or "").strip()
    if value.startswith("oc_"):
        return "chat_id"
    if value.startswith("om_"):
        return "message_id"
    return "open_id"


@dataclass
class TransportResult:
    outcome: str
    receipt_id: str = ""
    error: str = ""
    retry_after: float | None = None


class FeishuTransport:
    supports_idempotency = True

    def __init__(self) -> None:
        self._client = None
        self._bot_open_id: str | None = None

    def client(self):
        if self._client is None:
            import lark_oapi as lark

            settings = get_settings()
            self._client = (
                lark.Client.builder()
                .app_id(settings.feishu_app_id)
                .app_secret(settings.feishu_app_secret)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
        return self._client

    def bot_open_id(self) -> str:
        settings = get_settings()
        if settings.feishu_bot_open_id:
            return settings.feishu_bot_open_id
        if self._bot_open_id is not None:
            return self._bot_open_id
        try:
            from lark_oapi.core.enum import AccessTokenType, HttpMethod
            from lark_oapi.core.model import BaseRequest

            req = BaseRequest()
            req.http_method = HttpMethod.GET
            req.uri = "/open-apis/bot/v3/info"
            req.token_types = {AccessTokenType.TENANT}
            resp = self.client().request(req)
            body = json.loads(resp.content.decode("utf-8"))
            bot = body.get("bot") or body.get("data", {}).get("bot") or {}
            self._bot_open_id = bot.get("open_id") or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("获取机器人 open_id 失败: %s", exc)
            self._bot_open_id = ""
        return self._bot_open_id

    @staticmethod
    def _classify(resp: Any) -> TransportResult:
        if resp.success():
            receipt = getattr(resp.data, "message_id", "") or ""
            if not receipt:
                return TransportResult("unknown", error="success_without_receipt")
            return TransportResult("sent", receipt_id=receipt)
        code = int(getattr(resp, "code", 0) or 0)
        msg = f"code={code} msg={getattr(resp, 'msg', '')}"
        status = int(getattr(getattr(resp, "raw", None), "status_code", 0) or 0)
        if code in {99991400, 99991401} or status == 429:
            return TransportResult("retryable", error=msg, retry_after=5.0)
        if status >= 500:
            return TransportResult("retryable", error=msg, retry_after=2.0)
        return TransportResult("permanent", error=msg)

    def _create(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str,
        idempotency_key: str = "",
    ) -> TransportResult:
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            body = (
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
            )
            if idempotency_key:
                body.uuid(idempotency_key)
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(body.build())
                .build()
            )
            return self._classify(self.client().im.v1.message.create(req))
        except Exception as exc:  # noqa: BLE001
            return self._exception_result(exc)

    def _reply(
        self, message_id: str, msg_type: str, content: str, idempotency_key: str,
        reply_in_thread: bool = False,
    ) -> TransportResult:
        try:
            from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

            body = ReplyMessageRequestBody.builder().msg_type(msg_type).content(content)
            if idempotency_key:
                body.uuid(idempotency_key)
            if reply_in_thread:
                body.reply_in_thread(True)
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(body.build())
                .build()
            )
            return self._classify(self.client().im.v1.message.reply(req))
        except Exception as exc:  # noqa: BLE001
            return self._exception_result(exc)

    @staticmethod
    def _exception_result(exc: Exception) -> TransportResult:
        name = type(exc).__name__.lower()
        if "timeout" in name:
            return TransportResult("unknown", error=f"timeout:{exc}")
        if "connection" in name:
            return TransportResult("retryable", error=f"connection:{exc}", retry_after=2.0)
        return TransportResult("retryable", error=f"{type(exc).__name__}:{exc}", retry_after=5.0)

    def reply_text(
        self, message_id: str, text: str, idempotency_key: str = "", reply_in_thread: bool = False
    ) -> TransportResult:
        return self._reply(
            message_id,
            "text",
            json.dumps({"text": text}, ensure_ascii=False),
            idempotency_key,
            reply_in_thread,
        )

    def reply_card(
        self, message_id: str, card: dict, idempotency_key: str = "", reply_in_thread: bool = False
    ) -> TransportResult:
        return self._reply(
            message_id,
            "interactive",
            json.dumps(card, ensure_ascii=False),
            idempotency_key,
            reply_in_thread,
        )

    def send_text(self, receive_id: str, text: str, idempotency_key: str = "") -> TransportResult:
        return self._create(
            receive_id_type_of(receive_id), receive_id, "text",
            json.dumps({"text": text}, ensure_ascii=False), idempotency_key,
        )

    def send_card(self, receive_id: str, card: dict, idempotency_key: str = "") -> TransportResult:
        return self._create(
            receive_id_type_of(receive_id), receive_id, "interactive",
            json.dumps(card, ensure_ascii=False), idempotency_key,
        )

    def list_chats(self) -> list[dict]:
        from lark_oapi.api.im.v1 import ListChatRequest

        client = self.client()
        page_token: str | None = None
        chats: list[dict] = []
        while True:
            builder = ListChatRequest.builder().page_size(50)
            if page_token:
                builder.page_token(page_token)
            resp = client.im.v1.chat.list(builder.build())
            if not resp.success():
                raise RuntimeError(f"列举会话失败: code={resp.code} msg={resp.msg}")
            for item in resp.data.items or []:
                chats.append(
                    {
                        "chat_id": item.chat_id,
                        "chat_type": chat_type_of(item),
                        "chat_mode": getattr(item, "chat_mode", "") or "",
                        "name": item.name or "",
                    }
                )
            if not resp.data.has_more:
                break
            page_token = resp.data.page_token
        return chats

    def fetch_page(
        self, chat_id: str, thread_id: str, start_ms: int, end_ms: int, page_token: str
    ) -> Page:
        from lark_oapi.api.im.v1 import ListMessageRequest

        builder = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .start_time(str(start_ms // 1000))
            .end_time(str(end_ms // 1000))
            .page_size(50)
            .sort_type("ByCreateTimeAsc")
        )
        if page_token:
            builder.page_token(page_token)
        resp = self.client().im.v1.message.list(builder.build())
        if not resp.success():
            raise RuntimeError(f"拉取历史消息失败 chat={chat_id} code={resp.code} msg={resp.msg}")
        items: list[IncomingMessage] = []
        for item in resp.data.items or []:
            message = from_history_item(item)
            if thread_id and message.thread_id != thread_id:
                continue
            items.append(message)
        return Page(
            items=items,
            next_token=resp.data.page_token or "",
            has_more=bool(resp.data.has_more),
        )


_transport: FeishuTransport | None = None


def get_transport() -> FeishuTransport:
    global _transport
    if _transport is None:
        _transport = FeishuTransport()
    return _transport


def set_transport(value) -> None:
    global _transport
    _transport = value
