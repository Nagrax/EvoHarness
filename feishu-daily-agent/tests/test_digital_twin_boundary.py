"""数字分身的授权边界（用户明确要求，不允许放宽）。

规则：
1. **只有本人主动私聊过机器人**，才可能被登记为使用者 / 自动开通日报；
2. **绝不允许**因为机器人被拉进某个群，就推断"群里每个人都同意
   用这个机器人当自己的数字分身"；
3. 群里出现的任何发送者（含首次出现的人）都不会被自动登记身份。

为什么这条这么硬：数字分身会以本人名义对外发言，属于代表人格的行为，
必须由本人显式授权（OAuth 或主动私聊），不能由"在同一个群"推导出来。
"""
from __future__ import annotations

from app.core.database import session_scope
from app.models import Source
from app.services import subscription
from tests.helpers import incoming

OWNER_ID = "ou_lead"
OWNER_CHAT = "oc_lead"
GROUP = "oc_team_group"
STRANGER = "ou_group_stranger"


def _setup(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "feishu_owner_open_id", OWNER_ID)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", OWNER_CHAT)
    monkeypatch.setattr(settings, "feishu_allow_all_chats", True)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "")
    monkeypatch.setattr(settings, "feishu_group_reply_only_mention", False)
    monkeypatch.setattr(settings, "deployment_mode", "single_approval")
    monkeypatch.setattr(settings, "shadow_mode", False)


def test_group_member_is_never_auto_enrolled(settings, monkeypatch):
    """群里有人说话，绝不自动给他开通日报 / 登记为使用者。"""
    _setup(settings, monkeypatch)
    from app.handlers.events import persist_event

    accepted, _reason, _process = persist_event(
        incoming("请问年假怎么申请？", "om_grp_stranger", chat_id=GROUP,
                 chat_type="group", sender=STRANGER, mentions=[])
    )
    assert accepted, "群消息本身仍要能处理（机器人代表群主在群里答话）"
    with session_scope() as session:
        # 关键断言：陌生群成员没有被登记为使用者
        assert not subscription.is_subscribed(session, STRANGER), (
            "群成员不能因为群里有机器人就被开通日报"
        )
        rows = session.query(Source).filter(Source.owner == STRANGER).all()
        assert not rows, "群成员不能被自动登记为独立使用者"


def test_only_private_contact_enrolls(settings, monkeypatch):
    """主动私聊才登记 —— 这是唯一合法入口。"""
    _setup(settings, monkeypatch)
    from app.handlers.events import persist_event

    persist_event(
        incoming("你好", "om_p2p_first", chat_id="oc_member_p2p",
                 chat_type="p2p", sender="ou_member_x")
    )
    with session_scope() as session:
        assert subscription.is_subscribed(session, "ou_member_x")
        rows = session.query(Source).filter(Source.owner == "ou_member_x").all()
        assert rows, "私聊过的本人才登记"


def test_group_thread_message_does_not_enroll(settings, monkeypatch):
    """群里的子线程消息同样不触发身份开通。"""
    _setup(settings, monkeypatch)
    from app.handlers.events import persist_event

    persist_event(
        incoming("话题里问一句", "om_grp_thread", chat_id=GROUP,
                 chat_type="group", sender="ou_thread_user",
                 thread_id="omt_thread_1")
    )
    with session_scope() as session:
        assert not subscription.is_subscribed(session, "ou_thread_user")
        assert not session.query(Source).filter(
            Source.owner == "ou_thread_user"
        ).all()


def test_bot_never_enrolls_itself(settings, monkeypatch):
    _setup(settings, monkeypatch)
    from app.handlers.events import persist_event

    persist_event(
        incoming("回声", "om_echo", chat_id="oc_p2p_echo", chat_type="p2p",
                 sender="ou_bot_self", sender_type="app")
    )
    with session_scope() as session:
        assert not subscription.is_subscribed(session, "ou_bot_self")


def test_roster_contains_no_group_only_members(settings, monkeypatch):
    """花名册里不能出现"只在群里露过面"的人。"""
    _setup(settings, monkeypatch)
    from app.handlers.events import persist_event
    from app.services import roster

    persist_event(
        incoming("我在群里问一句", "om_grp_only", chat_id=GROUP,
                 chat_type="group", sender="ou_group_only")
    )
    with session_scope() as session:
        users = {u.open_id for u in roster.build_roster(session).recipients()}
    assert "ou_group_only" not in users
    assert OWNER_ID in users, "管理员仍是收件人（运维需要）"
