"""飞书客户端单例与机器人信息。"""
import json
import logging

import lark_oapi as lark
from lark_oapi.core.enum import AccessTokenType, HttpMethod
from lark_oapi.core.model import BaseRequest

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_client: lark.Client | None = None
_bot_open_id: str | None = None


def get_feishu_client() -> lark.Client:
    global _client
    if _client is None:
        settings = get_settings()
        _client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
    return _client


def get_bot_open_id() -> str:
    """获取机器人自身 open_id，用于群聊 @机器人 判断；失败返回空串走降级逻辑。

    SDK 未生成 bot.v3 资源，这里通过公开的原始请求能力调用
    GET /open-apis/bot/v3/info（机器人能力启用后即可用，无需额外权限）。
    """
    global _bot_open_id
    if _bot_open_id is not None:
        return _bot_open_id

    try:
        req = BaseRequest()
        req.http_method = HttpMethod.GET
        req.uri = "/open-apis/bot/v3/info"
        req.token_types = {AccessTokenType.TENANT}

        resp = get_feishu_client().request(req)
        body = json.loads(resp.content.decode("utf-8"))
        if body.get("code") not in (0, None):
            logger.warning("获取机器人信息失败 code=%s msg=%s", body.get("code"), body.get("msg"))
            _bot_open_id = ""
            return _bot_open_id

        bot = body.get("bot") or body.get("data", {}).get("bot") or {}
        _bot_open_id = bot.get("open_id") or bot.get("openid") or ""
        if not _bot_open_id:
            logger.warning("bot info 响应中未找到 open_id: %s", body)
    except Exception as exc:
        logger.warning("调用 bot info 异常，@机器人判断将降级为 mentions 非空: %s", exc)
        _bot_open_id = ""

    return _bot_open_id
