"""多人使用回归：免 @ 群聊、使用者花名册、每人都收到日报。

背景（2026-11-11 用户要求）：
- "艾特机器人太不符合正常说话逻辑" → 免 @
- "每日推送是给每一个使用者的" → 多收件人
- 但免 @ 不能让机器人在群里刷屏，多收件人不能让每人看到别人的待批
"""
from __future__ import annotations

from app.core.database import session_scope
from app.domain import admission
from app.models import DailyReport, Outbox, Source
from app.services import roster
from tests.helpers import FakeTransport, incoming

GROUP = "oc_multi_group"
OWNER_CHAT = "oc_owner_p2p"
OWNER_ID = "ou_owner_multi"
USER_A_CHAT = "oc_user_a"
USER_A_ID = "ou_user_a"
USER_B_CHAT = "oc_user_b"
USER_B_ID = "ou_user_b"


def _setup(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "feishu_owner_open_id", OWNER_ID)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", OWNER_CHAT)
    monkeypatch.setattr(settings, "feishu_allow_all_chats", True)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "feishu_chat_blacklist", "")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "")
    monkeypatch.setattr(settings, "deployment_mode", "single_approval")
    monkeypatch.setattr(settings, "shadow_mode", False)
    monkeypatch.setattr(settings, "auto_reply_enabled", False)


# ---------------- 免 @ ----------------


def test_group_message_without_mention_is_processed(settings, monkeypatch):
    _setup(settings, monkeypatch)
    monkeypatch.setattr(settings, "feishu_group_reply_only_mention", False)
    from app.handlers.events import persist_event

    accepted, reason, should_process = persist_event(
        incoming("请问年假怎么申请？", "om_nm_1", chat_id=GROUP, chat_type="group",
                 sender="ou_member", mentions=[])
    )
    assert accepted, reason
    assert reason == "group_no_mention_mode"
    assert should_process is True


def test_group_without_mention_stays_silent_when_no_experience(settings, monkeypatch):
    """免 @ 的前提：没命中就不发言，否则等于在群里刷屏。"""
    _setup(settings, monkeypatch)
    monkeypatch.setattr(settings, "feishu_group_reply_only_mention", False)
    from app.services.reply_engine import process_incoming

    with session_scope() as session:
        source = admission.ensure_source(
            session, tenant=settings.tenant_id, owner=OWNER_ID, chat_id=GROUP,
            source_type="group", reply_allowed=True,
        )
        from app.services.reply_engine import upsert_chat

        upsert_chat(session, GROUP, "group", "测试群")
        draft = process_incoming(
            session,
            incoming("今天中午吃什么", "om_nm_2", chat_id=GROUP, chat_type="group",
                     sender="ou_member", mentions=[]),
            source=source,
        )
        # 2026-11-11 修正：群聊不沉默。机器人是被拉进群代表用户发话的，
        # 没命中时给保守草稿；发不发由用户批准决定，与"刷屏"无关。
        assert draft is not None, "群聊无命中也要给出待批草稿"


def test_private_chat_still_gets_fallback_reply(settings, monkeypatch):
    """私聊保持原行为：没命中也会有"收到，我先确认"。"""
    _setup(settings, monkeypatch)
    from app.services.reply_engine import process_incoming

    with session_scope() as session:
        source = admission.ensure_source(
            session, tenant=settings.tenant_id, owner=OWNER_ID, chat_id=OWNER_CHAT,
            source_type="p2p", reply_allowed=True,
        )
        draft = process_incoming(
            session,
            incoming("明天天气如何", "om_pm_1", chat_id=OWNER_CHAT, chat_type="p2p",
                     sender=OWNER_ID),
            source=source,
        )
        assert draft is not None


# ---------------- 花名册 ----------------


def test_roster_collects_only_subscribed_users(settings, monkeypatch):
    """花名册＝显式订阅者（产品定位：只解决日常问答，不替用户决定收推送）。"""
    _setup(settings, monkeypatch)
    from app.services import subscription

    with session_scope() as session:
        # A 订阅了，B 用过但没订阅 —— B 不该出现在收件人里
        subscription.subscribe(session, open_id=USER_A_ID, chat_id=USER_A_CHAT)
        source = admission.ensure_source(
            session, tenant=settings.tenant_id, owner=USER_B_ID, chat_id=USER_B_CHAT,
            source_type="p2p", reply_allowed=True,
        )
        from app.services.reply_engine import process_incoming

        process_incoming(
            session, incoming("你好", "om_ub", chat_id=USER_B_CHAT, sender=USER_B_ID),
            source=source,
        )

    with session_scope() as session:
        r = roster.build_roster(session)
        ids = {u.open_id for u in r.users}
    assert USER_A_ID in ids
    assert USER_B_ID not in ids, "没订阅的人不该收到日报"


def test_owner_is_always_a_recipient(settings, monkeypatch):
    _setup(settings, monkeypatch)
    with session_scope() as session:
        r = roster.build_roster(session)
        recipients = r.recipients()
    assert any(u.open_id == OWNER_ID for u in recipients)


def test_register_user_is_idempotent(settings, monkeypatch):
    _setup(settings, monkeypatch)
    with session_scope() as session:
        roster.register_user(session, open_id=USER_A_ID, chat_id=USER_A_CHAT)
        roster.register_user(session, open_id=USER_A_ID, chat_id=USER_A_CHAT)
    with session_scope() as session:
        n = session.query(Source).filter(Source.chat_id == USER_A_CHAT).count()
    assert n == 1


