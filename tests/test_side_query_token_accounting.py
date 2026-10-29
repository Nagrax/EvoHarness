"""旁路查询（side query）token 单独记账的回归测试。

背景：主循环之外的调用（记忆召回、折叠/交接、沉淀抽取与使用判定、离线评测）此前
不记 token，导致"沉淀这一步花了多少 token"无法被量出、整题成本还被低估。本测试锁定：
- Anthropic / OpenAI 两条分支都能把 usage 累加进独立计数器，并按用途分桶；
- 用量缺失或畸形时静默跳过，不影响返回值；
- 没有可用客户端时工厂方法仍返回 None（不回归行为）。
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.agent import Agent  # noqa: E402


def _anthropic_agent() -> Agent:
    return Agent(
        api_key="test-key",
        anthropic_base_url="http://127.0.0.1:9",
        custom_system_prompt="sys",
        is_sub_agent=True,
        permission_mode="bypassPermissions",
    )


def _openai_agent() -> Agent:
    return Agent(
        api_base="http://127.0.0.1:9/v1",
        api_key="test-key",
        custom_system_prompt="sys",
        is_sub_agent=True,
        permission_mode="bypassPermissions",
    )


class _StubAnthropicMessages:
    def __init__(self, *, input_tokens, output_tokens, text="ok", with_usage=True):
        self._in = input_tokens
        self._out = output_tokens
        self._text = text
        self._with_usage = with_usage

    async def create(self, **kwargs):
        usage = SimpleNamespace(input_tokens=self._in, output_tokens=self._out) if self._with_usage else None
        return SimpleNamespace(
            usage=usage,
            content=[SimpleNamespace(type="text", text=self._text)],
            model=kwargs.get("model"),
            stop_reason="end_turn",
        )


class _StubAnthropicClient:
    def __init__(self, **kw):
        self.messages = _StubAnthropicMessages(**kw)


class _StubOpenAICompletions:
    def __init__(self, *, input_tokens, output_tokens, text="ok", with_usage=True):
        self._in = input_tokens
        self._out = output_tokens
        self._text = text
        self._with_usage = with_usage

    async def create(self, **kwargs):
        usage = (
            SimpleNamespace(prompt_tokens=self._in, completion_tokens=self._out) if self._with_usage else None
        )
        choice = SimpleNamespace(message=SimpleNamespace(content=self._text))
        return SimpleNamespace(choices=[choice], usage=usage)


class _StubOpenAIClient:
    def __init__(self, **kw):
        self.chat = SimpleNamespace(completions=_StubOpenAICompletions(**kw))


def test_anthropic_side_query_tokens_accumulate_and_bucket():
    agent = _anthropic_agent()
    agent._anthropic_client = _StubAnthropicClient(input_tokens=120, output_tokens=30)
    agent._openai_client = None

    sq = agent._build_side_query(max_tokens=100, label="evolution_extract")
    text = asyncio.run(sq("sys", "user"))

    assert text == "ok"
    assert agent.side_query_input_tokens == 120
    assert agent.side_query_output_tokens == 30
    assert agent.side_query_tokens_by_label["evolution_extract"] == {"input": 120, "output": 30}
    # 主循环计数器不受影响（两者分开记账）。
    assert agent.total_input_tokens == 0 and agent.total_output_tokens == 0


def test_openai_side_query_tokens_accumulate_and_bucket():
    agent = _openai_agent()
    agent._openai_client = _StubOpenAIClient(input_tokens=400, output_tokens=50)
    agent._anthropic_client = None

    sq = agent._build_side_query(max_tokens=100, label="evolution_usage_judge")
    text = asyncio.run(sq("sys", "user"))

    assert text == "ok"
    assert agent.side_query_input_tokens == 400
    assert agent.side_query_output_tokens == 50
    assert agent.side_query_tokens_by_label["evolution_usage_judge"] == {"input": 400, "output": 50}


def test_side_query_tokens_sum_across_labels():
    agent = _openai_agent()
    agent._openai_client = _StubOpenAIClient(input_tokens=100, output_tokens=10)
    agent._anthropic_client = None

    asyncio.run(agent._build_side_query(label="memory_recall")("sys", "u"))
    asyncio.run(agent._build_side_query(label="evolution_extract")("sys", "u"))

    assert agent.side_query_input_tokens == 200
    assert agent.side_query_output_tokens == 20
    assert agent.side_query_tokens_by_label["memory_recall"]["input"] == 100
    assert agent.side_query_tokens_by_label["evolution_extract"]["input"] == 100


def test_side_query_missing_usage_is_skipped_without_breaking():
    agent = _anthropic_agent()
    agent._anthropic_client = _StubAnthropicClient(input_tokens=0, output_tokens=0, with_usage=False)
    agent._openai_client = None

    text = asyncio.run(agent._build_side_query(label="fold")("sys", "u"))

    assert text == "ok"
    assert agent.side_query_input_tokens == 0
    assert agent.side_query_output_tokens == 0
    assert agent.side_query_tokens_by_label == {}


def test_side_query_returns_none_without_clients():
    agent = _anthropic_agent()
    agent._anthropic_client = None
    agent._openai_client = None

    assert agent._build_side_query(label="skill_eval") is None
