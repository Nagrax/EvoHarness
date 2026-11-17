"""OpenAI 兼容客户端（惰性创建，离线测试不触网）。"""
from __future__ import annotations

from typing import Any

from app.core.config import get_settings

_client: Any = None


def get_llm_client() -> Any:
    global _client
    if _client is None:
        from openai import OpenAI

        settings = get_settings()
        client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout,
        )
        # DE-15：组织模式下经统一预算网关逐次预留/结算；默认模式原样返回。
        from evoharness.governance.feishu_budget import maybe_wrap_openai
        _client = maybe_wrap_openai(client, principal="feishu-agent")
    return _client


def reset_client() -> None:
    global _client
    _client = None