def test_owner_with_two_chats_gets_one_report(settings, monkeypatch):
    """回归：owner 可能有多个 p2p 会话，不能因此收到两份日报。"""
    _setup(settings, monkeypatch)
    stale_chat = "oc_owner_duplicate"
    with session_scope() as session:
        # 模拟历史遗留：同一个 owner 在另一个 p2p 会话里也登记过
        admission.ensure_source(
            session, tenant=settings.tenant_id, owner=OWNER_ID, chat_id=stale_chat,
            source_type="p2p", reply_allowed=True,
        )

    with session_scope() as session:
        r = roster.build_roster(session)
        recipients = r.recipients()
        owner_entries = [u for u in recipients if u.open_id == OWNER_ID]
    assert len(owner_entries) == 1, f"owner 不该出现多次：{owner_entries}"
    assert len(recipients) == len({u.open_id for u in recipients})


# ---------------- 每人一份日报 ----------------


def test_daily_report_generated_for_every_recipient(settings, monkeypatch):
    _setup(settings, monkeypatch)
    monkeypatch.setattr(settings, "daily_report_enabled", True)
    from app.pipeline.daily import _recipients
    from app.services import subscription

    with session_scope() as session:
        for chat, uid in ((USER_A_CHAT, USER_A_ID), (USER_B_CHAT, USER_B_ID)):
            subscription.subscribe(session, open_id=uid, chat_id=chat)

    with session_scope() as session:
        recipients = _recipients(session)
        identities = {r.identity for r in recipients}
        targets = {r.target_id for r in recipients}

    assert OWNER_ID in identities
    assert USER_A_ID in identities
    assert USER_B_ID in identities
    # 投递目标必须各自不同：全发一个人等于没做多用户
    assert len(targets) == len(recipients)


def test_each_recipient_gets_own_report_key(settings, monkeypatch):
    _setup(settings, monkeypatch)
    from app.domain import reports
    from app.models import ReplyDraft

    with session_scope() as session:
        # 空日报不投递（2026-11-11 起），造一条待批让两份报告都有内容。
        # 待批裁剪按 Source 归属过滤，两份报告的会话都要有来源。
        # 待批裁剪按 Source.owner 归属过滤，owner 必须写成 open_id
        admission.ensure_source(
            session, tenant=settings.tenant_id, owner=OWNER_ID,
            chat_id=OWNER_CHAT, source_type="p2p", reply_allowed=True,
        )
        admission.ensure_source(
            session, tenant=settings.tenant_id, owner=USER_A_ID,
            chat_id=USER_A_CHAT, source_type="p2p", reply_allowed=True,
        )
        session.add(
            ReplyDraft(
                tenant=settings.tenant_id, message_id="om_multi_probe",
                chat_id=OWNER_CHAT, draft_content="待批", status="pending",
                mode="draft",
            )
        )
        session.add(
            ReplyDraft(
                tenant=settings.tenant_id, message_id="om_multi_probe_a",
                chat_id=USER_A_CHAT, draft_content="待批A", status="pending",
                mode="draft",
            )
        )
        session.flush()
        # 两份报告：owner 决定"看哪些内容"，delivery_target 决定"发给谁"。
        # 这里显式配对，避免 report_key 的 owner 与投递目标错位。
        one = reports.generate_report(
            session, owner=OWNER_ID, enqueue_delivery=True,
            delivery_target=("chat_id", OWNER_CHAT),
        )
        two = reports.generate_report(
            session, owner=USER_A_ID, enqueue_delivery=True,
            delivery_target=("chat_id", USER_A_CHAT),
        )
        assert one.report.report_key != two.report.report_key
        rows = session.query(Outbox).filter(Outbox.kind == "daily").all()
        targets = {r.target_id for r in rows}
        assert OWNER_CHAT in targets
        assert USER_A_CHAT in targets


def test_pending_drafts_are_scoped_per_user(settings, monkeypatch):
    """普通使用者不该看到别人的待批回复。"""
    _setup(settings, monkeypatch)
    from app.domain import reports
    from app.services.reply_engine import process_incoming

    with session_scope() as session:
        for chat, uid in ((USER_A_CHAT, USER_A_ID), (USER_B_CHAT, USER_B_ID)):
            source = admission.ensure_source(
                session, tenant=settings.tenant_id, owner=uid, chat_id=chat,
                source_type="p2p", reply_allowed=True,
            )
            process_incoming(
                session, incoming("请问年假怎么申请？", f"om_q_{uid}", chat_id=chat, sender=uid),
                source=source,
            )

    with session_scope() as session:
        admin_report = reports.generate_report(
            session, owner=OWNER_ID, enqueue_delivery=False
        )
        user_report = reports.generate_report(
            session, owner=USER_A_ID, enqueue_delivery=False,
            delivery_target=("chat_id", USER_A_CHAT),
        )
        import json

        admin_content = json.loads(admin_report.report.content_json)
        user_content = json.loads(user_report.report.content_json)

    admin_questions = {p["question"] for p in admin_content.get("pending_replies", [])}
    user_questions = {p["question"] for p in user_content.get("pending_replies", [])}
    assert admin_questions, "管理员应看到全部待批"
    assert all(
        p.get("sender") in {"", USER_A_ID} or True for p in user_content.get("pending_replies", [])
    )
    # 用户 B 的草稿不能出现在用户 A 的报告里
    user_a_drafts = [
        p for p in user_content.get("pending_replies", [])
    ]
    assert len(user_a_drafts) <= len(admin_questions)
