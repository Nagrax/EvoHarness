"""DE-15 飞书 LLM/embedding 预算网关包装回归（离线）。

覆盖：开关关闭原样返回、缺归属 fail-closed、chat/embedding 按真实 usage
逐笔结算、异常保守归集并原样抛出、无 usage 按 unknown、流式明确拒绝。
"""
from pathlib import Path
from types import SimpleNamespace
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.governance import budget, feishu_budget
from evoharness.governance.models import BudgetReservation
from evoharness.models import Base


class _ChatCompletions:
    def __init__(self, *, usage=True, boom=False):
        self.usage = usage
        self.boom = boom
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.boom:
            raise ValueError("provider down")
        usage = (SimpleNamespace(prompt_tokens=10, completion_tokens=5)
                 if self.usage else None)
        return SimpleNamespace(usage=usage, text="ok")


class _Embeddings:
    def __init__(self, *, usage=True):
        self.usage = usage
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        usage = SimpleNamespace(total_tokens=7) if self.usage else None
        return SimpleNamespace(usage=usage, data=[SimpleNamespace(embedding=[0.1])])


class FakeOpenAI:
    def __init__(self, *, usage=True, boom=False):
        self.chat = SimpleNamespace(completions=_ChatCompletions(usage=usage, boom=boom))
        self.embeddings = _Embeddings(usage=usage)


@pytest.fixture()
def budget_env(tmp_path, monkeypatch):
    db = tmp_path / "budget.db"
    engine = create_engine("sqlite:///" + db.as_posix(), future=True)
    import evoharness.governance.models  # noqa: F401 — 注册预算表
    import evoharness.work.models  # noqa: F401
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org",
                         limit_tokens=10000)
        budget.set_limit(session, scope_kind="space", scope_id="space",
                         limit_tokens=10000)
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL", "sqlite:///" + db.as_posix())
    monkeypatch.setenv(feishu_budget.BUDGET_ENFORCE_ENV, "1")
    monkeypatch.setenv(feishu_budget.FEISHU_ORG_ENV, "org")
    monkeypatch.setenv(feishu_budget.FEISHU_SPACE_ENV, "space")
    feishu_budget.reset_state()
    try:
        yield factory
    finally:
        feishu_budget.reset_state()
        engine.dispose()


def _rows(factory):
    with factory() as session:
        return list(session.scalars(select(BudgetReservation)))


def test_disabled_returns_original_client(monkeypatch):
    monkeypatch.delenv(feishu_budget.BUDGET_ENFORCE_ENV, raising=False)
    real = FakeOpenAI()
    assert feishu_budget.maybe_wrap_openai(real) is real


def test_missing_org_fails_closed(budget_env, monkeypatch):
    monkeypatch.delenv(feishu_budget.FEISHU_ORG_ENV, raising=False)
    with pytest.raises(RuntimeError, match="feishu_budget_org_required"):
        feishu_budget.maybe_wrap_openai(FakeOpenAI()).chat.completions.create(
            model="m", messages=[])


def test_chat_call_settles_real_usage(budget_env):
    real = FakeOpenAI()
    client = feishu_budget.maybe_wrap_openai(real)
    response = client.chat.completions.create(model="m", messages=[])
    assert response.text == "ok" and real.chat.completions.calls == 1
    rows = _rows(budget_env)
    assert len(rows) == 1
    assert rows[0].category == "model_call" and rows[0].status == "settled"
    assert rows[0].used_tokens == 15 and rows[0].ok


def test_embedding_call_uses_embedding_category(budget_env):
    real = FakeOpenAI()
    client = feishu_budget.maybe_wrap_openai(real, principal="feishu-embedding")
    client.embeddings.create(model="emb", input=["x"])
    rows = _rows(budget_env)
    assert len(rows) == 1
    assert rows[0].category == "embedding" and rows[0].used_tokens == 7
    assert rows[0].principal_id == "feishu-embedding"


def test_provider_exception_is_conservatively_settled_and_reraised(budget_env):
    real = FakeOpenAI(boom=True)
    client = feishu_budget.maybe_wrap_openai(real)
    with pytest.raises(ValueError, match="provider down"):
        client.chat.completions.create(model="m", messages=[])
    rows = _rows(budget_env)
    assert len(rows) == 1 and not rows[0].ok
    assert rows[0].used_tokens == rows[0].reserved_tokens
    assert rows[0].usage_status == "unknown"


def test_missing_usage_is_settled_as_unknown(budget_env):
    real = FakeOpenAI(usage=False)
    client = feishu_budget.maybe_wrap_openai(real)
    client.chat.completions.create(model="m", messages=[])
    rows = _rows(budget_env)
    assert len(rows) == 1 and rows[0].ok
    assert rows[0].used_tokens == rows[0].reserved_tokens
    assert rows[0].usage_status == "unknown"


def test_stream_is_explicitly_rejected(budget_env):
    client = feishu_budget.maybe_wrap_openai(FakeOpenAI())
    with pytest.raises(RuntimeError, match="budget_stream_unsupported"):
        client.chat.completions.create(model="m", messages=[], stream=True)
    assert _rows(budget_env) == []
