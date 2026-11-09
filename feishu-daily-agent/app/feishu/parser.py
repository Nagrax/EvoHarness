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

    return _TYPE_LABELS.get(msg_type, f"[{msg_type}]")


def clean_mention_placeholders(text: str) -> str:
    """移除群消息中的 @_user_N 占位符。"""
    return re.sub(r"@_user_\d+", "", text).strip()
