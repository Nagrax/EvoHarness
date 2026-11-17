"""接入实机回归：owner chat 授权、chat 类型推断、投递目标回退。

背景（2026-11-10 实机接入）：
1. p2p chat_id 也是 oc_ 前缀，按前缀判群会把 owner 私聊登记成 group，
   随后"群聊必须 @"的准入规则会静默丢弃 owner 的真实消息。
2. owner 的 open_id 在本应用命名空间下可能未知（open_id 跨应用不同），
   但 chat_id / 企业邮箱仍可投递；日报不能因为 open_id 缺失而发不出去。
"""
from __future__ import annotations

from app.core.database import session_scope
from app.domain import admission
from app.models import Message
from tests.helpers import incoming

P2P_CHAT = "oc_owner_p2p"
GROUP_CHAT = "oc_real_group"


def _enable_owner(settings, monkeypatch, chat_id: str = P2P_CHAT) -> None:
    monkeypatch.setattr(settings, "feishu_owner_chat_id", chat_id)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "")


def test_owner_chat_is_whitelisted_without_csv_entry(settings, monkeypatch):
    _enable_owner(settings, monkeypatch)
    assert settings.is_whitelisted(P2P_CHAT) is True
    assert settings.is_whitelisted(GROUP_CHAT) is False


def test_blacklist_still_wins_over_owner_chat(settings, monkeypatch):
    _enable_owner(settings, monkeypatch)
    monkeypatch.setattr(settings, "feishu_chat_blacklist", P2P_CHAT)
    assert settings.is_whitelisted(P2P_CHAT) is False


def test_owner_chat_p2p_message_is_admitted_and_processed(settings, monkeypatch):
    _enable_owner(settings, monkeypatch)
    from app.handlers.events import persist_event

    accepted, reason, should_process = persist_event(
        incoming("请问年假怎么申请", "om_owner_p2p_1", chat_id=P2P_CHAT, sender="ou_owner1")
    )
    assert accepted
    assert reason == "p2p_ok"
    assert should_process
    with session_scope() as session:
        assert session.query(Message).filter(Message.message_id == "om_owner_p2p_1").one()


def test_group_no_mention_is_only_ingested(settings, monkeypatch):
    """显式开启"必须 @"时的行为（默认已免 @，见 test_multi_user.py）。"""
    monkeypatch.setattr(settings, "feishu_chat_whitelist", GROUP_CHAT)
    monkeypatch.setattr(settings, "feishu_group_reply_only_mention", True)
    from app.domain.admission import ensure_source
    from app.handlers.events import persist_event

    with session_scope() as session:
        ensure_source(
            session, tenant=settings.tenant_id, owner="ou_owner", chat_id=GROUP_CHAT,
            source_type="group", reply_allowed=True,
        )
    accepted, reason, should_process = persist_event(
        incoming("大家好", "om_group_1", chat_id=GROUP_CHAT, chat_type="group",
                 mentions=["ou_someone"])
    )
    assert accepted
    assert reason == "group_no_mention_ingest_only"
    assert should_process is False


def test_daily_source_bootstrap_marks_owner_chat_as_p2p(settings, monkeypatch):
    _enable_owner(settings, monkeypatch)
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", P2P_CHAT)
    from app.pipeline.daily import bootstrap_sources
    from tests.helpers import FakeTransport

    transport = FakeTransport(chats=[])  # 没有 im:chat:readonly 时的真实情形
    with session_scope() as session:
        sources = bootstrap_sources(session, transport=transport)
        assert sources, "owner 白名单必须能登记来源"
        owner_source = next(s for s in sources if s.chat_id == P2P_CHAT)
        assert owner_source.source_type == "p2p"
        # 回复许可仍然只由 FEISHU_REPLY_ALLOWED_CHATS 显式授予（读不推导写）。
        assert owner_source.reply_allowed is True


def test_owner_chat_read_permission_does_not_imply_reply(settings, monkeypatch):
    _enable_owner(settings, monkeypatch)
    from app.pipeline.daily import bootstrap_sources
    from tests.helpers import FakeTransport

    with session_scope() as session:
        sources = bootstrap_sources(session, transport=FakeTransport(chats=[]))
        owner_source = next(s for s in sources if s.chat_id == P2P_CHAT)
        assert owner_source.reply_allowed is False


def test_owner_target_falls_back_to_chat_then_email(settings, monkeypatch):
    monkeypatch.setattr(settings, "feishu_owner_chat_id", P2P_CHAT)
    monkeypatch.setattr(settings, "feishu_owner_open_id", "")
    monkeypatch.setattr(settings, "feishu_owner_email", "owner@example.com")
    assert settings.owner_target() == ("chat_id", P2P_CHAT)

    monkeypatch.setattr(settings, "feishu_owner_chat_id", "")
    monkeypatch.setattr(settings, "feishu_owner_open_id", "ou_known_owner")
    assert settings.owner_target() == ("open_id", "ou_known_owner")

    monkeypatch.setattr(settings, "feishu_owner_open_id", "")
    assert settings.owner_target() == ("email", "owner@example.com")

    monkeypatch.setattr(settings, "feishu_owner_email", "")
    assert settings.owner_target() == ("open_id", "")


def test_receive_id_type_supports_email():
    from app.feishu.transport import receive_id_type_of

    assert receive_id_type_of("oc_x") == "chat_id"
    assert receive_id_type_of("ou_x") == "open_id"
    assert receive_id_type_of("om_x") == "message_id"
    assert receive_id_type_of("nagrax@example.com") == "email"


def test_daily_report_targets_owner_chat(settings, monkeypatch):
    _enable_owner(settings, monkeypatch)
    monkeypatch.setattr(settings, "feishu_owner_open_id", "")
    monkeypatch.setattr(settings, "feishu_owner_email", "")
    from app.domain import reports
    from app.models import DailyReport, Outbox, ReplyDraft

    with session_scope() as session:
        # 空日报不投递（2026-11-11 起），先造一条待批让报告有内容。
        # 待批裁剪按 Source 归属过滤，所以这里同时登记来源。
        from app.domain.admission import ensure_source

        ensure_source(
            session, tenant=settings.tenant_id, owner=P2P_CHAT, chat_id=P2P_CHAT,
            source_type="p2p", reply_allowed=True,
        )
        session.add(
            ReplyDraft(
                tenant=settings.tenant_id,
                message_id="om_daily_probe",
                chat_id=P2P_CHAT,
                draft_content="待批回复",
                status="pending",
                # _pending_drafts 只收 draft/shadow 两种 mode，默认值不是它
                mode="draft",
            )
        )
        session.flush()
        # 显式传 owner：这条测试验证的是"投递目标回退到 chat_id"。
        # 也显式传 logical_date，避免"今天的待批 vs 昨天的统计窗口"错位。
        from datetime import date

        result = reports.generate_report(
            session, owner=P2P_CHAT, logical_date=date.today(), enqueue_delivery=True
        )
        assert result.outbox_id, "有内容时必须入队投递"
        row = session.query(Outbox).filter(Outbox.id == result.outbox_id).one()
        assert row.kind == "daily"
        assert row.target_type == "chat_id"
        assert row.target_id == P2P_CHAT
        assert row.chat_id == P2P_CHAT
        report = session.query(DailyReport).one()
        assert report.status == "draft"
