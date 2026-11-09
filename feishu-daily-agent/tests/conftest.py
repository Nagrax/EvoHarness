"""测试隔离：临时数据库、固定假身份、网络阻断。"""
from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest

TEST_DB = Path(tempfile.mkdtemp(prefix="feishu-test-")) / "agent.db"
os.environ["FEISHU_TEST_MODE"] = "1"
os.environ["FEISHU_TEST_DB_PREFIX"] = str(TEST_DB.parent)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["FEISHU_APP_ID"] = "test-app"
os.environ["FEISHU_APP_SECRET"] = "test-secret"
os.environ["FEISHU_OWNER_OPEN_ID"] = "ou_owner"
os.environ["FEISHU_BOT_OPEN_ID"] = "ou_bot"
os.environ["FEISHU_DEPLOYMENT_MODE"] = "offline"
os.environ["FEISHU_AUTO_REPLY_ENABLED"] = "0"
os.environ["FEISHU_SHADOW_MODE"] = "1"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9"
os.environ["LLM_API_KEY"] = "test"
os.environ["LLM_MODEL"] = "test-model"
os.environ.setdefault("EVOHARNESS_ROOT", str(Path(__file__).resolve().parents[2]))
os.environ["FEISHU_HEARTBEAT_PATH"] = str(TEST_DB.parent / "heartbeat.json")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("测试禁止访问网络")

    monkeypatch.setattr(socket, "create_connection", blocked)
    original_connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"测试禁止访问网络: {address}")
        return original_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def _reset_db():
    from app.core.database import engine
    from app.models import Base

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def settings():
    from app.core.config import get_settings

    return get_settings()


@pytest.fixture
def auto_mode(monkeypatch, settings):
    monkeypatch.setattr(settings, "deployment_mode", "whitelist_auto")
    monkeypatch.setattr(settings, "auto_reply_enabled", True)
    monkeypatch.setattr(settings, "shadow_mode", False)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "oc_1")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "oc_1")
    return settings


@pytest.fixture
def approval_mode(monkeypatch, settings):
    monkeypatch.setattr(settings, "deployment_mode", "single_approval")
    monkeypatch.setattr(settings, "auto_reply_enabled", False)
    monkeypatch.setattr(settings, "shadow_mode", False)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "oc_1")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "oc_1")
    return settings


@pytest.fixture
def source():
    from app.core.database import session_scope
    from app.domain.admission import ensure_source

    with session_scope() as session:
        value = ensure_source(
            session,
            tenant="default",
            owner="ou_owner",
            chat_id="oc_1",
            source_type="p2p",
            reply_allowed=True,
            allowed_content_types="text,post",
        )
        session.flush()
        session.expunge(value)
        return value
