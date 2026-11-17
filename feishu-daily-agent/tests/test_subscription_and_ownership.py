"""订阅门槛与归属权回归。

产品定位（2026-11-11 用户明确）：
- 只解决日常问答，日报只发给**真实确认使用**的人；
- 谁确认使用机器人，机器人负责**他的全部工作消息**，批准权也归他，
  不按"提问者/被提问者"拆分——一次协助不是一次授权。
"""
from __future__ import annotations

from app.core.database import session_scope
from app.domain import admission, review
from app.models import ActionToken, ReplyDraft, Source
from app.services import reply_engine, subscription
from tests.helpers import incoming

OWNER_ID = "ou_lead"
OWNER_CHAT = "oc_lead"
USER_A = "ou_member_a"
CHAT_A = "oc_member_a"
GROUP = "oc_team_group"


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


# ---------------- 订阅门槛 ----------------


def test_not_subscribed_before_first_contact(settings, monkeypatch):
    _setup(settings, monkeypatch)
    with session_scope() as session:
        assert subscription.is_subscribed(session, USER_A) is False


def test_auto_enroll_on_first_private_contact(settings, monkeypatch):
    """愿意私聊的人就是愿意收日报的人：首次私聊自动开通并返回"需告知"。"""
    _setup(settings, monkeypatch)
    with session_scope() as session:
        need_notice = subscription.auto_enroll(
            session, open_id=USER_A, chat_id=CHAT_A
        )
        assert need_notice is True, "首次应提示一次"
        assert subscription.is_subscribed(session, USER_A) is True
        # 第二次不再提示
        assert subscription.auto_enroll(
            session, open_id=USER_A, chat_id=CHAT_A
        ) is False


def test_unsubscribe_is_remembered(settings, monkeypatch):
    """退订后不能被自动加回来（尊重明确拒绝）。"""
    _setup(settings, monkeypatch)
    with session_scope() as session:
        subscription.auto_enroll(session, open_id=USER_A, chat_id=CHAT_A)
        assert subscription.unsubscribe(session, open_id=USER_A) is True
        assert subscription.is_subscribed(session, USER_A) is False
        # 再次私聊也不自动恢复
        assert subscription.auto_enroll(
            session, open_id=USER_A, chat_id=CHAT_A
        ) is False
        assert subscription.is_subscribed(session, USER_A) is False


def test_resubscribe_clears_opt_out(settings, monkeypatch):
    _setup(settings, monkeypatch)
    with session_scope() as session:
        subscription.auto_enroll(session, open_id=USER_A, chat_id=CHAT_A)
        subscription.unsubscribe(session, open_id=USER_A)
        subscription.subscribe(session, open_id=USER_A, chat_id=CHAT_A)
        assert subscription.has_opted_out(session, USER_A) is False
        assert subscription.is_subscribed(session, USER_A) is True


def test_subscribe_command_reply(settings, monkeypatch):
    _setup(settings, monkeypatch)
    with session_scope() as session:
        text = subscription.handle_subscribe_command(
            session, text="订阅", open_id=USER_A, chat_id=CHAT_A
        )
        assert text and "日报" in text
        off = subscription.handle_subscribe_command(
            session, text="退订", open_id=USER_A, chat_id=CHAT_A
        )
        assert off and "已退订" in off


def test_non_subscribe_text_is_not_a_command(settings, monkeypatch):
    _setup(settings, monkeypatch)
    with session_scope() as session:
        assert subscription.handle_subscribe_command(
            session, text="订阅一下年假政策", open_id=USER_A, chat_id=CHAT_A
        ) is None, "不能把长句误判成订阅指令"


# ---------------- 批准归属 ----------------


def _make_draft(settings, *, chat_id: str, sender: str, chat_type: str = "p2p") -> int:
    """造一条待批草稿，返回 id。"""
    with session_scope() as session:
        source = admission.ensure_source(
            session, tenant=settings.tenant_id, owner=sender, chat_id=chat_id,
            source_type=chat_type, reply_allowed=True,
        )
        chat = reply_engine.upsert_chat(session, chat_id, chat_type, "t")
        draft = reply_engine.process_incoming(
            session,
            incoming("请问年假怎么申请？", f"om_{chat_id}", chat_id=chat_id,
                     chat_type=chat_type, sender=sender),
            source=source,
        )
        assert draft is not None, "应产出待批草稿"
        return draft.id


