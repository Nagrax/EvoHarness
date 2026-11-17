"""评测脚本（eval/）异步模型调用的预算网关装配（DE-15 路径 5）。

- 开关：``EVOHARNESS_ORG_BUDGET_ENFORCE=1``；
- 归属：``EVOHARNESS_EVAL_BUDGET_ORG`` / ``_SPACE``，缺组织 fail-closed；
- 每次调用经 ``GatewayModelPort``：reserve→调用→真实 usage settle；
- 默认模式（开关未开）脚本保持原直连。
"""
from __future__ import annotations

import os
from typing import Any

BUDGET_ENFORCE_ENV = "EVOHARNESS_ORG_BUDGET_ENFORCE"
EVAL_ORG_ENV = "EVOHARNESS_EVAL_BUDGET_ORG"
EVAL_SPACE_ENV = "EVOHARNESS_EVAL_BUDGET_SPACE"

_STATE: dict | None = None


def enabled() -> bool:
    return os.environ.get(BUDGET_ENFORCE_ENV, "").strip().lower() in {
        "1", "true", "on", "yes"}


def _gateway():
    global _STATE
    if _STATE is None:
        from evoharness.application import Application
        from evoharness.config import resolve_config
        from evoharness.governance.budget_gateway import BudgetGateway
        config = resolve_config()
        application = Application.open(config.database_url)
        _STATE = {"app": application,
                  "gateway": BudgetGateway(application.session_factory)}
    return _STATE["gateway"]


def build_async_client(*, api_base: str | None, api_key: str | None,
                       model: str, use_openai: bool = True,
                       principal: str = "eval") -> Any:
    org_id = (os.environ.get(EVAL_ORG_ENV) or "").strip()
    if not org_id:
        raise RuntimeError("eval_budget_org_required")
    space_id = (os.environ.get(EVAL_SPACE_ENV) or "").strip()
    from evoharness.work.agent_adapter import GatewayModelPort
    port = GatewayModelPort(
        _gateway(),
        budget_kwargs={"org_id": org_id, "space_id": space_id,
                       "principal_id": principal, "category": "model_call"})
    return port.build_client({
        "api_base": api_base,
        "anthropic_base_url": None if use_openai else api_base,
        "api_key": str(api_key or os.environ.get("EVOHARNESS_LOCAL_PLACEHOLDER_KEY",
                                                 "local-no-key")),
        "model": model,
        "use_openai": bool(use_openai),
    })


def reset_state() -> None:
    global _STATE
    if _STATE is not None:
        try:
            _STATE["app"].close()
        except Exception:  # noqa: BLE001
            pass
    _STATE = None
