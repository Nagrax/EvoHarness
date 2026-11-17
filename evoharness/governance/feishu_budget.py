"""飞书同步 OpenAI 客户端的预算网关包装（DE-15 路径 3/4）。

- 开关：``EVOHARNESS_ORG_BUDGET_ENFORCE=1``（与 Agent/Web/REPL 同一开关）；
- 归属：``FEISHU_BUDGET_ORG`` / ``FEISHU_BUDGET_SPACE``；缺组织 fail-closed；
- 每次 ``chat.completions.create`` / ``embeddings.create`` 一笔预留 + 按真实
  usage 结算；异常按预留全额保守归集并原样抛出；无 usage 字段按 unknown 归集；
- 默认模式（开关未开）原样返回客户端，飞书既有行为不变。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from evoharness.ids import sha256_json

BUDGET_ENFORCE_ENV = "EVOHARNESS_ORG_BUDGET_ENFORCE"
FEISHU_ORG_ENV = "FEISHU_BUDGET_ORG"
FEISHU_SPACE_ENV = "FEISHU_BUDGET_SPACE"

_STATE: dict | None = None


def budget_enforced() -> bool:
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


def _attribution() -> tuple[str, str]:
    org_id = (os.environ.get(FEISHU_ORG_ENV) or "").strip()
    if not org_id:
        raise RuntimeError("feishu_budget_org_required")
    return org_id, (os.environ.get(FEISHU_SPACE_ENV) or "").strip()


def _usage_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    total = getattr(usage, "total_tokens", None)
    if total is not None:
        return max(0, int(total))
    return max(0, int(getattr(usage, "prompt_tokens", 0) or 0)
               + int(getattr(usage, "completion_tokens", 0) or 0))


class _BudgetedCreate:
    def __init__(self, gateway, real, *, category: str, principal: str):
        self.gateway = gateway
        self.real = real
        self.category = category
        self.principal = principal

    def create(self, **kwargs):
        if kwargs.get("stream"):
            # 飞书当前不流式调用；流式拿不到稳定 usage，明确拒绝而不是少记。
            raise RuntimeError("budget_stream_unsupported")
        org_id, space_id = _attribution()
        record = self.gateway.reserve(
            org_id=org_id, space_id=space_id, principal_id=self.principal,
            category=self.category, request_hash=sha256_json(kwargs))
        self.gateway.mark_executing(record.reservation_id)
        try:
            result = self.real.create(**kwargs)
        except Exception:
            self.gateway.settle(record, used_tokens=record.reserved_tokens,
                                ok=False, usage_status="unknown",
                                outcome="failed")
            raise
        usage = _usage_tokens(result)
        if usage is None:
            self.gateway.settle(record, used_tokens=record.reserved_tokens,
                                ok=True, usage_status="unknown", outcome="ok")
        else:
            self.gateway.settle(record, used_tokens=usage, ok=True, outcome="ok")
        return result


class BudgetedOpenAI:
    """duck-type OpenAI 客户端：只替换 chat.completions / embeddings 两个付费面。"""

    def __init__(self, real: Any, *, principal: str = "feishu-agent"):
        gateway = _gateway()
        self._real = real
        self.chat = SimpleNamespace(completions=_BudgetedCreate(
            gateway, real.chat.completions, category="model_call",
            principal=principal))
        self.embeddings = _BudgetedCreate(
            gateway, real.embeddings, category="embedding", principal=principal)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def maybe_wrap_openai(client: Any, *, principal: str = "feishu-agent") -> Any:
    if not budget_enforced():
        return client
    return BudgetedOpenAI(client, principal=principal)


def reset_state() -> None:
    global _STATE
    if _STATE is not None:
        try:
            _STATE["app"].close()
        except Exception:  # noqa: BLE001
            pass
    _STATE = None
