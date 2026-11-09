"""日报卡片渲染回归：积压再大也不能超过飞书卡片长度上限。"""
from __future__ import annotations

import json

from app.core.database import session_scope
from app.feishu.cards import build_report_card
from app.models import DailyReport


def _report(session, content: dict) -> DailyReport:
    report = DailyReport(
        report_key="report-test-1",
        tenant="default",
        owner="ou_owner",
        logical_date=content.get("date", "2026-11-09"),
        timezone="Asia/Shanghai",
        window_start_ms=0,
        window_end_ms=1,
        content_json=json.dumps(content, ensure_ascii=False),
        content_hash="hash",
    )
    session.add(report)
    session.flush()
    return report


def _large_content(count: int = 120) -> dict:
    return {
        "date": "2026-11-09",
        "summary": {
            "tasks": count,
            "pending": count,
            "sent": count,
            "experience_changes": count,
            "gaps": count,
        },
        "tasks": [
            {
                "id": i,
                "title": "任务标题" * 30,
                "due": "2026-11-10",
                "needs_confirmation": i % 2 == 0,
                "sources": [{"text": "来源原文" * 80}],
            }
            for i in range(count)
        ],
        "pending_replies": [
            {
                "id": i,
                "sender": "ou_sender",
                "question": "问题内容" * 80,
                "content": "拟回复内容" * 80,
                "score": 0.9,
                "experience_id": i,
                "version": 1,
                "shadow": False,
            }
            for i in range(count)
        ],
        "sent_replies": [
            {"id": i, "question": "问题内容" * 80, "content": "回复内容" * 80, "receipt": "om_" + "x" * 24}
            for i in range(count)
        ],
        "experience_changes": [
            {
                "experience_id": i,
                "version": 1,
                "status": "candidate",
                "action": "新增",
                "question": "经验问题" * 40,
            }
            for i in range(count)
        ],
        "gaps": [
            {"delivery": "delivery", "target": "target", "status": "failed", "error": "错误" * 80}
            for i in range(count)
        ],
    }


def test_report_card_stays_under_feishu_limit_with_large_backlog(settings):
    with session_scope() as session:
        report = _report(session, _large_content())

        card = build_report_card(session, report)
        raw = json.dumps(card, ensure_ascii=False)

    assert len(raw.encode("utf-8")) <= settings.daily_card_max_bytes
    assert "待批 120" in raw
    assert raw.count("未展示") == 5
    assert len(card["elements"]) < 100


def test_report_card_shrinks_to_tighter_budget(settings, monkeypatch):
    monkeypatch.setattr(settings, "daily_card_max_bytes", 8000)
    with session_scope() as session:
        report = _report(session, _large_content())

        card = build_report_card(session, report)
        raw = json.dumps(card, ensure_ascii=False)

    assert len(raw.encode("utf-8")) <= 8000
    assert "#0" in raw


def test_small_report_keeps_all_items(settings):
    content = _large_content(count=2)
    with session_scope() as session:
        report = _report(session, content)

        card = build_report_card(session, report)
        raw = json.dumps(card, ensure_ascii=False)

    assert "未展示" not in raw
    assert raw.count("#0") >= 3
    assert raw.count("#1") >= 3
