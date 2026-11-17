"""DE-15 组织模式 Web/REPL 预算网关装配回归。

- 开关解析；缺组织 fail-closed；org+space 下构造出的客户端是网关感知端口
  （duck-type AsyncOpenAI），不触网；
- server `_get_agent` 在组织模式缺组织时给出可操作错误，配置齐全时注入端口；
- REPL 装配路径存在（源码断言）。
"""
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evoharness.governance import chat_budget
from evoharness.models import Base

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv(chat_budget.BUDGET_BYPASS_ENV, raising=False)
    monkeypatch.delenv(chat_budget.CHAT_ORG_ENV, raising=False)
    monkeypatch.delenv(chat_budget.CHAT_SPACE_ENV, raising=False)
    chat_budget.reset_state()
    yield monkeypatch, tmp_path
    chat_budget.reset_state()


def test_budget_enforced_switch(clean_env):
    monkeypatch, _ = clean_env
    assert chat_budget.budget_enforced() is False
    monkeypatch.setenv(chat_budget.BUDGET_BYPASS_ENV, "1")
    assert chat_budget.budget_enforced() is True


def test_missing_org_fails_closed(clean_env):
    monkeypatch, tmp_path = clean_env
    monkeypatch.setenv(chat_budget.BUDGET_BYPASS_ENV, "1")
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL",
                       "sqlite:///" + (tmp_path / "chat.db").as_posix())
    with pytest.raises(RuntimeError, match="chat_budget_org_required"):
        chat_budget.build_chat_gateway_client(
            api_base="http://127.0.0.1:9/v1", api_key="synthetic", model="m",
            use_openai=True, principal_id="web-chat")


def test_builds_gateway_aware_client_with_org(clean_env):
    monkeypatch, tmp_path = clean_env
    db = tmp_path / "chat.db"
    # 预建业务库表（BudgetGateway 预留会访问预算表）
    engine = create_engine("sqlite:///" + db.as_posix(), future=True)
    Base.metadata.create_all(engine)
    import evoharness.work.models  # noqa: F401 — 注册 Run 表
    import evoharness.governance.models  # noqa: F401 — 注册预算表
    engine.dispose()
    monkeypatch.setenv(chat_budget.BUDGET_BYPASS_ENV, "1")
    monkeypatch.setenv(chat_budget.CHAT_ORG_ENV, "org")
    monkeypatch.setenv(chat_budget.CHAT_SPACE_ENV, "space")
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL", "sqlite:///" + db.as_posix())
    client = chat_budget.build_chat_gateway_client(
        api_base="http://127.0.0.1:9/v1", api_key="synthetic", model="m",
        use_openai=True, principal_id="web-chat")
    assert type(client).__name__ == "_GatewayOpenAIClient"
    assert client._port.budget_kwargs["org_id"] == "org"
    assert client._port.budget_kwargs["space_id"] == "space"
    assert client._port.budget_kwargs["principal_id"] == "web-chat"


def test_server_get_agent_requires_org_in_org_mode(clean_env):
    monkeypatch, tmp_path = clean_env
    monkeypatch.setenv(chat_budget.BUDGET_BYPASS_ENV, "1")
    monkeypatch.setenv("EVOHARNESS_DATABASE_URL",
                       "sqlite:///" + (tmp_path / "server.db").as_posix())
    import server.app as web
    with pytest.raises(RuntimeError, match="budget_gateway_unavailable"):
        web._get_agent("sess-budget", "default",
                       api_base="http://127.0.0.1:9/v1", api_key="synthetic",
                       model="m")
    web.AGENTS.pop("sess-budget", None)


def test_server_and_repl_share_the_assembly_helper():
    server_src = (REPO / "server" / "app.py").read_text(encoding="utf-8")
    repl_src = (REPO / "agents" / "main.py").read_text(encoding="utf-8")
    assert "build_chat_gateway_client" in server_src
    assert "build_chat_gateway_client" in repl_src
