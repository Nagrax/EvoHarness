"""回复决策引擎：实时事件与每日补采共用的统一入口。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.ids import sha256_json, sha256_text
from app.domain import admission, intake, policy, review, tasks
from app.domain import outbox as outbox_module
from app.feishu.messages import IncomingMessage
from app.llm.service import (
    classify_message,
    compose_reply,
    generate_fallback_reply,
    prompt_versions,
)
from app.memory import experience as experience_module
from app.models import (
    Category,
    Chat,
    DraftMode,
    DraftStatus,
    Experience,
    ExperienceStatus,
    Message,
    ReplyDecision,
    ReplyDraft,
    Source,
)

logger = logging.getLogger(__name__)


def classify_message(text: str) -> dict:  # re-export for monkeypatching in callers/tests
    from app.llm.service import classify_message as _classify

    return _classify(text)


def compose_reply(question: str, experience) -> str:
    from app.llm.service import compose_reply as _compose

    return _compose(question, experience)


def generate_fallback_reply(question: str) -> str:
    from app.llm.service import generate_fallback_reply as _fallback

    return _fallback(question)


def upsert_chat(session: Session, chat_id: str, chat_type: str, name: str) -> Chat:
    settings = get_settings()
    chat = session.get(Chat, chat_id)
    if chat is None:
        chat = Chat(chat_id=chat_id, chat_type=chat_type, tenant=settings.tenant_id, name=name)
        session.add(chat)
    else:
        # 类型必须可修正：历史行可能按 oc_ 前缀把 p2p 登记成 group（2026-11-10 实机）。
        if chat_type and chat.chat_type != chat_type:
            chat.chat_type = chat_type
        if name:
            chat.name = name
    session.flush()
    return chat


def ingest_message(
    session: Session,
    incoming: IncomingMessage,
    *,
    source: Source | None = None,
    allow_unregistered: bool = True,
) -> Message:
    """幂等入库并分类；不触发回复。"""
    settings = get_settings()
    if source is None:
        result = admission.admit(session, incoming, allow_unregistered=allow_unregistered)
        if not result.accepted:
            raise PermissionError(result.reason)
        source = result.source
    intake_result = intake.ingest_message(session, incoming, source=source)
    message = intake_result.message
    # 历史补采只入库不分类；处理阶段必须补上分类，否则补采问题会永久停在 unknown。
    needs_classify = (
        intake_result.created
        or intake_result.edited
        or (not message.handled and message.category == Category.UNKNOWN)
    )
    if needs_classify:
        classification = classify_message(message.content_text)
        message.category = classification["category"]
        message.summary = str(classification.get("summary") or "")[:255]
        message.due = str(classification.get("due") or "")[:64]
        session.flush()
    return message


def _latest_draft(session: Session, message_id: str) -> ReplyDraft | None:
    return session.scalar(
        select(ReplyDraft)
        .where(ReplyDraft.message_id == message_id)
        .order_by(ReplyDraft.id.desc())
    )


def _snapshot_id(session: Session, tenant: str) -> str:
    rows = []
    for head in session.scalars(
        select(experience_module.ExperienceHead).where(
            experience_module.ExperienceHead.tenant == tenant
        )
    ):
        rows.append((head.intent_id, head.experience_id))
    return sha256_json(sorted(rows))[:32]


def _mention_hint(session: Session, message: Message) -> dict:
    """群聊回复时带上"@提问者"的信息。

    飞书的引用回复在群里不显眼：别人看不出这句话是回给谁的。
    私聊不需要 @（就两个人），加上反而多噪音。
    """
    chat = session.get(Chat, message.chat_id)
    is_p2p = bool(chat is None or chat.chat_type == "p2p")
    return {
        "mention_open_id": (message.sender_id or "") if not is_p2p else "",
        "is_p2p": is_p2p,
    }


def _approver_for(session: Session, message: Message) -> str:
    """这条待批该由谁批准 —— **按分身主人，不按群归属**。

    正确模型（2026-11-11 用户第三次纠正）：
    **机器人是用户的数字分身。群里出现机器人，只是为了让分身能代替
    它自己的主人在群里说话。所以一切使用该机器人的用户都拥有自己的
    批准权，而不是只有把机器人拉进群的人才有。**

    落地判定：
    1. 私聊：会话本身就是某人的分身入口 → 归该人；
    2. 群聊：群里有多个分身，先看这条消息是否由某个分身的主人发出
       （他自己问的，自然归他）；否则看该群登记的活跃分身集合，
       **只有一个**时归他（无歧义），多个时归管理员兜底（无法判定归属，
       硬指派会错批别人的话）。
    3. 都没有 → 管理员兜底。
    """
    from app.core.config import get_settings

    settings = get_settings()
    admin_id = (settings.feishu_owner_open_id or "").strip()

    chat = session.get(Chat, message.chat_id)
    is_group = bool(chat is not None and chat.chat_type == "group")

    if not is_group:
        # 私聊：说话人就是分身主人
        if (message.sender_id or "").startswith("ou_"):
            return message.sender_id
        source = session.get(Source, message.source_id) if message.source_id else None
        if source is not None and (source.owner or "").startswith("ou_"):
            return source.owner
        return admin_id

    # 群聊：分身按人划分，一个群可以有多个分身
    twins = [
        (s.owner or "").strip()
        for s in session.scalars(
            select(Source).where(
                Source.tenant == message.tenant,
                Source.chat_id == message.chat_id,
                Source.source_type == "group",
                Source.enabled.is_(True),
                Source.revoked_at.is_(None),
            )
        )
        if (s.owner or "").startswith("ou_")
    ]
    twins = sorted(set(twins))

    # ① 提问者本人在这个群里也有分身 → 归他（他自己问的，他最清楚怎么答）
    sender = (message.sender_id or "").strip()
    if sender in twins:
        return sender

    # ② 群里只有一个分身 → 无歧义，归他
    if len(twins) == 1:
        return twins[0]

    # ③ 多个分身且无法判定 → 管理员兜底（宁可人工看，也不能错批别人的话）
    return admin_id


def process_incoming(
    session: Session,
    incoming: IncomingMessage,
    *,
    source: Source | None = None,
    allow_unregistered: bool = True,
    now: datetime | None = None,
    shadow_override: bool | None = None,
) -> Optional[ReplyDraft]:
    """处理一条消息：分类、任务抽取、经验匹配、硬门、草稿/发送意图。"""
    settings = get_settings()
    now = now or utcnow()
    if source is None:
        admission_result = admission.admit(
            session, incoming, allow_unregistered=allow_unregistered
        )
        if not admission_result.accepted:
            return None
        source = admission_result.source
    message = ingest_message(session, incoming, source=source)
    if message.recalled:
        return None

    existing_decision = session.scalar(
        select(ReplyDecision).where(
            ReplyDecision.tenant == message.tenant,
            ReplyDecision.message_id == message.message_id,
        )
    )
    if existing_decision is not None or message.handled:
        message.handled = True
        return _latest_draft(session, message.message_id)

    tasks.extract_and_upsert(session, message, source=source)

    # L9049：显式「问：」前缀 → 请求 EvoHarness Agent（执行层大脑）。
    # 前缀是用户的显式意图标记，优先于分类器；桥不可用/未开启/超时 →
    # 返回 None 并按原分类流程走（优雅降级，协同层不因执行层停摆）。
    from types import SimpleNamespace

    from app.services import agent_bridge

    bridge_answer: str | None = None
    ask_text = agent_bridge.strip_ask_prefix(message.content_text or "")
    if ask_text is not None and agent_bridge.enabled():
        bridge_answer = agent_bridge.fetch_answer(ask_text)

    if message.category != Category.QUESTION and bridge_answer is None:
        # 分类器是关键词规则，会误判："告诉我任务" 无问号且含"任务"→判 task，
        # 于是既不检索经验也不出草稿（2026-11-10 实机：同类问题第二次提问
        # 直接消失，看起来像"经验回流失效"）。这里补一步低成本兜底：
        # 非 question 也做一次检索，命中已发布经验才按问题处理；否则维持原语义。
        fallback_match = experience_module.find_best_match(
            session, message.content_text, tenant=message.tenant, source=source
        )
        actionable = bool(
            fallback_match.experience is not None
            and fallback_match.experience.status == ExperienceStatus.APPROVED_AUTO
            and fallback_match.score >= settings.auto_reply_threshold
        )
        if not actionable:
            tasks.apply_completion_signal(session, message)
            message.handled = True
            session.flush()
            return None
        # 命中已发布经验：说明用户确实在问一件"库里有答案"的事。
        # policy.evaluate 对非 question 一律 skip，所以这里必须先把类别
        # 提升为 question，否则兜底检索通过了也照样被 skip 掉
        # （2026-11-10 实机：ACTIONABLE=True 但 decision=skip/not_question）。
        message.category = Category.QUESTION
        session.flush()

    if bridge_answer is not None:
        match = None
        decision = SimpleNamespace(
            decision="draft",
            reasons=["agent_ask"],
            experience=None,
            similarity=0.0,
            shadow=False,
        )
    else:
        match = experience_module.find_best_match(
            session, message.content_text, tenant=message.tenant, source=source
        )
        decision = policy.evaluate(
            session,
            message=message,
            source=source,
            experience=match.experience,
            similarity=match.score,
            match_reasons=match.reasons,
            now=now,
            shadow_override=shadow_override,
        )
    versions = prompt_versions()
    session.add(
        ReplyDecision(
            tenant=message.tenant,
            message_id=message.message_id,
            decision=decision.decision,
            reason_codes=json.dumps(decision.reasons, ensure_ascii=False),
            experience_id=decision.experience.id if decision.experience else None,
            similarity=decision.similarity,
            rule_pass=decision.decision == "auto_eligible",
            policy_version=settings.policy_version,
            model_version=settings.llm_model,
            prompt_version=versions["classify"],
            data_snapshot_id=_snapshot_id(session, message.tenant),
            shadow=decision.shadow,
        )
    )
    message.handled = True

    if decision.decision == "skip":
        session.flush()
        return None

    experience = decision.experience
    if experience is not None:
        content = compose_reply(message.content_text, experience)
        experience_id = experience.id
    elif bridge_answer is not None:
        content = bridge_answer
        experience_id = None
    else:
        # 群聊不沉默：机器人是被拉进群**代表用户发话**的，
        # 不是来观察群聊的。没命中经验时给出保守草稿（等用户批准才发），
        # 这与"刷屏"无关——没人批准就不会有任何消息出现在群里。
        #
        # 历史设计（2026-11-11 之前）在这里让群聊沉默，那是把"免 @ 会自动说话"
        # 和"批准后才说话"混为一谈：实际发不发由审批决定，不是由沉默决定。
        content = generate_fallback_reply(message.content_text)
        experience_id = None

    # 首次私聊的"已开启日报"告知：随草稿一起给出，不额外多发一条消息。
    # 从入参 IncomingMessage 取（ORM 的 Message 上没有这个字段）。
    notice = getattr(incoming, "enroll_notice", "") or ""
    if notice:
        content = f"{content}\n\n{notice}"

    if decision.decision == "auto_eligible":
        draft = ReplyDraft(
            tenant=message.tenant,
            message_id=message.message_id,
            chat_id=message.chat_id,
            experience_id=experience_id,
            draft_content=content,
            content_hash=sha256_text(content),
            version=1,
            score=decision.similarity,
            confidence=decision.similarity,
            mode=DraftMode.AUTO,
            status=DraftStatus.APPROVED,
            approved_by="policy",
            approved_at=now,
        )
        session.add(draft)
        session.flush()
        intent = outbox_module.intent_key(
            message.tenant, message.message_id, "reply", f"draft:{draft.id}", "v1"
        )
        outbox_module.enqueue(
            session,
            intent=intent,
            tenant=message.tenant,
            kind="reply",
            chat_id=message.chat_id,
            target_type="message",
            target_id=message.message_id,
            payload={
                "text": content,
                "draft_id": draft.id,
                "reply_in_thread": bool(message.thread_id),
                "auto": True,
                **_mention_hint(session, message),
            },
            policy_version=settings.policy_version,
            experience_id=experience_id,
            source_id=source.id if source else None,
        )
        experience_module.record_hit(session, experience, auto=True)
        session.flush()
        return draft

    draft = ReplyDraft(
        tenant=message.tenant,
        message_id=message.message_id,
        chat_id=message.chat_id,
        experience_id=experience_id,
        draft_content=content,
        content_hash=sha256_text(content),
        version=1,
        score=decision.similarity,
        confidence=decision.similarity,
        mode=DraftMode.SHADOW if decision.shadow else DraftMode.DRAFT,
        status=DraftStatus.PENDING,
        shadow=decision.shadow,
        note=",".join(decision.reasons),
        # 归属＝该会话的使用者（私聊归本人，群聊由管理员把关），
        # 不是"提问者"——谁用这个机器人，谁对自己的工作消息负责。
        approver_id=_approver_for(session, message),
    )
    session.add(draft)
    if experience is not None:
        experience_module.record_hit(session, experience)
    session.flush()
    return draft


# ── DE-19：组织答疑可选入口（独立空间身份；不改变个人日报/个人分身语义）────────


def process_org_question(
    session: Session,
    *,
    org_id: str,
    space_id: str,
    subject: str,
    source_event_id: str,
    audience_scope: str = "",
    audience_id: str = "",
    work_item_id: int | None = None,
    escalation_target_provider=None,
    sink=None,
    auto_enabled: bool = False,
    now=None,
) -> dict:
    """经渠道绑定解析出的业务空间问题走组织答疑 SOP（DE-19）。

    与 process_incoming（个人分身链路）互不串扰：本入口只绑定 org/space 身份，
    不写 Message/ReplyDecision/ReplyDraft 等个人分身事实表；个人日报、
    审批与自动回复语义全部保持原状。领域实现在
    ``evoharness.workflows.knowledge_answer``（延迟导入：核心不反向依赖飞书）。
    """
    from evoharness.workflows.knowledge_answer import answer_question

    return answer_question(
        session,
        org_id=org_id,
        space_id=space_id,
        subject=subject,
        source_event_id=source_event_id,
        audience_scope=audience_scope,
        audience_id=audience_id,
        work_item_id=work_item_id,
        escalation_target_provider=escalation_target_provider,
        sink=sink,
        auto_enabled=auto_enabled,
        now=now,
    )


def approve_draft(
    session: Session, draft_id: int, edited_content: Optional[str] = None
) -> Optional[ReplyDraft]:
    settings = get_settings()
    draft = session.get(ReplyDraft, draft_id)
    if draft is None:
        return None
    try:
        outcome = review.approve_draft_direct(
            session, draft_id, actor=settings.feishu_owner_open_id,
            edited_content=edited_content,
        )
    except review.ReviewError:
        return draft
    if outcome.outbox_id:
        draft.status = DraftStatus.APPROVED
    return draft


def reject_draft(
    session: Session, draft_id: int, reason: str = ""
) -> Optional[ReplyDraft]:
    settings = get_settings()
    draft = session.get(ReplyDraft, draft_id)
    if draft is None:
        return None
    try:
        return review.reject_draft_direct(
            session, draft_id, actor=settings.feishu_owner_open_id, reason=reason
        )
    except review.ReviewError:
        return draft


def dispatch_pending(*, limit: int = 20, transport=None):
    """提交后发送的便捷入口；测试可注入假传输。"""
    from app.core.database import SessionLocal

    return outbox_module.dispatch_outbox(SessionLocal, transport=transport, limit=limit)
