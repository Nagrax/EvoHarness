"""F05：版本化经验、受众、负例、发布/回滚与 EvoHarness 检索接缝。"""
from __future__ import annotations

import json
from datetime import timedelta

from app.core.clock import utcnow
from app.core.database import session_scope
from app.domain import admission
from app.memory import experience as experience_module
from app.memory import retrieval
from app.models import (
    DraftStatus,
    Experience,
    ExperienceHead,
    ExperienceStatus,
    Outbox,
    ReplyDraft,
    Source,
)


def test_versioned_publish_supersede_and_rollback():
    with session_scope() as session:
        v1 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v1 答案",
            visibility="public", sensitive_level="internal",
        )
        experience_module.publish(session, v1.id, actor="ou_owner")
        v2 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v2 答案",
            intent_id=v1.intent_id, previous_id=v1.id,
            visibility="public", sensitive_level="internal",
            reason="答案更新",
        )
        experience_module.publish(session, v2.id, actor="ou_owner")
        assert session.get(Experience, v1.id).status == ExperienceStatus.SUPERSEDED
        assert session.get(ExperienceHead, v1.intent_id).experience_id == v2.id
        restored = experience_module.rollback(
            session, v1.intent_id, actor="ou_owner", reason="回滚"
        )
        assert restored.id == v1.id
        assert session.get(Experience, v1.id).status == ExperienceStatus.APPROVED_AUTO
        assert session.get(Experience, v2.id).status == ExperienceStatus.PAUSED


def test_second_rollback_is_noop_and_does_not_pause_head():
    with session_scope() as session:
        v1 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v1",
            visibility="public", sensitive_level="internal",
        )
        experience_module.publish(session, v1.id, actor="ou_owner")
        v2 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v2",
            intent_id=v1.intent_id, previous_id=v1.id,
            visibility="public", sensitive_level="internal",
        )
        experience_module.publish(session, v2.id, actor="ou_owner")
        experience_module.rollback(session, v1.intent_id, actor="ou_owner")
        head = session.get(ExperienceHead, v1.intent_id)
        assert head.experience_id == v1.id
        again = experience_module.rollback(session, v1.intent_id, actor="ou_owner")
        assert again is None
        assert session.get(ExperienceHead, v1.intent_id).experience_id == v1.id
        assert session.get(Experience, v1.id).status == ExperienceStatus.APPROVED_AUTO


def test_candidate_does_not_displace_published_head():
    with session_scope() as session:
        v1 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v1",
            visibility="public", sensitive_level="internal",
        )
        experience_module.publish(session, v1.id, actor="ou_owner")
        v2 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v2-candidate",
            intent_id=v1.intent_id, previous_id=v1.id,
            visibility="public", sensitive_level="internal",
        )
        head = session.get(ExperienceHead, v1.intent_id)
        assert head.experience_id == v1.id
        assert session.get(Experience, v2.id).status == ExperienceStatus.CANDIDATE


def test_private_experience_not_used_in_group():
    with session_scope() as session:
        group = admission.ensure_source(
            session, tenant="default", owner="ou_owner", chat_id="oc_group",
            source_type="group", reply_allowed=True,
        )
        exp = experience_module.create_candidate(
            session, question="年假怎么申请", answer="私人答案",
            visibility="private", audience={"chat_ids": ["oc_other"]},
        )
        session.flush()
        result = experience_module.find_best_match(
            session, "年假怎么申请", source=group
        )
        assert result.experience is None or result.experience.id != exp.id


def test_public_experience_audience_match():
    with session_scope() as session:
        group = admission.ensure_source(
            session, tenant="default", owner="ou_owner", chat_id="oc_group",
            source_type="group", reply_allowed=True,
        )
        exp = experience_module.create_candidate(
            session, question="年假怎么申请", answer="公开答案",
            visibility="public", sensitive_level="internal",
            audience={"chat_types": ["group"]},
        )
        session.flush()
        result = experience_module.find_best_match(
            session, "年假怎么申请", source=group
        )
        assert result.experience is not None
        assert result.experience.id == exp.id


