"""数字分身归属回归：分身按「人」划分，不按「群」划分。

用户明确纠正（2026-11-11，第三次）：
**机器人是用户的数字分身。群里出现机器人，只是为了让分身能代替它自己的
主人在群里说话。所以一切使用该机器人的用户都拥有自己的批准权，而不是只有
把机器人拉进群的人才有批准权。**

因此：
- 同一个群里可以有多个人的分身，各有一条来源、各批各的；
- 谁问的，若他本人在该群有分身，就归他；
- 群里只有一个分身时无歧义归他；
- 多个分身且无法判定时归管理员兜底（不能错批别人的话）。
"""
from __future__ import annotations

from app.core.database import session_scope
from app.domain import admission
from app.models import ReplyDraft, Source
from app.services import reply_engine
from tests.helpers import incoming

OWNER_ID = "ou_lead"
OWNER_CHAT = "oc_lead"
ALICE = "ou_alice"
BOB = "ou_bob"
GROUP = "oc_shared_group"


def _setup(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "feishu_owner_open_id", OWNER_ID)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", OWNER_CHAT)
    monkeypatch.setattr(settings, "feishu_allow_all_chats", True)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "")
    monkeypatch.setattr(settings, "feishu_group_reply_only_mention", False)
    monkeypatch.setattr(settings, "deployment_mode", "single_approval")
    monkeypatch.setattr(settings, "shadow_mode", False)
    monkeypatch.setattr(settings, "auto_reply_enabled", False)


def _add_twin(session, settings, open_id: str, *, chat_id: str = GROUP,
              source_type: str = "group") -> Source:
    """为一个使用者在该群登记分身来源。"""
    return admission.ensure_source(
        session, tenant=settings.tenant_id, owner=open_id, chat_id=chat_id,
        source_type=source_type, reply_allowed=True,
    )


def _draft_for(settings, *, chat_id: str, sender: str, chat_type: str,
               source_id: int | None = None) -> int:
    with session_scope() as session:
        source = (
            session.get(Source, source_id) if source_id
            else _add_twin(session, settings, sender, chat_id=chat_id,
                           source_type=chat_type)
        )
        reply_engine.upsert_chat(session, chat_id, chat_type, "t")
        draft = reply_engine.process_incoming(
            session,
            incoming("请问年假怎么申请？", f"om_{sender}_{chat_id}",
                     chat_id=chat_id, chat_type=chat_type, sender=sender),
            source=source,
        )
        assert draft is not None, "应产出待批草案"
        return draft.id


# ---------------- 同一群允许多个分身 ----------------


def test_same_group_allows_multiple_twins(settings, monkeypatch):
    """核心：一个群里可以有多个人的分身，各有独立来源。"""
    _setup(settings, monkeypatch)
    with session_scope() as session:
        a = _add_twin(session, settings, ALICE)
        b = _add_twin(session, settings, BOB)
        assert a.id != b.id, "两个分身必须是两条独立来源"
    with session_scope() as session:
        rows = session.query(Source).filter(Source.chat_id == GROUP).all()
        owners = {r.owner for r in rows}
    assert owners == {ALICE, BOB}


def test_repeated_registration_is_idempotent(settings, monkeypatch):
    _setup(settings, monkeypatch)
    with session_scope() as session:
        first = _add_twin(session, settings, ALICE)
        again = _add_twin(session, settings, ALICE)
        assert first.id == again.id


# ---------------- 归属判定 ----------------


def test_sender_who_owns_a_twin_gets_the_draft(settings, monkeypatch):
    """谁问的、且他本人在该群有分身 → 归他批。"""
    _setup(settings, monkeypatch)
    with session_scope() as session:
        _add_twin(session, settings, ALICE)
        bob_source = _add_twin(session, settings, BOB)
        bob_source_id = bob_source.id

    draft_id = _draft_for(
        settings, chat_id=GROUP, sender=BOB, chat_type="group", source_id=bob_source_id
    )
    with session_scope() as session:
        draft = session.get(ReplyDraft, draft_id)
        assert draft.approver_id == BOB, "BOB 自己有分身，就该归 BOB"


