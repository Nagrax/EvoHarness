"""端到端闭环集成测试：离线 mock LLM 与假飞书传输。

保留原型六例的覆盖：
1. 无经验问题 → 待批草稿
2. 批准 → 发送 + 沉淀候选
3. 已验证经验 → 自动回复
4. 驳回 → 经验停用
5. 非问题消息 → 不产生草稿
6. 幂等处理
"""
from __future__ import annotations

import pytest

from app.core.database import session_scope
from app.memory.experience import create_candidate, publish
from app.models import (
    Category,
    DraftMode,
    DraftStatus,
    Experience,
    ExperienceStatus,
    Message,
    Outbox,
    ReplyDraft,
)
from app.services import reply_engine
from tests.helpers import FakeTransport, incoming


def answer_for(experience) -> str:
    return "基于经验的回复"


@pytest.fixture(autouse=True)
def _patch_llm(monkeypatch):
    monkeypatch.setattr(
        reply_engine,
        "classify_message",
        lambda t: {"category": Category.QUESTION, "summary": "", "due": ""},
    )
    monkeypatch.setattr(reply_engine, "generate_fallback_reply", lambda q: "新拟回复内容")
    monkeypatch.setattr(reply_engine, "compose_reply", lambda q, e: answer_for(e))


def _process(source, text: str, mid: str, **kwargs):
    with session_scope() as session:
        return reply_engine.process_incoming(
            session, incoming(text, mid, **kwargs), source=source
        )


def test_question_without_experience_becomes_pending_draft(source, approval_mode):
    draft = _process(source, "请问年假怎么申请", "om_1")
    assert draft.status == DraftStatus.PENDING
    assert draft.mode == DraftMode.DRAFT
    assert draft.experience_id is None
    assert draft.draft_content == "新拟回复内容"


def test_approve_sends_and_sedates_new_experience(source, approval_mode):
    draft = _process(source, "请问年假怎么申请", "om_1")
    with session_scope() as session:
        reply_engine.approve_draft(session, draft.id)
    transport = FakeTransport()
    reply_engine.dispatch_pending(transport=transport)
    with session_scope() as session:
        saved = session.get(ReplyDraft, draft.id)
        assert saved.status == DraftStatus.SENT
        assert saved.sent_message_id == "om_sent_1"
        experiences = session.query(Experience).all()
        assert len(experiences) == 1
        assert experiences[0].status == ExperienceStatus.CANDIDATE
        assert len(transport.replies) == 1


def test_verified_experience_triggers_auto_reply(source, auto_mode, monkeypatch):
    monkeypatch.setattr(auto_mode, "auto_reply_threshold", 0.1)
    monkeypatch.setattr(auto_mode, "draft_min_threshold", 0.05)
    with session_scope() as session:
        exp = create_candidate(
            session,
            question="请问年假怎么申请",
            answer="基于经验的回复",
            patterns=["请问年假怎么申请", "年假申请流程"],
            visibility="public",
            sensitive_level="internal",
            audience={"chat_types": ["p2p"]},
        )
        publish(session, exp.id, actor="ou_owner")
        session.flush()
        exp_id = exp.id

    draft = _process(source, "年假怎么申请呢", "om_2")
    assert draft.mode == DraftMode.AUTO
    assert draft.status == DraftStatus.APPROVED
    assert draft.experience_id == exp_id
    transport = FakeTransport()
    reply_engine.dispatch_pending(transport=transport)
    with session_scope() as session:
        saved = session.get(ReplyDraft, draft.id)
        assert saved.status == DraftStatus.SENT
        assert saved.sent_message_id == "om_sent_1"
    assert transport.replies == [("om_2", "基于经验的回复", "om_sent_1")]


def test_reject_records_and_can_disable_experience(source, monkeypatch, settings, approval_mode):
    monkeypatch.setattr(settings, "experience_dismiss_limit", 1)
    monkeypatch.setattr(settings, "draft_min_threshold", 0.05)
    monkeypatch.setattr(settings, "auto_reply_threshold", 0.99)
    with session_scope() as session:
        exp = create_candidate(
            session,
            question="请问年假怎么申请",
            answer="基于经验的回复",
            patterns=["请问年假怎么申请", "年假申请流程"],
            visibility="public",
            sensitive_level="internal",
            audience={"chat_types": ["p2p"]},
        )
        publish(session, exp.id, actor="ou_owner")
        exp_id = exp.id

    draft = _process(source, "年假如何申请", "om_2")
    assert draft.experience_id == exp_id
    with session_scope() as session:
        reply_engine.reject_draft(session, draft.id, reason="回答不准确")
        saved = session.get(Experience, exp_id)
        assert saved.status == ExperienceStatus.PAUSED
        assert saved.reject_count == 1


def test_non_question_message_is_ingested_without_draft(source, monkeypatch):
    monkeypatch.setattr(
        reply_engine,
        "classify_message",
        lambda t: {"category": Category.TASK, "summary": "交材料", "due": ""},
    )
    draft = _process(source, "记得周五前交材料", "om_9")
    assert draft is None
    with session_scope() as session:
        assert session.query(ReplyDraft).count() == 0
        assert session.query(Message).count() == 1


def test_idempotent_processing(source):
    first = _process(source, "请问年假怎么申请", "om_1")
    again = _process(source, "请问年假怎么申请", "om_1")
    with session_scope() as session:
        assert session.query(Message).count() == 1
        assert again is not None
        assert again.id == first.id
        assert session.query(Outbox).count() == 0