def test_negative_example_blocks_high_similarity_wrong_intent(source, monkeypatch, settings):
    monkeypatch.setattr(settings, "negative_example_threshold", 0.1)
    monkeypatch.setattr(settings, "draft_min_threshold", 0.0)
    with session_scope() as session:
        db_source = session.get(Source, source.id)
        experience_module.create_candidate(
            session, question="年假怎么申请", answer="年假流程",
            patterns=["年假怎么申请", "年假流程"],
            negative_examples=["年假报销怎么申请"],
            visibility="public", sensitive_level="internal",
            audience={"chat_types": ["p2p"]},
        )
        session.flush()
        result = experience_module.find_best_match(
            session, "年假报销怎么申请", source=db_source
        )
        assert "negative_example_conflict" in result.reasons


def test_correction_creates_candidate_and_keeps_receipt(source, approval_mode):
    from app.services import reply_engine
    from tests.helpers import incoming

    with session_scope() as session:
        draft = reply_engine.process_incoming(
            session, incoming("请问年假怎么申请", "om_c"), source=source
        )
        draft.sent_message_id = "om_already"
        draft.status = DraftStatus.SENT
        previous = experience_module.create_candidate(
            session, question="请问年假怎么申请", answer="旧答案",
            visibility="public", sensitive_level="internal", category="faq",
            patterns=["请问年假怎么申请"], audience={"chat_types": ["p2p"]},
            preconditions="需要在职", steps="1. 打开 OA 2. 提交", allowed_links="https://example.com/leave",
        )
        experience_module.publish(session, previous.id, actor="ou_owner")
        draft.experience_id = previous.id
        session.flush()
        candidate = experience_module.correct_sent_reply(
            session, draft_id=draft.id, corrected_answer="纠正后的答案",
            actor="ou_owner", reason="owner 指令纠正", negative_examples=["错误问法"],
        )
        assert candidate is not None
        assert candidate.status == ExperienceStatus.CANDIDATE
        assert candidate.visibility == "public"
        assert candidate.category == "faq"
        assert candidate.sensitive_level == "internal"
        assert candidate.preconditions == "需要在职"
        assert candidate.steps == "1. 打开 OA 2. 提交"
        assert candidate.allowed_links == "https://example.com/leave"
        assert session.get(ReplyDraft, draft.id).sent_message_id == "om_already"


def test_owner_correct_command_pauses_experience_and_creates_candidate(source, approval_mode):
    from app.handlers.commands import try_handle_command
    from app.services import reply_engine
    from tests.helpers import incoming

    with session_scope() as session:
        draft = reply_engine.process_incoming(
            session, incoming("请问年假怎么申请", "om_cmd_correct"), source=source
        )
        draft.sent_message_id = "om_cmd_receipt"
        draft.status = DraftStatus.SENT
        previous = experience_module.create_candidate(
            session, question="请问年假怎么申请", answer="旧答案",
            visibility="public", sensitive_level="internal", category="faq",
        )
        experience_module.publish(session, previous.id, actor="ou_owner")
        draft.experience_id = previous.id
        session.flush()
        draft_id = draft.id
        previous_id = previous.id

    assert try_handle_command(
        incoming(
            f"纠正 #{draft_id} 新答案",
            "om_cmd_correct_message",
            sender="ou_owner",
        )
    )
    with session_scope() as session:
        assert session.get(Experience, previous_id).status == ExperienceStatus.PAUSED
        candidate = session.query(Experience).filter(
            Experience.previous_id == previous_id,
            Experience.status == ExperienceStatus.CANDIDATE,
        ).one()
        assert candidate.canonical_answer == "新答案"
        notice = session.query(Outbox).filter(Outbox.kind == "notice").one()
        assert "旧经验已暂停" in json.loads(notice.payload_json)["text"]


def test_correct_command_without_previous_does_not_claim_pause(source, approval_mode):
    from app.handlers.commands import try_handle_command
    from app.services import reply_engine
    from tests.helpers import incoming

    with session_scope() as session:
        draft = reply_engine.process_incoming(
            session, incoming("请问年假怎么申请", "om_cmd_correct2"), source=source
        )
        draft.sent_message_id = "om_cmd_receipt2"
        draft.status = DraftStatus.SENT
        session.flush()
        draft_id = draft.id

    assert try_handle_command(
        incoming(
            f"纠正 #{draft_id} 新答案",
            "om_cmd_correct_message2",
            sender="ou_owner",
        )
    )
    with session_scope() as session:
        notice = session.query(Outbox).filter(Outbox.kind == "notice").one()
        text = json.loads(notice.payload_json)["text"]
        assert "已生成经验候选" in text
        assert "旧经验已暂停" not in text


