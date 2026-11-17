"""飞书消息内容解析：把各类消息体转成纯文本。"""
import json
import re
from typing import Any

_TYPE_LABELS = {
    "image": "[图片]",
    "file": "[文件]",
    "audio": "[语音]",
    "media": "[视频]",
    "sticker": "[表情]",
    "interactive": "[卡片]",
    "share_chat": "[分享群名片]",
    "share_user": "[分享用户]",
    "system": "[系统消息]",
}

# 渠道入口归一（DE-22）：文档/纪要导入类消息体的可读文本归一；
# 既有 msg_type 的解析行为保持不变。
_CHANNEL_ENTRY_TYPES = ("document_import", "minutes_import")


def parse_channel_entry(msg_type: str, raw_content: str) -> dict:
    """把文档/纪要导入类消息体归一为 {kind, title, text, ref, ...}。

    只服务于 DE-22 渠道入口；返回 dict 供上层路由到
    evoharness.channels.documents / meeting_notes，解析仍受限（纯文本优先）。
    非 channel 类型返回空 dict，不改变既有 parse_message_content 行为。
    """
    data = _safe_loads(raw_content)
    if msg_type == "document_import":
        return {"kind": "document", "title": str(data.get("title") or ""),
                "text": str(data.get("text") or ""),
                "ref": str(data.get("doc_id") or ""),
                "version": data.get("version"),
                "attachments": data.get("attachments") or []}
    if msg_type == "minutes_import":
        return {"kind": "minutes", "title": str(data.get("title") or ""),
                "text": str(data.get("text") or ""),
                "ref": str(data.get("minutes_id") or ""),
                "meeting_ref": str(data.get("meeting_ref") or ""),
                "authorized_attendees": data.get("authorized_attendees") or []}
    return {}


def _safe_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def parse_message_content(msg_type: str, raw_content: str) -> str:
    """提取消息可读文本。"""
    data = _safe_loads(raw_content)

    if msg_type == "text":
        return str(data.get("text", "")).strip()

    if msg_type == "post":
        # 结构: {"zh_cn": {"title": ..., "content": [[node, ...], ...]}}
        body = next(iter(data.values()), {}) if isinstance(data, dict) else {}
        parts: list[str] = []
        if isinstance(body, dict):
            if body.get("title"):
                parts.append(str(body["title"]))
            for paragraph in body.get("content", []) or []:
                for node in paragraph or []:
                    tag = node.get("tag")
                    if tag in ("text", "a"):
                        parts.append(node.get("text", ""))
                    elif tag == "at":
                        parts.append("@" + node.get("user_name", ""))
        return " ".join(p.strip() for p in parts if p and p.strip())

    if msg_type in _CHANNEL_ENTRY_TYPES:
        entry = parse_channel_entry(msg_type, raw_content)
        return entry.get("text") or entry.get("title") or ""

    return _TYPE_LABELS.get(msg_type, f"[{msg_type}]")


def clean_mention_placeholders(text: str) -> str:
    """移除群消息中的 @_user_N 占位符。"""
    return re.sub(r"@_user_\d+", "", text).strip()
