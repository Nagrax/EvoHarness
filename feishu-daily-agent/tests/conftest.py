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
# 部署用 .env 里的真实 owner 会话/邮箱不得渗进测试（否则夹具认主会被挡下，
# 或者测试反过来把假 owner 写进真实配置）。环境变量优先级高于 .env。
os.environ["FEISHU_OWNER_CHAT_ID"] = ""
os.environ["FEISHU_OWNER_EMAIL"] = ""
os.environ["FEISHU_OWNER_BOOTSTRAP"] = "0"
os.environ["FEISHU_CHAT_WHITELIST"] = ""
os.environ["FEISHU_CHAT_BLACKLIST"] = ""
os.environ["FEISHU_REPLY_ALLOWED_CHATS"] = ""
# 部署环境可能开了"全群放行"；测试必须从关闭态起跑，否则白名单/回复许可
# 相关的断言会全部失真（2026-11-11 实际踩到：.env 开了 ALLOW_ALL，
# 两个"读许可不等于写许可"的用例直接翻红）。
os.environ["FEISHU_ALLOW_ALL_CHATS"] = "0"
os.environ["FEISHU_DAILY_REPORT_ENABLED"] = "0"
os.environ["FEISHU_ALLOW_LIVE_MODEL"] = "0"
os.environ["FEISHU_BOT_OPEN_ID"] = "ou_bot"
# User-confirmed default; mention-only cases enable their gate explicitly.
os.environ["FEISHU_GROUP_REPLY_ONLY_MENTION"] = "0"
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


@pytest.fixture(autouse=True)
def _reset_owner_mutations():
    """owner bootstrap 会就地改 Settings 实例（object.__setattr__），
    monkeypatch 管不到，必须每例快照/还原，否则会串到后续测试。"""
    from app.core.config import get_settings

    settings = get_settings()
    fields = (
        "feishu_owner_open_id",
        "feishu_owner_chat_id",
        "feishu_chat_whitelist",
        "feishu_reply_allowed_chats",
        "feishu_bot_open_id",
    )
    snapshot = {name: getattr(settings, name) for name in fields}
    yield
    for name, value in snapshot.items():
        object.__setattr__(settings, name, value)


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
