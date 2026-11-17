"""DE-15 评测脚本异步模型调用预算网关回归（离线）。

覆盖：开关/缺组织、端口客户端构造、真实 usage 结算、异常保守归集、
四个 eval 脚本接线存在。
"""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from evoharness.governance import budget, eval_budget
from evoharness.governance.models import BudgetReservation
from evoharness.models import Base


class _FakeCompletions:
    def __init__(self, *, boom=False):
        self.boom = boom

    async def create(self, **kwargs):
        if self.boom:
            raise ValueError("provider down")
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])


class _FakeReal:
    def __init__(self, *, boom=False):
        self.chat = SimpleNamespace(completions=_FakeCompletions(boom=boom))


@pytest.fixture()
def budget_env(tmp_path, monkeypatch):
    db = tmp_path / "eval-budget.db"
    engine = create_engine("sqlite:///" + db.as_posix(), future=True)
    import evoharness.governance.models  # noqa: F401
    import evoharness.work.models  # noqa: F401
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        budget.set_limit(session, scope_kind="org", scope_id="org",
                         limit_tokens=10000)
        budget.set_limit(session, scope_kind="space", scope_id="space",
                         limit_tokens=10000)
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL", "sqlite:///" + db.as_posix())
    monkeypatch.setenv(eval_budget.BUDGET_ENFORCE_ENV, "1")
    monkeypatch.setenv(eval_budget.EVAL_ORG_ENV, "org")
    monkeypatch.setenv(eval_budget.EVAL_SPACE_ENV, "space")
    eval_budget.reset_state()
    try:
        yield factory
    finally:
        eval_budget.reset_state()
        engine.dispose()


def _rows(factory):
    with factory() as session:
        return list(session.scalars(select(BudgetReservation)))


def test_enabled_switch(monkeypatch):
    monkeypatch.delenv(eval_budget.BUDGET_ENFORCE_ENV, raising=False)
    assert eval_budget.enabled() is False
    monkeypatch.setenv(eval_budget.BUDGET_ENFORCE_ENV, "1")
    assert eval_budget.enabled() is True


def test_missing_org_fails_closed(budget_env, monkeypatch):
    monkeypatch.delenv(eval_budget.EVAL_ORG_ENV, raising=False)
    with pytest.raises(RuntimeError, match="eval_budget_org_required"):
        eval_budget.build_async_client(api_base="http://127.0.0.1:9/v1",
                                       api_key="x", model="m")


def test_gateway_client_settles_real_usage(budget_env):
    client = eval_budget.build_async_client(
        api_base="http://127.0.0.1:9/v1", api_key="x", model="m",
        use_openai=True, principal="eval-test")
    assert type(client).__name__ == "_GatewayOpenAIClient"
    assert client._port.budget_kwargs["principal_id"] == "eval-test"
    client._port.real = _FakeReal()
    response = asyncio.run(client.chat.completions.create(model="m", messages=[]))
    assert response.choices[0].message.content == "ok"
    rows = _rows(budget_env)
    assert len(rows) == 1 and rows[0].status == "settled"
    assert rows[0].used_tokens == 15 and rows[0].principal_id == "eval-test"


def test_gateway_client_conservatively_settles_failure(budget_env):
    client = eval_budget.build_async_client(
        api_base="http://127.0.0.1:9/v1", api_key="x", model="m")
    client._port.real = _FakeReal(boom=True)
    with pytest.raises(ValueError, match="provider down"):
        asyncio.run(client.chat.completions.create(model="m", messages=[]))
    rows = _rows(budget_env)
    assert len(rows) == 1 and not rows[0].ok
    assert rows[0].used_tokens == rows[0].reserved_tokens
    assert rows[0].usage_status == "unknown"


def test_eval_scripts_are_wired_to_the_gateway():
    for name in ("eval/memory_lifecycle_ab.py", "eval/reflection_recall_probe.py",
                 "eval/evolution_experiment.py", "eval/run_benchmark.py"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert "eval_budget" in text, name
