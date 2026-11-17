"""EvoHarness Agent 桥（L9049）：协同层显式求助执行层大脑。

设计约束：
- 默认关闭（FEISHU_EVOHARNESS_ASK_ENABLED=0），开启后仅当消息带「问：」前缀
  才调用 http://127.0.0.1:8800/api/chat（SSE）；失败/超时返回 None，调用方
  优雅回落到经验库流程——协同层不能因为执行层不可用而停摆。
- 桥接层只做传输与裁剪，不做策略：答案走标准草稿审批链（respect 部署模式）。
- 测试环境网络被阻断，bridge 仅在 enabled() 且被调用时触网。
"""
from __future__ import annotations

import json
import logging

from app.core.config import get_settings

logger = logging.getLogger(__name__)

ASK_PREFIXES = ("问：", "问:", "ask:", "Ask:")
ANSWER_TIMEOUT = 90.0
MAX_ANSWER_CHARS = 1800  # 草稿/卡片体积约束（FEISHU_DAILY_CARD_MAX_BYTES 之内量级）


def enabled() -> bool:
    try:
        return bool(get_settings().evoharness_ask_enabled)
    except Exception:  # noqa: BLE001 — 配置异常按关闭处理
        return False


def strip_ask_prefix(text: str) -> str | None:
    """命中「问：」前缀返回去前缀文本；否则 None。"""
    raw = (text or "").strip()
    for prefix in ASK_PREFIXES:
        if raw.startswith(prefix):
            stripped = raw[len(prefix):].strip()
            return stripped or None
    return None


def fetch_answer(question: str) -> str | None:
    """向 EvoHarness Web 服务提一个问题，收集 SSE 文本帧；失败返回 None。"""
    settings = get_settings()
    payload = {
        "session_id": f"feishu-ask-{abs(hash(question)) % 10**8}",
        "message": question,
        "permission_mode": "default",
    }
    chunks: list[str] = []
    try:
        import requests

        with requests.post(
            f"{settings.evoharness_ask_base_url}/api/chat",
            json=payload, stream=True, timeout=(5, ANSWER_TIMEOUT),
        ) as resp:
            if resp.status_code != 200:
                logger.warning("agent bridge HTTP %s", resp.status_code)
                return None
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except ValueError:
                    continue
                etype = event.get("type")
                if etype == "text":
                    chunks.append(event.get("text") or "")
                elif etype == "error":
                    logger.warning("agent bridge error event: %s", event.get("message"))
                    return None
                elif etype == "end":
                    break
    except Exception as exc:  # noqa: BLE001 — 网络/服务不可用必须静默回落
        logger.warning("agent bridge unavailable: %s", f"{type(exc).__name__}: {exc}"[:120])
        return None
    answer = "".join(chunks).strip()
    if not answer:
        return None
    if len(answer) > MAX_ANSWER_CHARS:
        answer = answer[:MAX_ANSWER_CHARS - 1] + "…"
    return answer
