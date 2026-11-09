"""F09 近义标定回归：BM25 原始分归一化 s/(1+s)。

自迭代实测（2026-11-08 auto 窗口）：原始分量纲随语料漂移，exact=1.574 / 近义=0.978，
0.85 固定阈值让近义变体误触发自动发送。归一化后近义 0.978→0.494（低于草稿阈值 0.55），
只有精确匹配（钳制 0.95）能过自动阈值。
"""
import pytest

from app.core.database import session_scope
from app.core.clock import utcnow
from app.models import Experience, ExperienceHead, ExperienceStatus, Source


def _make(exp_id: int, status: ExperienceStatus, version: int, intent_id: str = "intent-annual-leave") -> None:
    with session_scope() as session:
        exp = Experience(
            tenant="default",
            owner="ou_owner",
            intent_id=intent_id,
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
            chat_id="oc_norm1",
            source_type="p2p",
            reply_allowed=True,
        )
        session.expunge(value)
        return value


def _source():
    with session_scope() as session:
        return session.scalars(
            __import__("sqlalchemy").select(Source).where(Source.chat_id == "oc_norm1")
        ).first()


def test_scores_bounded_below_auto_threshold(p2p_source, monkeypatch):
    """大原始分归一化后必须低于 0.85：原始 5.0 → 0.8333，不得自动发送。"""
    from app.memory import experience as exp_module
    from app.domain import policy

    _make(60, ExperienceStatus.APPROVED_AUTO, 1)
    monkeypatch.setattr(
        exp_module.retrieval, "rank",
        lambda query, corpus: ([("60", 5.0)], "test-engine"),
    )
    with session_scope() as session:
        src = session.scalars(
            __import__("sqlalchemy").select(Source).where(Source.chat_id == "oc_norm1")
        ).first()
        # 非精确查询：只考察归一化本身，避开 exact 钳制路径
        result = exp_module.find_best_match(session, "年假申请的流程说明", source=src)
    # 归一化 5.0 → 0.8333；无精确命中时不抬升
    assert result.score < 0.85
    assert "exact_pattern_match" not in result.reasons


def test_variant_0978_below_draft_threshold(p2p_source, monkeypatch):
    """实测误触发场景：近义原始 0.978 → 归一化 0.494 < 0.55，必须落在草稿阈值之下。"""
    from app.memory import experience as exp_module

    _make(61, ExperienceStatus.APPROVED_AUTO, 4)
    monkeypatch.setattr(
        exp_module.retrieval, "rank",
        lambda query, corpus: ([("61", 0.978)], "test-engine"),
    )
    with session_scope() as session:
        src = session.scalars(
            __import__("sqlalchemy").select(Source).where(Source.chat_id == "oc_norm1")
        ).first()
        result = exp_module.find_best_match(session, "年假要怎么申请呀", source=src)
    assert result.score < 0.55
    assert "below_draft_threshold" in result.reasons


def test_exact_match_still_clamps_to_095(p2p_source, monkeypatch):
    """精确匹配钳制不受归一化影响：原始分任意，exact 仍 0.95。"""
    from app.memory import experience as exp_module

    _make(62, ExperienceStatus.APPROVED_AUTO, 4)
    monkeypatch.setattr(
        exp_module.retrieval, "rank",
        lambda query, corpus: ([("62", 1.574)], "test-engine"),
    )
    with session_scope() as session:
        src = session.scalars(
            __import__("sqlalchemy").select(Source).where(Source.chat_id == "oc_norm1")
        ).first()
        result = exp_module.find_best_match(session, "请问年假怎么申请？", source=src)
    assert result.score >= 0.95
    assert "exact_pattern_match" in result.reasons


def test_normalization_monotonic_order_preserved(p2p_source, monkeypatch):
    """保序：归一化单调，高原始分经验仍排前（tie_rank 在归一化后仍成立）。"""
    from app.memory import experience as exp_module

    _make(63, ExperienceStatus.APPROVED_AUTO, 1, intent_id="intent-a")
    _make(64, ExperienceStatus.APPROVED_AUTO, 1, intent_id="intent-b")
    monkeypatch.setattr(
        exp_module.retrieval, "rank",
        lambda query, corpus: ([("63", 2.0), ("64", 0.5)], "test-engine"),
    )
    with session_scope() as session:
        src = session.scalars(
            __import__("sqlalchemy").select(Source).where(Source.chat_id == "oc_norm1")
        ).first()
        # 非精确查询：只验证归一化保序，避开 exact 钳制路径（查询==pattern 会合法钳到 0.95）
        result = exp_module.find_best_match(session, "年假申请的流程说明", source=src)
    assert result.experience.id == 63
    # 2.0 → 2/3；0.5 → 1/3
    assert abs(result.score - 2.0 / 3.0) < 1e-6
