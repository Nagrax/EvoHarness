"""自迭代回归：同 question_pattern 多版本并列时，检索必须优先已发布 head。

实测（2026-11-08 自迭代 auto 窗口预检）：候选与 head 同分时排序取决于语料迭代顺序，
候选可能压过 head（expected #40 got #36），导致自动窗口预检失败或取错版本。
修复：find_best_match 同分确定性排序（分数 → 已发布优先 → 高版本优先 → id 优先）。
"""
import pytest

from app.core.database import session_scope
from app.core.clock import utcnow
from app.models import Experience, ExperienceStatus, Source


def _make(exp_id: int, status: ExperienceStatus, version: int) -> None:
    with session_scope() as session:
        exp = Experience(
            tenant="default",
            owner="ou_owner",
            intent_id="intent-annual-leave",
            version=version,
            question_pattern="请问年假怎么申请？",
            canonical_answer=f"答案版本 {version}",
            status=status,
            visibility="private",
            created_by="test",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        exp.id = exp_id
        session.add(exp)
        session.flush()


@pytest.fixture
def p2p_source():
    from app.domain.admission import ensure_source

    with session_scope() as session:
        value = ensure_source(
            session,
            tenant="default",
            owner="ou_owner",
            chat_id="oc_tie1",
            source_type="p2p",
            reply_allowed=True,
        )
        session.expunge(value)
        return value


def test_tie_prefers_published_head_over_candidate(p2p_source, monkeypatch):
    from app.memory import experience as exp_module

    _make(36, ExperienceStatus.CANDIDATE, 2)
    _make(40, ExperienceStatus.APPROVED_AUTO, 4)

    # 相同文本 → BM25 必然同分；monkeypatch 固定平分，语料迭代顺序不得决定结果
    monkeypatch.setattr(
        exp_module.retrieval,
        "rank",
        lambda query, corpus: ([("36", 1.5), ("40", 1.5)], "test-engine"),
    )

    with session_scope() as session:
        src = session.scalars(
            __import__("sqlalchemy").select(Source).where(Source.chat_id == "oc_tie1")
        ).first()
        result = exp_module.find_best_match(session, "请问年假怎么申请？", source=src)

    assert result.experience is not None
    assert result.experience.id == 40
    assert result.experience.status == ExperienceStatus.APPROVED_AUTO


def test_higher_score_still_wins_over_head_preference(p2p_source, monkeypatch):
    """分数优先于身份：已发布经验不得凭身份压过明显更高分的其他经验。"""
    from app.memory import experience as exp_module

    _make(50, ExperienceStatus.APPROVED_AUTO, 1)  # 弱相关（年假）
    # 强相关候选（独立意图簇，避免 version 唯一约束冲突）
    with session_scope() as session:
        exp = Experience(
            tenant="default",
            owner="ou_owner",
            intent_id="intent-expense",
            version=1,
            question_pattern="报销流程是什么？",
            canonical_answer="报销需在费用系统提交发票影像",
            status=ExperienceStatus.CANDIDATE,
            visibility="private",
            created_by="test",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        exp.id = 51
        session.add(exp)
        session.flush()

    monkeypatch.setattr(
        exp_module.retrieval,
        "rank",
        lambda query, corpus: ([("50", 0.2), ("51", 2.0)], "test-engine"),
    )

    with session_scope() as session:
        src = session.scalars(
            __import__("sqlalchemy").select(Source).where(Source.chat_id == "oc_tie1")
        ).first()
        result = exp_module.find_best_match(session, "报销流程是什么？", source=src)

    assert result.experience is not None
    assert result.experience.id == 51


def test_candidate_higher_score_swaps_to_intent_head(p2p_source, monkeypatch):
    """自迭代实测（2026-11-08）：候选变体文本原始分高于已发布 head 时，
    对外命中必须切换到 head（对外应答使用发布版本）。"""
    from app.memory import experience as exp_module

    _make(70, ExperienceStatus.APPROVED_AUTO, 4)   # head（答案 v4）
    _make(71, ExperienceStatus.CANDIDATE, 5)       # 同意图候选（答案文本变体，得分更高）

    monkeypatch.setattr(
        exp_module.retrieval,
        "rank",
        lambda query, corpus: ([("71", 1.9), ("70", 1.2)], "test-engine"),
    )
    # head 指针指向 70
    from app.models import ExperienceHead

    with session_scope() as session:
        session.add(ExperienceHead(intent_id="intent-annual-leave", tenant="default", experience_id=70))
        session.flush()

    with session_scope() as session:
        src = session.scalars(
            __import__("sqlalchemy").select(Source).where(Source.chat_id == "oc_tie1")
        ).first()
        result = exp_module.find_best_match(session, "请问年假怎么申请？", source=src)

    assert result.experience is not None
    assert result.experience.id == 70
    assert "intent_head_preferred" in result.reasons
