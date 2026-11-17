"""经验回流回归：批准 → 沉淀 → 发布 → 同类问题必须命中。

背景（2026-11-10 实机）：整条链"看起来通"——草稿批准了、经验发布了——
但同类问题始终 `no_active_experience`。两个叠加缺陷：
1. `_audience_allows` 在无 source 上下文时无条件返回 audience_source_missing；
2. 沉淀时 owner 可能取到空串，private 受众校验按 owner 比对必然不匹配。
结果：经验库在涨，召回永远是 0。
"""
from __future__ import annotations

from app.core.database import session_scope
from app.domain import admission, review
from app.memory import experience as experience_module
from app.models import Experience, ExperienceStatus, ReplyDraft
from app.services.reply_engine import process_incoming
from tests.helpers import incoming

OWNER = "ou_flow_owner"
CHAT = "oc_flow_chat"


def _owner_mode(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "feishu_owner_open_id", OWNER)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", CHAT)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", CHAT)
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", CHAT)
    monkeypatch.setattr(settings, "deployment_mode", "single_approval")
    monkeypatch.setattr(settings, "shadow_mode", False)
    monkeypatch.setattr(settings, "auto_reply_enabled", False)


def _source(session, settings):
    return admission.ensure_source(
        session,
        tenant=settings.tenant_id,
        owner=OWNER,
        chat_id=CHAT,
        source_type="p2p",
        reply_allowed=True,
    )


def test_approved_answer_sediments_and_is_retrievable(settings, monkeypatch):
    _owner_mode(settings, monkeypatch)
    question = "年假怎么申请？"

    with session_scope() as session:
        source = _source(session, settings)
        draft = process_incoming(
            session,
            incoming(question, "om_flow_1", chat_id=CHAT, sender=OWNER),
            source=source,
        )
        assert draft is not None and str(draft.status) == "pending"
        draft_id = draft.id

    with session_scope() as session:
        review.approve_draft_direct(session, draft_id, actor=OWNER)
        draft = session.get(ReplyDraft, draft_id)
        assert draft.experience_id, "批准后必须沉淀经验"

    with session_scope() as session:
        # 2026-11-11 起"批准即发布"：无需另发「验证 #n」，状态直接可复用
        published = session.get(Experience, draft.experience_id)
        assert published.status == ExperienceStatus.APPROVED_AUTO
        # 已发布经验必须能命中（回流不是只有手动发布后才生效）
        match = experience_module.find_best_match(session, question, tenant=settings.tenant_id)
        assert match.experience is not None, match.reasons
        assert match.experience.id == published.id
        assert match.score > 0

    with session_scope() as session:
        source = _source(session, settings)
        # 同问法、带 source 的真实路径都必须命中已发布经验
        for text in (question, question.rstrip("？"), "  " + question + " "):
            match = experience_module.find_best_match(
                session, text, tenant=settings.tenant_id, source=source
            )
            assert match.experience is not None, f"{text!r} 未命中：{match.reasons}"
        # 无关问题不应命中
        other = experience_module.find_best_match(
            session, "打印机卡纸了怎么办", tenant=settings.tenant_id, source=source
        )
        assert other.experience is None


def test_sedimented_owner_is_never_empty(settings, monkeypatch):
    """owner 为空会让 private 经验的受众校验永久失败。"""
    _owner_mode(settings, monkeypatch)
    with session_scope() as session:
        experience = experience_module.create_candidate(
            session, question="报销怎么走？", answer="在 OA 提交"
        )
        assert experience.owner, "沉淀的经验必须有 owner"
        assert experience.owner == OWNER


def test_owner_falls_back_to_chat_id_when_open_id_missing(settings, monkeypatch):
    monkeypatch.setattr(settings, "feishu_owner_open_id", "")
    monkeypatch.setattr(settings, "feishu_owner_chat_id", CHAT)
    with session_scope() as session:
        experience = experience_module.create_candidate(
            session, question="门禁怎么申请？", answer="找行政"
        )
        assert experience.owner == CHAT


def test_misclassified_message_still_hits_published_experience(settings, monkeypatch):
    """分类器误判时也必须能命中已发布经验。

    实机（2026-11-10）："告诉我任务" 无问号且含"任务"，被判 task，
    于是既不检索经验也不出草稿——第二次问同一句直接"消失"，
    看起来像经验回流失效。现在非 question 也做一次检索兜底。
    """
    _owner_mode(settings, monkeypatch)
    question = "告诉我任务"

    # 先直接造一条已发布经验（模拟此前已沉淀发布的那条）
    with session_scope() as session:
        exp = experience_module.create_candidate(
            session,
            question=question,
            answer="你的任务是完成日报闭环验证。",
            audience={"chat_ids": [CHAT]},
            visibility="private",
        )
        experience_module.publish(session, exp.id, actor=OWNER)
        published_id = exp.id

    with session_scope() as session:
        source = _source(session, settings)
        draft = process_incoming(
            session,
            incoming(question, "om_flow_task", chat_id=CHAT, sender=OWNER),
            source=source,
        )
        assert draft is not None, "误判成 task 的问题也必须产出草稿或命中"
        assert draft.experience_id == published_id, (
            f"应命中已发布经验 {published_id}，实际 {draft.experience_id}"
        )


def test_non_question_without_experience_stays_silent(settings, monkeypatch):
    """没有任何命中时，非 question 消息仍然不打扰用户（保持原语义）。"""
    _owner_mode(settings, monkeypatch)
    with session_scope() as session:
        source = _source(session, settings)
        draft = process_incoming(
            session,
            incoming("任务：把页脚改成显示模型", "om_flow_t2", chat_id=CHAT, sender=OWNER),
            source=source,
        )
        assert draft is None