def test_single_twin_group_assigns_to_that_twin(settings, monkeypatch):
    """群里只有一个分身 → 无歧义，归他。"""
    _setup(settings, monkeypatch)
    draft_id = _draft_for(settings, chat_id=GROUP, sender="ou_someone_else",
                          chat_type="group")
    # 上面 _draft_for 用 sender 建了分身；这里单独构造"只有 ALICE 有分身"的场景
    with session_scope() as session:
        session.query(Source).filter(Source.chat_id == GROUP).delete()
        _add_twin(session, settings, ALICE)
        session.flush()
        from app.models import Message

        message = session.query(Message).filter(
            Message.message_id == f"om_ou_someone_else_{GROUP}"
        ).one()
        approver = reply_engine._approver_for(session, message)
    assert approver == ALICE


def test_ambiguous_multi_twin_group_falls_back_to_admin(settings, monkeypatch):
    """多个分身且提问者不是其中之一 → 无法判定，归管理员兜底。

    宁可让人工多看一眼，也不能把别人的话错误地分给某个分身去批。
    """
    _setup(settings, monkeypatch)
    with session_scope() as session:
        alice_src = _add_twin(session, settings, ALICE)
        _add_twin(session, settings, BOB)
        alice_src_id = alice_src.id

    # 用 ALICE 的分身来源处理"陌生人"的提问：提问者本人不在分身集合里
    with session_scope() as session:
        source = session.get(Source, alice_src_id)
        reply_engine.upsert_chat(session, GROUP, "group", "t")
        draft = reply_engine.process_incoming(
            session,
            incoming("请问年假怎么申请？", "om_outsider_q", chat_id=GROUP,
                     chat_type="group", sender="ou_outsider"),
            source=source,
        )
        assert draft is not None
        draft_id = draft.id

    with session_scope() as session:
        draft = session.get(ReplyDraft, draft_id)
        assert draft.approver_id == OWNER_ID, "歧义场景应退回管理员"


def test_private_chat_always_belongs_to_speaker(settings, monkeypatch):
    """私聊是某人的分身入口，归本人。"""
    _setup(settings, monkeypatch)
    draft_id = _draft_for(settings, chat_id="oc_alice_p2p", sender=ALICE,
                          chat_type="p2p")
    with session_scope() as session:
        draft = session.get(ReplyDraft, draft_id)
        assert draft.approver_id == ALICE


def test_each_twin_can_approve_own_draft(settings, monkeypatch):
    """两句话分属两个分身，各批各的，互不干扰。"""
    _setup(settings, monkeypatch)
    from app.domain import review

    with session_scope() as session:
        alice_src = _add_twin(session, settings, ALICE)
        bob_src = _add_twin(session, settings, BOB)
        alice_src_id, bob_src_id = alice_src.id, bob_src.id

    a_draft = _draft_for(settings, chat_id=GROUP, sender=ALICE,
                         chat_type="group", source_id=alice_src_id)
    b_draft = _draft_for(settings, chat_id=GROUP, sender=BOB,
                         chat_type="group", source_id=bob_src_id)

    with session_scope() as session:
        assert session.get(ReplyDraft, a_draft).approver_id == ALICE
        assert session.get(ReplyDraft, b_draft).approver_id == BOB

        token_a = review.create_action_token(
            session, owner=ALICE, action="approve", target_type="draft",
            target_id=a_draft, target_version=1,
        )
        outcome = review.handle_card_action(
            session, action_id=token_a.action_id, operator_id=ALICE
        )
        assert outcome.status in {"queued", "approved"}

        # BOB 不能碰 ALICE 的草稿
        token_a2 = review.create_action_token(
            session, owner=ALICE, action="reject", target_type="draft",
            target_id=a_draft, target_version=1,
        )
        try:
            review.handle_card_action(
                session, action_id=token_a2.action_id, operator_id=BOB
            )
        except review.ReviewError:
            pass
        else:
            raise AssertionError("BOB 不应能操作 ALICE 的草稿")
