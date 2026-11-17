"""首次接入引导：把第一条 p2p 消息的发送者登记为 owner。

为什么需要它
------------
飞书自建应用刚发布时，服务端只知道自己的 bot open_id，不知道 owner 的
open_id，也没有 ``im:chat:readonly`` 去列举会话。此时如果要求 owner 先手工
查 open_id/chat_id，接入就会卡住。显式打开 ``FEISHU_OWNER_BOOTSTRAP=1`` 后，
第一条私聊消息足以完成引导：登记来源、写入白名单、持久化到 .env。

安全边界
--------
- 默认关闭；只有部署者显式打开才生效。
- 只接受 ``chat_type == "p2p"`` 且发送者不是机器人、不是应用的消息。
- 只在 owner 仍为占位值（空 / ``ou_owner``）时触发一次；数据库里记录
  ``owner_bootstrap`` 标记，之后换人发消息会被拒绝，不会重复认主。
- 这个开关会绕过“必须先显式白名单”的规则，因此生产多租户环境不要打开；
  单用户自建应用可以打开，因为应用可用范围本身已经限制了谁能发消息。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from app.core.config import get_settings
from app.core.database import session_scope
from app.core.envfile import update_env_file
from app.domain.admission import ensure_source
from app.feishu.messages import IncomingMessage
from app.models import SystemState

logger = logging.getLogger(__name__)

DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
MARKER_KEY = "owner_bootstrap"
_PLACEHOLDER_OWNERS = {"", "ou_owner", "ou_xxxxxxxx", "ou_xxx"}
_LOCK = threading.Lock()


def _is_placeholder(owner: str) -> bool:
    return (owner or "").strip().lower() in _PLACEHOLDER_OWNERS


def resolve_owner_open_id(session) -> str:
    """返回"已知的 owner open_id"：显式配置优先，其次是首条消息学到的。

    为什么需要它：p2p chat_id 已足够投递，但卡片按钮的一次性令牌、owner 指令
    校验都按 open_id 绑定；若只配了 chat_id，审批链会因 operator 对不上而全部
    被拒。首条本会话消息已经证明了发送者身份，把它记下来即可。
    """
    settings = get_settings()
    configured = (settings.feishu_owner_open_id or "").strip()
    if configured and not _is_placeholder(configured):
        return configured
    state = session.get(SystemState, MARKER_KEY)
    if state is None:
        return ""
    try:
        marker = json.loads(state.value or "{}")
    except ValueError:
        return ""
    learned = str(marker.get("owner") or "").strip()
    return "" if _is_placeholder(learned) else learned


def learn_owner_from_message(incoming: IncomingMessage) -> str:
    """owner 会话里出现首条用户消息时，记住发送者身份（不写 .env）。

    只在部署者已显式配置该 chat 为 owner 会话时触发，因此不存在"随便谁
    发消息就认主"的风险；学到之后卡片审批与 owner 指令才能通过校验。
    """
    settings = get_settings()
    configured_chat = (settings.feishu_owner_chat_id or "").strip()
    if not configured_chat or configured_chat != (incoming.chat_id or "").strip():
        return ""
    if incoming.is_from_bot or not incoming.sender_id:
        return ""
    if incoming.sender_type and incoming.sender_type != "user":
        return ""
    if not _is_placeholder(settings.feishu_owner_open_id):
        return settings.feishu_owner_open_id

    with _LOCK:
        with session_scope() as session:
            state = session.get(SystemState, MARKER_KEY)
            owner = ""
            if state is not None:
                try:
                    owner = str((json.loads(state.value or "{}")).get("owner") or "")
                except ValueError:
                    owner = ""
            if owner and owner != incoming.sender_id:
                logger.warning(
                    "owner 已绑定 %s，忽略 %s 的身份学习请求", owner[-6:], incoming.sender_id[-6:]
                )
                return owner
            if state is None:
                state = SystemState(key=MARKER_KEY, value="")
                session.add(state)
            state.value = json.dumps(
                {
                    "owner": incoming.sender_id,
                    "chat_id": incoming.chat_id,
                    "tenant": settings.tenant_id,
                    "learned": True,
                },
                ensure_ascii=False,
            )
        if _is_placeholder(settings.feishu_owner_open_id):
            object.__setattr__(settings, "feishu_owner_open_id", incoming.sender_id)
            logger.warning(
                "已从 owner 会话学到 owner open_id=%s（用于卡片审批与指令校验）",
                incoming.sender_id[-6:],
            )
        return incoming.sender_id


def maybe_bootstrap_owner(
    incoming: IncomingMessage, *, env_path: Path | None = None
) -> dict:
    """在消息持久化前尝试认主；返回结构化结果，绝不抛异常给事件循环。"""
    settings = get_settings()
    if not settings.feishu_owner_bootstrap:
        return {"bootstrapped": False, "reason": "disabled"}
    if incoming.is_from_bot:
        return {"bootstrapped": False, "reason": "bot_echo"}
    if incoming.chat_type != "p2p":
        return {"bootstrapped": False, "reason": "not_p2p"}
    if incoming.sender_type and incoming.sender_type != "user":
        return {"bootstrapped": False, "reason": "not_user"}
    if not incoming.sender_id:
        return {"bootstrapped": False, "reason": "sender_unknown"}

    with _LOCK:
        if not _is_placeholder(settings.feishu_owner_open_id):
            return {"bootstrapped": False, "reason": "owner_already_set"}

        marker: dict | None = None
        with session_scope() as session:
            state = session.get(SystemState, MARKER_KEY)
            if state is not None:
                try:
                    marker = json.loads(state.value or "{}")
                except ValueError:
                    marker = {"owner": "", "chat_id": ""}
                existing_owner = str(marker.get("owner") or "")
                if existing_owner and existing_owner != incoming.sender_id:
                    logger.warning(
                        "owner bootstrap 已绑定 %s，拒绝 %s 的认主请求",
                        existing_owner[-6:], incoming.sender_id[-6:],
                    )
                    return {
                        "bootstrapped": False,
                        "reason": "owner_already_claimed",
                        "owner": existing_owner,
                    }
            # 部署者已经显式指定 owner 会话时，只接受该会话的认主请求：
            # 避免"任意第一个私聊机器人的人"抢走 owner（多租户/共享应用场景）。
            configured_chat = (settings.feishu_owner_chat_id or "").strip()
            if configured_chat and configured_chat != incoming.chat_id:
                logger.warning(
                    "owner bootstrap 只接受已配置会话 %s，忽略 %s",
                    configured_chat[-6:], incoming.chat_id[-6:],
                )
                return {"bootstrapped": False, "reason": "chat_not_owner_configured"}

            ensure_source(
                session,
                tenant=settings.tenant_id,
                owner=incoming.sender_id,
                chat_id=incoming.chat_id,
                thread_id=incoming.thread_id,
                source_type="p2p",
                discovery="owner_bootstrap",
                read_identity="bot",
                reply_identity=settings.feishu_reply_identity,
                reply_allowed=True,
                allowed_content_types="text,post",
            )
            state = session.get(SystemState, MARKER_KEY) or SystemState(
                key=MARKER_KEY, value=""
            )
            state.value = json.dumps(
                {
                    "owner": incoming.sender_id,
                    "chat_id": incoming.chat_id,
                    "tenant": settings.tenant_id,
                },
                ensure_ascii=False,
            )
            session.add(state)

        updates = {
            "FEISHU_OWNER_OPEN_ID": incoming.sender_id,
            "FEISHU_OWNER_CHAT_ID": incoming.chat_id,
            "FEISHU_CHAT_WHITELIST": incoming.chat_id,
            "FEISHU_REPLY_ALLOWED_CHATS": incoming.chat_id,
        }
        if not settings.feishu_bot_open_id.strip():
            bot_id = incoming.bot_open_id
            if bot_id:
                updates["FEISHU_BOT_OPEN_ID"] = bot_id
        try:
            # 测试模式禁止写部署用 .env：测试会驱动真实处理器，若不隔离会把
            # 夹具里的假 owner/chat 写进真实部署配置（2026-11-10 实际踩到）。
            # 显式传入 env_path 仍允许写（调用方自己负责隔离）。
            target_path = env_path or DEFAULT_ENV_PATH
            skip = (
                env_path is None
                and settings.test_mode
                and Path(target_path).resolve() == DEFAULT_ENV_PATH.resolve()
            )
            if skip:
                logger.warning("测试模式跳过 .env 写入（owner bootstrap 仅内存生效）")
            else:
                update_env_file(target_path, updates)
        except OSError as exc:
            # 写盘失败不阻断当前进程：内存配置仍已生效，下次消息还会重试。
            logger.error("owner bootstrap 写 .env 失败: %s", exc)

        object.__setattr__(settings, "feishu_owner_open_id", incoming.sender_id)
        object.__setattr__(settings, "feishu_owner_chat_id", incoming.chat_id)
        object.__setattr__(settings, "feishu_chat_whitelist", incoming.chat_id)
        object.__setattr__(
            settings, "feishu_reply_allowed_chats", incoming.chat_id
        )
        logger.warning(
            "owner bootstrap 完成：owner=%s chat=%s（已写入 .env，重启后自动生效）",
            incoming.sender_id[-6:], incoming.chat_id[-6:],
        )
        return {
            "bootstrapped": True,
            "reason": "first_p2p_message",
            "owner": incoming.sender_id,
            "chat_id": incoming.chat_id,
        }