def test_exact_pattern_match_gets_calibrated_score(source, settings):
    with session_scope() as session:
        db_source = session.get(Source, source.id)
        exp = experience_module.create_candidate(
            session,
            question="年假怎么申请？",
            answer="在飞书工作台提交申请。",
            patterns=["年假怎么申请？", "年假申请流程"],
            visibility="public",
            sensitive_level="internal",
            audience={"chat_types": ["p2p"]},
            category="faq",
        )
        experience_module.publish(session, exp.id, actor="ou_owner")
        session.flush()

        exact = experience_module.find_best_match(
            session, "年假怎么申请", source=db_source
        )
        assert exact.score >= settings.exact_match_score
        assert "exact_pattern_match" in exact.reasons

        near = experience_module.find_best_match(
            session, "年假报销怎么申请", source=db_source
        )
        assert near.score < settings.exact_match_score


def test_verify_card_is_version_bound():
    from app.domain import review as review_module

    with session_scope() as session:
        exp = experience_module.create_candidate(
            session, question="年假怎么申请", answer="答案",
            visibility="public", sensitive_level="internal",
        )
        token = review_module.create_action_token(
            session, owner="ou_owner", action="verify_experience",
            target_type="experience", target_id=exp.id, target_version=99,
        )
        action_id = token.action_id
        exp_id = exp.id
    try:
        with session_scope() as session:
            review_module.handle_card_action(
                session, action_id=action_id, operator_id="ou_owner"
            )
        raise AssertionError("stale experience version must be rejected")
    except review_module.ReviewError as exc:
        assert "stale_target_version" in str(exc)
    with session_scope() as session:
        assert session.get(Experience, exp_id).status == ExperienceStatus.CANDIDATE


def test_rollback_card_is_bound_to_current_head():
    from app.domain import review as review_module

    with session_scope() as session:
        v1 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v1",
            visibility="public", sensitive_level="internal",
        )
        experience_module.publish(session, v1.id, actor="ou_owner")
        v2 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v2",
            intent_id=v1.intent_id, previous_id=v1.id,
            visibility="public", sensitive_level="internal",
        )
        experience_module.publish(session, v2.id, actor="ou_owner")
        token = review_module.create_action_token(
            session, owner="ou_owner", action="rollback_experience",
            target_type="experience", target_id=v2.id, target_version=2,
            payload={"intent_id": v1.intent_id},
        )
        action_id = token.action_id
        v3 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v3",
            intent_id=v1.intent_id, previous_id=v2.id,
            visibility="public", sensitive_level="internal",
        )
        experience_module.publish(session, v3.id, actor="ou_owner")
    try:
        with session_scope() as session:
            review_module.handle_card_action(
                session, action_id=action_id, operator_id="ou_owner"
            )
        raise AssertionError("stale rollback must be rejected")
    except review_module.ReviewError as exc:
        assert "rollback_not_current_head" in str(exc)
        assert "stale_target_version" not in str(exc)


def test_old_superseded_version_cannot_be_published_by_stale_card():
    from app.domain import review as review_module

    with session_scope() as session:
        v1 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v1",
            visibility="public", sensitive_level="internal",
        )
        experience_module.publish(session, v1.id, actor="ou_owner")
        v2 = experience_module.create_candidate(
            session, question="年假怎么申请", answer="v2",
            intent_id=v1.intent_id, previous_id=v1.id,
            visibility="public", sensitive_level="internal",
        )
        experience_module.publish(session, v2.id, actor="ou_owner")
        token = review_module.create_action_token(
            session, owner="ou_owner", action="verify_experience",
            target_type="experience", target_id=v1.id, target_version=1,
        )
        action_id = token.action_id
    try:
        with session_scope() as session:
            review_module.handle_card_action(
                session, action_id=action_id, operator_id="ou_owner"
            )
        raise AssertionError("superseded version must not be publishable")
    except review_module.ReviewError as exc:
        assert "experience_not_publishable" in str(exc)


def test_evoharness_retrieval_engine_is_used():
    ranked, engine = retrieval.rank("年假申请", {"a": retrieval.tokenize("年假申请流程"), "b": retrieval.tokenize("报销流程")})
    assert ranked
    assert ranked[0][0] == "a"
    assert engine == "evoharness-bm25"
    # 受控语料只含传入的 key，不混入全局 Skill
    assert {key for key, _ in ranked} <= {"a", "b"}
