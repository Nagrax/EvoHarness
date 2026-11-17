"""日报治理：影子草稿同题聚类去重 + sender 脱敏。"""
from __future__ import annotations

from app.core.database import session_scope
from app.domain.reports import _coverage, cluster_shadow_drafts, mask_sender
from app.models import Outbox, OutboxStatus


def test_mask_sender_hides_open_ids_but_keeps_names():
    assert mask_sender("ou_1234567890abcdef") == "ou_123***"
    assert mask_sender("abcdefghijklmnopqrst") == "abcdef***"
    assert mask_sender("张三") == "张三"
    assert mask_sender("") == ""


def test_shadow_drafts_cluster_same_question_and_keep_pending():
    items = [
        {"id": 1, "shadow": True, "question": "年假要怎么申请呀？",
         "score": 0.2, "duplicate_count": 1},
        {"id": 2, "shadow": True, "question": "年假要怎么申请呀",
         "score": 0.5},
        {"id": 3, "shadow": False, "question": "年假要怎么申请呀",
         "score": 0.9},
        {"id": 4, "shadow": True, "question": "报销流程是什么",
         "score": 0.3},
    ]

    out = cluster_shadow_drafts(items)

    # 待批草稿逐条保留；影子同题只留代表条目，并累计重复量与最高相似度
    assert [item["id"] for item in out] == [1, 3, 4]
    assert out[0]["duplicate_count"] == 2
    assert out[0]["score"] == 0.5
    assert out[2]["duplicate_count"] == 1


def test_coverage_aggregates_identical_delivery_failures(settings):
    tenant = "governance-test"
    with session_scope() as session:
        for i, target in enumerate([
            "ou_aaaaaaaaaaaaaaaa", "ou_bbbbbbbbbbbbbbbb", "ou_cccccccccccccccc",
        ]):
            session.add(Outbox(
                intent_key=f"{tenant}-{i}", tenant=tenant, kind="reply",
                target_type="message", target_id=target, payload_json="{}",
                status=OutboxStatus.FAILED, last_error="timeout",
            ))
    with session_scope() as session:
        gaps = _coverage(session, tenant)

    deliveries = [gap for gap in gaps if gap.get("delivery")]
    assert len(deliveries) == 1
    assert deliveries[0]["count"] == 3
    assert deliveries[0]["target"] == "ou_aaa***"
