"""DE-19：内部答疑 SOP 工作流与升级链回归。

覆盖（对应 tasks.json DE-19 acceptance_scenarios 与实施要求）：
1. 无答案返回缺失清单（不杜撰）；有证据答案带来源引用；冲突证据并存呈报不猜一个。
2. 私有知识对无授权受众不出现；同一问题不同受众得到不同可见集（工作流不放大权限）。
3. 承诺/敏感内容必须走批准；批准后原范围内复用；授权人纠正（supersede/源失效）
   后旧版本与旧批准不再被复用，未发送意图取消。
4. 重复 source event 去重：不产生第二份草稿/第二次发送意图。
5. 升级链：无证据/低置信升级到授权配置的责任人并带具体缺失项；无人配置时明确等待。
6. 组织答疑独立空间身份：不写个人分身事实表；回复/纠正经 DE-10 补充队列回流原
   工作项且幂等；个人分身语义不受影响。

离线 fake 端口：RecordedSink 只记录事件，不调模型、不真发信。
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from evoharness.clock import utcnow
from evoharness.governance import authorization
from evoharness.governance.authorization import AuthorizationError
from evoharness.knowledge import service as knowledge
from evoharness.models import (Base, Message, Outbox, OutboxStatus, ReplyDecision,
                               ReplyDraft, Task, TaskEvent, TaskStatus)
from evoharness.organization.models import BusinessSpace, Organization
from evoharness.work.models import TaskSupplement
from evoharness.workflows import knowledge_answer as ka


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def qa_space(session):
    """授权配置：组织 + 业务空间责任人（升级目标来自配置，不猜 owner）。"""
    session.add(Organization(org_id="org", title="EvoHarness", owner_principal_id="boss"))
    session.add(BusinessSpace(space_id="support", org_id="org", title="支持",
                              owner_principal_id="space-owner"))
    session.flush()


def _claim(s, subject="退款规则", value="7 天无理由", scope="public",
           refs=("doc:policy@v3",), **kw):
    return knowledge.record_claim(s, org_id="org", space_id="support",
                                  owner_scope=scope, subject=subject, value=value,
                                  source_refs=list(refs), **kw)


def _ask(s, event="om_1", subject="退款规则", audience="", auto=False, sink=None, **kw):
    return ka.answer_question(s, org_id="org", space_id="support", subject=subject,
                              source_event_id=event, audience_scope=audience,
                              audience_id=audience or "anonymous",
                              sink=sink, auto_enabled=auto, **kw)


def _events(sink, kind):
    return [e for e in sink.events if e["kind"] == kind]


def _supplement_count(s, task_id):
    return s.scalar(select(func.count(TaskSupplement.id)).where(
        TaskSupplement.task_id == task_id))


# ── 1) 无答案 / 有来源 / 冲突 ─────────────────────────────────────────────────


def test_missing_evidence_returns_missing_list_not_fabricated_answer(session, qa_space):
    sink = ka.RecordedSink()
    outcome = _ask(session, sink=sink)
    assert outcome["kind"] == "no_evidence"
    assert outcome["answer"] is None                     # 不杜撰答案
    assert outcome["status"] == "escalated"
    missing = outcome["missing"][0]
    assert missing["missing"] == "authoritative_claim"
    assert missing["subject"] == "退款规则"
    assert "record_claim" in missing["need"]             # 说清缺什么、去哪补
    escalations = _events(sink, "escalation")
    assert escalations and escalations[0]["missing"] == outcome["missing"]
    assert _events(sink, "send_intent") == []


def test_sourced_answer_carries_citations(session, qa_space):
    _claim(session)
    sink = ka.RecordedSink()
    outcome = _ask(session, sink=sink)
    assert outcome["kind"] == "sourced"
    assert outcome["status"] == "pending_approval"       # 首答出草稿待批准
    text = outcome["answer"]["text"]
    assert "7 天无理由" in text
    assert "doc:policy@v3" in text                       # 来源引用在答案里
    drafts = _events(sink, "draft")
    assert drafts and drafts[0]["requires"] == "approval"
    assert "first_answer_requires_approval" in drafts[0]["reasons"]


def test_conflicting_claims_reported_side_by_side(session, qa_space):
    first = _claim(session, value="7 天无理由")
    second = _claim(session, value="30 天无理由", refs=("msg:owner-decision@5",))
    sink = ka.RecordedSink()
    outcome = _ask(session, sink=sink)
    assert outcome["status"] == "conflict"
    assert outcome["answer"] is None                     # 不猜一个
    assert set(outcome["missing"][0]["claim_ids"]) == {first.claim_id, second.claim_id}
    drafts = _events(sink, "draft")
    values = {c["value"] for c in drafts[0]["claims"]}
    assert values == {"7 天无理由", "30 天无理由"}        # 冲突证据并存呈报
    assert _events(sink, "send_intent") == []            # 冲突绝不自动发送
    assert outcome["delivery"]["mode"] == "conflict_report"
    assert outcome["escalation"]["reason"] == "conflict_unresolved"


def test_expired_claim_is_hinted_not_answered(session, qa_space):
    _claim(session, valid_until=utcnow() - timedelta(days=1))
    outcome = _ask(session)
    assert outcome["answer"] is None
    assert outcome["missing"][0]["expired_hint"]         # 提示回源核实
    assert "有效期" in outcome["missing"][0]["need"]


# ── 2) 受众边界 ────────────────────────────────────────────────────────────────


def test_private_claim_hidden_from_unauthorized_audience(session, qa_space):
    sink = ka.RecordedSink()
    _claim(session, value="内部口径", scope="ou_alice", refs=("msg:alice@1",))
    outcome = _ask(session, audience="ou_bob", sink=sink)
    assert outcome["answer"] is None                     # 私有知识不因受众不同而扩散
    assert outcome["kind"] == "no_evidence"
    assert outcome["status"] == "escalated"
    assert all("内部口径" not in str(e) for e in sink.events)


def test_same_question_different_audiences_get_different_visible_sets(session, qa_space):
    # 同一 value 的公开口径 + alice 私有补充来源：alice 见 2 条、bob 只见公开 1 条
    _claim(session, value="7 天无理由", scope="public", refs=("doc:faq@v1",))
    _claim(session, value="7 天无理由", scope="ou_alice", refs=("msg:alice@1",))
    alice = _ask(session, event="om_a", audience="ou_alice")
    bob = _ask(session, event="om_b", audience="ou_bob")
    assert len(alice["answer"]["claims"]) == 2
    assert len(bob["answer"]["claims"]) == 1              # 同一问题不同可见集
    assert "msg:alice@1" in alice["answer"]["text"]
    assert "msg:alice@1" not in bob["answer"]["text"]
    assert "doc:faq@v1" in bob["answer"]["text"]


def test_conflicting_values_across_scopes_stay_conflict_for_authorized_audience(session, qa_space):
    # 私有口径与公开口径 value 不同：对授权受众仍是冲突信息，呈报而非自动裁决，
    # 同时绝不把私有口径泄漏给未授权受众。
    _claim(session, value="7 天无理由", scope="public", refs=("doc:faq@v1",))
    _claim(session, value="内部 15 天", scope="ou_alice", refs=("msg:alice@1",))
    alice = _ask(session, event="om_a", audience="ou_alice")
    bob = _ask(session, event="om_b", audience="ou_bob")
    assert alice["status"] == "conflict"                  # 冲突保留呈报，不猜一个
    assert bob["kind"] == "sourced"                       # bob 仍只见公开口径
    assert "内部 15 天" not in bob["answer"]["text"]


# ── 3) 批准、复用与纠正 ────────────────────────────────────────────────────────


def test_promise_and_sensitive_content_require_approval(session, qa_space):
    _claim(session, value="保证 100% 退款", refs=("doc:p@v1",))
    sink = ka.RecordedSink()
    outcome = _ask(session, event="om_p", sink=sink, auto=True)
    assert outcome["delivery"]["mode"] == "draft_pending_approval"
    assert "promise_or_commitment" in outcome["answer"]["risk_flags"]
    assert _events(sink, "send_intent") == []

    _claim(session, subject="客服密码", value="客服密码是 123456", refs=("doc:s@v1",))
    sink2 = ka.RecordedSink()
    outcome2 = _ask(session, event="om_s", subject="客服密码", sink=sink2, auto=True)
    assert outcome2["delivery"]["mode"] == "draft_pending_approval"
    assert "sensitive_content" in outcome2["answer"]["risk_flags"]
    assert _events(sink2, "send_intent") == []


def test_approved_answer_reused_within_scope_only(session, qa_space):
    _claim(session)
    first = _ask(session, event="om_1", audience="ou_bob")
    approval = ka.approve_answer(session, actor_id="space-owner", org_id="org", space_id="support",
                                 subject="退款规则", audience_scope="ou_bob",
                                 answer_payload_dict=first["answer"]["payload"])
    sink2 = ka.RecordedSink()
    second = _ask(session, event="om_2", audience="ou_bob", auto=True, sink=sink2)
    assert second["status"] == "answered"
    assert second["delivery"]["mode"] == "auto_send"      # 批准后原范围内复用
    assert second["delivery"]["decision_id"] == approval["decision_id"]
    assert len(_events(sink2, "send_intent")) == 1

    third = _ask(session, event="om_3", audience="ou_carol", auto=True)
    assert third["delivery"]["mode"] == "draft_pending_approval"  # 不跨受众复用
    assert third["delivery"]["decision_id"] == ""


def test_auto_send_switch_off_keeps_reusable_draft(session, qa_space):
    _claim(session)
    first = _ask(session, event="om_1")
    ka.approve_answer(session, actor_id="space-owner", org_id="org", space_id="support",
                      subject="退款规则", answer_payload_dict=first["answer"]["payload"])
    second = _ask(session, event="om_2", auto=False)      # 自动发送开关关闭
    assert second["status"] == "answered"
    assert second["delivery"]["mode"] == "draft_reusable"
    assert "approved_scope_reuse" in second["delivery"]["reasons"]


def test_correction_supersedes_old_answer_and_approval(session, qa_space):
    old = _claim(session)
    first = _ask(session, event="om_1")
    ka.approve_answer(session, actor_id="space-owner", org_id="org", space_id="support",
                      subject="退款规则", answer_payload_dict=first["answer"]["payload"])
    reused = _ask(session, event="om_2", auto=True)
    assert reused["delivery"]["mode"] == "auto_send"

    correction = ka.apply_correction(
        session, actor_id="space-owner", org_id="org", space_id="support",
        subject="退款规则", value="15 天无理由", source_refs=["doc:policy@v4"],
        supersedes=old.claim_id)
    session.refresh(old)
    assert old.state == "superseded"                      # DE-16 supersede 语义
    assert correction["claim_id"]

    after = _ask(session, event="om_3", auto=True)
    assert "15 天无理由" in after["answer"]["text"]
    assert "7 天无理由" not in after["answer"]["text"]     # 旧版本不再被复用
    assert after["delivery"]["decision_id"] == ""         # 旧批准失效，需重新批准
    assert after["delivery"]["mode"] == "draft_pending_approval"
    # 旧批准已被撤回：重放旧载荷也无法通过统一重验
    with pytest.raises(AuthorizationError):
        authorization.validate(session, target_type="knowledge_answer",
                               target_id=ka.reuse_target_id("org", "support", "退款规则"),
                               target_revision=1,
                               payload_hash=first["answer"]["payload_hash"],
                               action="send_as_bot")


def test_correction_via_source_invalidation(session, qa_space):
    _claim(session)
    outcome = ka.apply_correction(
        session, actor_id="space-owner", space_id="support", subject="其他规则",
        value="占位", source_refs=["doc:new@v1"], owner_scope="public",
        invalidate_source_ref="doc:policy@v3", revoke_approval=False)
    assert outcome["invalidated_claims"]
    after = _ask(session)
    assert after["answer"] is None                        # 源失效后不再回答旧口径
    assert after["kind"] == "no_evidence"


def test_correction_cancels_pending_send_intent(session, qa_space):
    _claim(session)
    first = _ask(session, event="om_1")
    ka.approve_answer(session, actor_id="space-owner", org_id="org", space_id="support",
                      subject="退款规则", answer_payload_dict=first["answer"]["payload"])
    target_id = ka.reuse_target_id("org", "support", "退款规则")
    session.add(Outbox(intent_key=f"send:{target_id}", tenant="org", kind="reply",
                       chat_id="org-chat", target_type="knowledge_answer",
                       target_id=target_id, payload_json="{}",
                       status=OutboxStatus.QUEUED))
    session.flush()
    result = ka.apply_correction(
        session, actor_id="space-owner", org_id="org", space_id="support",
        subject="退款规则", value="更正口径", source_refs=["doc:policy@v9"])
    assert result["cancelled_intents"] == ["send:" + target_id]
    row = session.scalar(select(Outbox).where(Outbox.intent_key == f"send:{target_id}"))
    assert row.status == OutboxStatus.CANCELLED
    assert "decision_revoked" in row.last_error


# ── 4) 去重 ────────────────────────────────────────────────────────────────────


def test_duplicate_source_event_no_second_draft_or_send(session, qa_space):
    _claim(session)
    sink = ka.RecordedSink()
    first = _ask(session, event="om_1", sink=sink)
    assert len(sink.events) == 1

    replay = _ask(session, event="om_1", sink=sink)
    assert replay["status"] == "duplicate"
    assert replay["answer_id"] == first["answer_id"]
    assert len(sink.events) == 1                         # 不产生第二份草稿

    ka.approve_answer(session, actor_id="space-owner", org_id="org", space_id="support",
                      subject="退款规则", answer_payload_dict=first["answer"]["payload"])
    again = _ask(session, event="om_1", sink=sink, auto=True)
    assert again["status"] == "duplicate"
    assert _events(sink, "send_intent") == []


# ── 5) 升级链 ──────────────────────────────────────────────────────────────────


def test_escalation_target_comes_from_authorization_config(session, qa_space):
    sink = ka.RecordedSink()
    outcome = _ask(session, event="om_missing", sink=sink)
    assert outcome["escalation"]["target"] == {"kind": "space_owner",
                                               "principal_id": "space-owner"}
    escalations = _events(sink, "escalation")
    assert escalations[0]["target"]["principal_id"] == "space-owner"
    assert escalations[0]["missing"][0]["subject"] == "退款规则"


def test_injected_provider_overrides_default_target(session, qa_space):
    def provider(session, *, org_id, space_id, subject):
        return {"kind": "position_owner", "principal_id": "qa-lead"}

    outcome = _ask(session, escalation_target_provider=provider)
    assert outcome["escalation"]["target"]["principal_id"] == "qa-lead"


def test_no_escalation_target_waits_explicitly(session):
    # 空间未配置责任人：明确等待而不是假回答，也不猜 owner
    sink = ka.RecordedSink()
    outcome = _ask(session, event="om_w", sink=sink)
    assert outcome["status"] == "awaiting_escalation_target"
    assert outcome["escalation"] is None
    assert outcome["answer"] is None
    assert _events(sink, "escalation") == []


def test_low_confidence_escalates_with_missing_items(session, qa_space):
    _claim(session)
    sink = ka.RecordedSink()
    outcome = _ask(session, event="om_low", sink=sink, min_confidence=0.99)
    assert outcome["status"] == "escalated"
    assert outcome["escalation"]["reason"] == "low_confidence"
    assert outcome["missing"][0]["missing"] == "higher_confidence_source"
    assert outcome["delivery"]["mode"] == "escalation_review"
    assert _events(sink, "escalation")


# ── 6) 空间身份隔离与工作项回流 ────────────────────────────────────────────────


def test_org_answer_does_not_touch_twin_fact_tables(session, qa_space):
    _claim(session)
    outcome = _ask(session)
    assert session.scalars(select(Message.id)).first() is None
    assert session.scalars(select(ReplyDecision.id)).first() is None
    assert session.scalars(select(ReplyDraft.id)).first() is None
    # 组织答疑身份字段独立于个人分身 tenant/chat 域
    assert outcome["org_id"] == "org"
    assert outcome["space_id"] == "support"
    assert outcome["answer_id"]


def test_reflow_appends_to_work_item_and_dedups(session, qa_space):
    _claim(session)
    task = Task(task_key="org-support-refund", tenant="org", chat_id="org-chat",
                title="退款规则答疑", action="答疑", status=TaskStatus.OPEN,
                workflow_kind="legacy_todo")
    session.add(task)
    session.flush()

    first = _ask(session, event="om_1", work_item_id=task.id)
    assert first["reflow"]["task_event"] == "note_appended"
    assert _supplement_count(session, task.id) == 1

    replay = _ask(session, event="om_1", work_item_id=task.id)   # 同事件重放
    assert replay["status"] == "duplicate"
    assert _supplement_count(session, task.id) == 1

    _ask(session, event="om_2", work_item_id=task.id)             # 新事件正常回流
    assert _supplement_count(session, task.id) == 2
    notes = [e for e in session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id))
             if e.event_type == "note_appended"]
    assert len(notes) == 2
    # 回流只做补充，不改变原工作项状态（个人/业务任务语义保留）
    session.refresh(task)
    assert task.status == TaskStatus.OPEN


def test_answer_requires_core_inputs(session):
    with pytest.raises(ka.KnowledgeAnswerError, match="space_required"):
        ka.answer_question(session, space_id="", subject="x", source_event_id="e")
    with pytest.raises(ka.KnowledgeAnswerError, match="subject_required"):
        ka.answer_question(session, space_id="support", subject="  ", source_event_id="e")
    with pytest.raises(ka.KnowledgeAnswerError, match="source_event_required"):
        ka.answer_question(session, space_id="support", subject="x", source_event_id="")


def test_frozen_org_faq_holdout_is_independent_value():
    # 低风险 FAQ 的独立冻结口径存在，且口径说明与 golden（F08/F09）隔离
    assert ka.ORG_FAQ_HOLDOUT["auto_min_confidence"] == 0.92
    assert ka.ORG_FAQ_HOLDOUT["draft_min_confidence"] == 0.5
    assert "F08/F09" in ka.ORG_FAQ_HOLDOUT["note"]