def test_private_draft_belongs_to_that_user(settings, monkeypatch):
    """私聊归本人：谁用这个机器人，谁批自己的工作消息。"""
    _setup(settings, monkeypatch)
    draft_id = _make_draft(settings, chat_id=CHAT_A, sender=USER_A)
    with session_scope() as session:
        draft = session.get(ReplyDraft, draft_id)
        assert draft.approver_id == USER_A


def test_group_draft_belongs_to_using_user_not_group_member(settings, monkeypatch):
    """群里的待批仍归**真实使用机器人的用户**，不归群成员。

    机器人是象征用户的（代替他在群里回答），而不是代表群聊本身。
    所以群消息产生的待批，批准权属于把机器人拉进群的那个使用者。
    """
    _setup(settings, monkeypatch)
    # 机器人由 USER_A 拉进群：该群来源的 owner 是 USER_A
    draft_id = _make_draft(
        settings, chat_id=GROUP, sender=USER_A, chat_type="group"
    )
    with session_scope() as session:
        draft = session.get(ReplyDraft, draft_id)
        assert draft.approver_id == USER_A, "群里的待批归使用机器人的用户"
        assert draft.approver_id != "ou_someone_in_group", "不归群成员"


def test_owner_of_draft_can_approve(settings, monkeypatch):
    """被授权的使用者能批准自己的草稿（这是本次修复的核心）。"""
    _setup(settings, monkeypatch)
    draft_id = _make_draft(settings, chat_id=CHAT_A, sender=USER_A)

    with session_scope() as session:
        token = review.create_action_token(
            session, owner=USER_A, action="approve", target_type="draft",
            target_id=draft_id, target_version=1,
        )
        outcome = review.handle_card_action(
            session, action_id=token.action_id, operator_id=USER_A
        )
        assert outcome.status in {"queued", "approved"}


def test_other_user_cannot_approve(settings, monkeypatch):
    """别人不能批你的草稿：错误答案流出去会被沉淀成经验，污染整条链。"""
    _setup(settings, monkeypatch)
    draft_id = _make_draft(settings, chat_id=CHAT_A, sender=USER_A)

    with session_scope() as session:
        token = review.create_action_token(
            session, owner=USER_A, action="approve", target_type="draft",
            target_id=draft_id, target_version=1,
        )
        try:
            review.handle_card_action(
                session, action_id=token.action_id, operator_id="ou_stranger"
            )
        except review.ReviewError as exc:
            assert "operator_mismatch" in str(exc)
        else:
            raise AssertionError("陌生人不应能批准")


def test_admin_can_always_approve(settings, monkeypatch):
    """管理员兜底：成员离职/失效时任务不能卡死。"""
    _setup(settings, monkeypatch)
    draft_id = _make_draft(settings, chat_id=CHAT_A, sender=USER_A)

    with session_scope() as session:
        token = review.create_action_token(
            session, owner=USER_A, action="approve", target_type="draft",
            target_id=draft_id, target_version=1,
        )
        outcome = review.handle_card_action(
            session, action_id=token.action_id, operator_id=OWNER_ID
        )
        assert outcome.status in {"queued", "approved"}


# ---------------- 驳回＝教学信号 ----------------


def test_reject_archives_question_and_reason(settings, monkeypatch):
    """没有经验来源的草稿被驳回时，也必须留下"哪一问被否、为什么"。"""
    _setup(settings, monkeypatch)
    draft_id = _make_draft(settings, chat_id=CHAT_A, sender=USER_A)

    with session_scope() as session:
        review.reject_draft_direct(
            session, draft_id, actor=USER_A, reason="答案是编的，年假不是 5 天"
        )

    from app.models import ExperienceEvent

    with session_scope() as session:
        archived = [
            e for e in session.query(ExperienceEvent).all()
            if e.action == "reject_archived"
        ]
        assert archived, "驳回必须归档，否则纠错信号丢失"
        detail = archived[-1].detail
        assert "年假" in detail
        assert "答案是编的" in detail, "驳回理由要能追溯"
