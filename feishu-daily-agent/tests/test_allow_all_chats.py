"""全群放行（FEISHU_ALLOW_ALL_CHATS）回归。

需求（2026-11-11）：所有群都要在白名单上，而不是逐个填 chat_id。
设计取舍：
- 群是动态增加的，逐个填 chat_id 必然漏；改用显式开关，默认仍是关闭。
- "所有群"的真实边界是"机器人被拉进去的群"——飞书只推送机器人所在会话。
- 黑名单必须仍然一票否决，否则出事时没有止血手段。
- 新群首次发言要能自登记来源，否则"拉进群就能用"会退化成"等一天"。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.core.database import session_scope
from app.domain import admission
from app.models import Source
from tests.helpers import incoming

GROUP = "oc_new_group"
OWNER_CHAT = "oc_owner_p2p"


def _allow_all(settings, monkeypatch, *, blacklist: str = "") -> None:
    monkeypatch.setattr(settings, "feishu_allow_all_chats", True)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "")
    monkeypatch.setattr(settings, "feishu_chat_blacklist", blacklist)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", OWNER_CHAT)


def test_allow_all_permits_any_chat(settings, monkeypatch):
    _allow_all(settings, monkeypatch)
    assert settings.is_whitelisted(GROUP) is True
    assert settings.is_whitelisted("oc_another_group") is True
    assert settings.reply_allowed_for(GROUP) is True


def test_blacklist_still_vetoes_under_allow_all(settings, monkeypatch):
    _allow_all(settings, monkeypatch, blacklist=GROUP)
    assert settings.is_whitelisted(GROUP) is False
    assert settings.reply_allowed_for(GROUP) is False


def test_default_remains_closed(settings, monkeypatch):
    monkeypatch.setattr(settings, "feishu_allow_all_chats", False)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "feishu_owner_chat_id", OWNER_CHAT)
    assert settings.is_whitelisted(GROUP) is False


def test_new_group_auto_registers_source_on_first_message(settings, monkeypatch):
    """拉进群后第一次发言，不能因为缺 Source 被丢掉。

    注意：2026-11-11 起群聊默认免 @（FEISHU_GROUP_REPLY_ONLY_MENTION=0），
    所以这里同时覆盖"带 @ "的显式提及路径，确认两条路都通。
    """
    _allow_all(settings, monkeypatch)
    from app.handlers.events import persist_event

    accepted, reason, should_process = persist_event(
        incoming("请问年假怎么申请？", "om_newgrp_1", chat_id=GROUP, chat_type="group",
                 sender="ou_member", mentions=["ou_bot"])
    )
    assert accepted, reason
    assert reason in {"group_mention_ok", "group_no_mention_mode"}
    assert should_process is True
    with session_scope() as session:
        source = session.query(Source).filter(Source.chat_id == GROUP).one()
        assert source.discovery == "allow_all"
        assert source.enabled is True


def test_auto_registered_group_without_mention_is_processed(settings, monkeypatch):
    """免 @ 模式下，不 @ 也进处理链路（是否出声由下游按有无命中决定）。"""
    _allow_all(settings, monkeypatch)
    from app.handlers.events import persist_event

    accepted, reason, should_process = persist_event(
        incoming("大家好", "om_newgrp_2", chat_id=GROUP, chat_type="group",
                 sender="ou_member", mentions=["ou_someone"])
    )
    assert accepted
    assert reason in {"group_no_mention_mode", "group_no_mention_ingest_only"}
    # 默认配置免 @，因此应进入处理链路
    assert should_process is True


def test_concurrent_first_messages_do_not_duplicate_source(settings, monkeypatch):
    """两个线程同时为新群登记，必须只留一行、且都不抛错。"""
    _allow_all(settings, monkeypatch)

    def register(index: int) -> bool:
        with session_scope() as session:
            admission.ensure_source(
                session,
                tenant=settings.tenant_id,
                owner="ou_owner",
                chat_id=GROUP,
                source_type="group",
                discovery="allow_all",
            )
        return True

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(register, range(6)))
    with session_scope() as session:
        assert session.query(Source).filter(Source.chat_id == GROUP).count() == 1


def test_pipeline_bootstrap_picks_up_all_groups(settings, monkeypatch):
    """日报 bootstrap 会把 list_chats 返回的所有群登记为来源。"""
    _allow_all(settings, monkeypatch)
    from app.pipeline.daily import bootstrap_sources
    from tests.helpers import FakeTransport

    transport = FakeTransport(chats=[
        {"chat_id": GROUP, "chat_type": "group", "name": "新群"},
        {"chat_id": "oc_group_2", "chat_type": "group", "name": "第二个群"},
    ])
    with session_scope() as session:
        sources = bootstrap_sources(session, transport=transport)
        ids = {s.chat_id for s in sources}
    assert GROUP in ids
    assert "oc_group_2" in ids
