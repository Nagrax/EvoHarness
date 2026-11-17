"""组织模式 Web/REPL 模型调用的预算网关装配（DE-15/BP-02）。

- 开关与 `agents.Agent` 的直连阻断一致：``EVOHARNESS_ORG_BUDGET_ENFORCE=1``；
- 归属：``EVOHARNESS_CHAT_BUDGET_ORG`` / ``_SPACE``；缺组织即 fail-closed，
  绝不把调用记到无名账号或默认公共范围；
- 每次模型调用经 ``GatewayModelPort``：reserve → 真实调用 → 按 usage settle；
- 本模块只做装配，不做计费判定；Web 与 REPL 共用同一实现。
"""
from __future__ import annotations

import os
from typing import Any

BUDGET_BYPASS_ENV = "EVOHARNESS_ORG_BUDGET_ENFORCE"
CHAT_ORG_ENV = "EVOHARNESS_CHAT_BUDGET_ORG"
CHAT_SPACE_ENV = "EVOHARNESS_CHAT_BUDGET_SPACE"

_STATE: dict | None = None


def budget_enforced() -> bool:
    return os.environ.get(BUDGET_BYPASS_ENV, "").strip().lower() in {
        "1", "true", "on", "yes"}


def _state() -> dict:
    global _STATE
    if _STATE is None:
        from evoharness.application import Application
        from evoharness.config import resolve_config
        from evoharness.governance.budget_gateway import BudgetGateway
        config = resolve_config()
        application = Application.open(config.database_url)
        _STATE = {
            "app": application,
            "gateway": BudgetGateway(application.session_factory,
                                     default_category="model_call"),
        }
    return _STATE


def build_chat_gateway_client(*, api_base: str | None, api_key: str | None,
                              model: str, use_openai: bool,
                              principal_id: str) -> Any:
    """构造网关感知客户端（duck-type AsyncOpenAI/AsyncAnthropic）。"""
    org_id = (os.environ.get(CHAT_ORG_ENV) or "").strip()
    if not org_id:
        raise RuntimeError("chat_budget_org_required")
    space_id = (os.environ.get(CHAT_SPACE_ENV) or "").strip()
    from evoharness.work.agent_adapter import GatewayModelPort
    port = GatewayModelPort(
        _state()["gateway"],
        budget_kwargs={"org_id": org_id, "space_id": space_id,
                       "principal_id": str(principal_id or "chat"),
                       "category": "model_call"})
    key = str(api_key or os.environ.get("EVOHARNESS_LOCAL_PLACEHOLDER_KEY",
                                        "local-no-key"))
    return port.build_client({
        "api_base": api_base,
        "anthropic_base_url": None if use_openai else api_base,
        "api_key": key,
        "model": model,
        "use_openai": bool(use_openai),
    })


def reset_state() -> None:
    """测试/重配置用：关闭缓存的 Application，下次重新解析配置。"""
    global _STATE
    if _STATE is not None:
        try:
            _STATE["app"].close()
        except Exception:  # noqa: BLE001
            pass
    _STATE = None
