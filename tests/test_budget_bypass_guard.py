"""DE-15 组织模式阻断 SDK 直连旁路（必须注入网关感知端口）。

- EVOHARNESS_ORG_BUDGET_ENFORCE=1 时，Agent 不得自行构造真实付费 SDK 客户端；
- 注入 sdk_client（BudgetGateway 端口/离线假 SDK）仍可构造；
- 默认模式行为不变（不直连阻断只在本开关下生效）。
"""
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agents.agent import Agent, BUDGET_BYPASS_ENV


def test_org_mode_blocks_direct_openai_client(monkeypatch):
    monkeypatch.setenv(BUDGET_BYPASS_ENV, "1")
    with pytest.raises(RuntimeError, match="budget_sdk_bypass_blocked"):
        Agent(api_base="http://127.0.0.1:9/v1", api_key="synthetic")


def test_org_mode_blocks_direct_anthropic_client(monkeypatch):
    monkeypatch.setenv(BUDGET_BYPASS_ENV, "1")
    with pytest.raises(RuntimeError, match="budget_sdk_bypass_blocked"):
        Agent(custom_system_prompt="sys", is_sub_agent=True)


def test_org_mode_allows_injected_gateway_client(monkeypatch):
    monkeypatch.setenv(BUDGET_BYPASS_ENV, "1")
    dummy = SimpleNamespace()
    agent = Agent(api_base="http://127.0.0.1:9/v1", api_key="synthetic",
                  sdk_client=dummy)
    assert agent._openai_client is dummy and agent._anthropic_client is None


def test_default_mode_keeps_injected_client_path(monkeypatch):
    monkeypatch.delenv(BUDGET_BYPASS_ENV, raising=False)
    dummy = SimpleNamespace()
    agent = Agent(custom_system_prompt="sys", is_sub_agent=True, sdk_client=dummy)
    assert agent._anthropic_client is dummy and agent._openai_client is None
