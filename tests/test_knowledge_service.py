"""DE-16：知识主张的来源、冲突保留、权限裁剪与源失效。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evoharness.clock import utcnow
from evoharness.knowledge import service
from evoharness.knowledge.service import KnowledgeError
from evoharness.models import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


def _claim(s, subject="退款规则", value="7 天无理由", scope="private", refs=("doc:policy@v3",), **kw):
    return service.record_claim(s, space_id="support", owner_scope=scope, subject=subject,
                                value=value, source_refs=list(refs), **kw)


def test_claim_requires_explicit_source(session):
    with pytest.raises(KnowledgeError, match="claim_source_required"):
        service.record_claim(session, space_id="support", subject="规则", value="x",
                             source_refs=[])
    with pytest.raises(KnowledgeError, match="claim_subject_required"):
        service.record_claim(session, space_id="support", subject="  ", value="x",
                             source_refs=["msg:m1@2"])


def test_conflicting_rules_are_kept_with_sources(session):
    first = _claim(session, value="7 天无理由")
    second = _claim(session, value="30 天无理由", refs=("msg:owner-decision@5",))
    # 两份冲突规则并存，各自保留来源；不因新笔记删旧权威
    result = service.search(session, space_id="support", subject="退款规则",
                            owner_scope="private", include_private=True)
    values = {c["value"] for c in result["claims"]}
    assert values == {"7 天无理由", "30 天无理由"}
    assert set(result["conflicts"]) == {first.claim_id, second.claim_id}
    # 每条答案可追到授权来源
    for c in result["claims"]:
        assert c["source_refs"]


def test_explicit_supersede_keeps_history(session):
    first = _claim(session)
    replacement = _claim(session, value="15 天无理由", refs=("doc:policy@v4",),
                         supersedes=first.claim_id)
    session.refresh(first)
    assert first.state == "superseded"
    # supersede 只能同主体同谓词
    with pytest.raises(KnowledgeError, match="supersede_scope_mismatch"):
        service.record_claim(session, space_id="support", subject="其他规则", value="x",
                             source_refs=["doc:p@v1"], supersedes=first.claim_id)
    result = service.search(session, space_id="support", subject="退款规则",
                            owner_scope="private", include_private=True)
    assert [c["claim_id"] for c in result["claims"]] == [replacement.claim_id]
    assert result["claims"][0]["supersedes"] == first.claim_id


def test_visibility_is_scope_cropped_not_public_by_default(session):
    _claim(session, value="私有口径")
    _claim(session, value="公开口径", scope="public", refs=("doc:faq@v1",))
    # 未知/未授权主体：只见公开主张，私有不默认公开
    public_only = service.search(session, space_id="support", subject="退款规则")
    assert [c["value"] for c in public_only["claims"]] == ["公开口径"]
    authorized = service.search(session, space_id="support", subject="退款规则",
                                 owner_scope="private", include_private=True)
    assert len(authorized["claims"]) == 2


def test_expired_claims_hint_requery_instead_of_answering(session):
    _claim(session, value="旧指标", valid_until=utcnow() - timedelta(days=1))
    result = service.search(session, space_id="support", subject="退款规则",
                            owner_scope="private", include_private=True)
    assert result["claims"] == []                    # 过期不作为答案
    assert result["expired_hint"]                    # 提示回源查询


def test_source_revocation_invalidates_without_deleting(session):
    first = _claim(session)
    other = _claim(session, subject="另一规则", refs=("doc:other@v1",))
    invalidated = service.invalidate_for_source(session, source_ref="doc:policy@v3",
                                                reason="来源撤权")
    assert invalidated == [first.claim_id]
    session.refresh(first)
    session.refresh(other)
    assert first.state == "stale" and "stale:来源撤权" in first.confidence_basis
    assert other.state == "active"
    # 正文与历史保留（不物理删除，可审计）
    assert first.value == "7 天无理由"
    result = service.search(session, space_id="support", subject="退款规则",
                            owner_scope="private", include_private=True)
    assert result["claims"] == []
