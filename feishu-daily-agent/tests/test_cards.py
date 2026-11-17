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
    # 人话总结必须带上待批规模（旧版为"待批 120"数字串，v2 改为自然句式）
    assert "120 条回复待批" in raw
    assert raw.count("未展示") == 5
    assert len(card["elements"]) < 100
    # 排版降噪回归：逐条重复的纠正指令收敛为节末一句；空节不再占位
    assert raw.count("纠正 #") <= 1
    assert "暂无待办" not in raw


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


def test_future_report_title_marked_simulated(settings):
    content = _large_content(count=1)
    content["date"] = "2099-01-01"
    with session_scope() as session:
        report = _report(session, content)
        card = build_report_card(session, report)
        raw = json.dumps(card, ensure_ascii=False)
    assert "飞书日报 · 2099-01-01（模拟）" in raw


def test_today_report_title_not_marked_simulated(settings):
    from datetime import date

    content = _large_content(count=1)
    content["date"] = date.today().isoformat()
    with session_scope() as session:
        report = _report(session, content)
        card = build_report_card(session, report)
        raw = json.dumps(card, ensure_ascii=False)
    assert "（模拟）" not in raw


def test_shadow_draft_has_observation_note_without_buttons(settings):
    content = _large_content(count=1)
    content["pending_replies"] = [
        {
            "id": 901,
            "sender": "ou_sender",
            "question": "年假要怎么申请呀",
            "content": "影子回复",
            "score": 0.2,
            "experience_id": 1,
            "version": 1,
            "shadow": True,
        },
        {
            "id": 902,
            "sender": "ou_sender",
            "question": "请问年假怎么申请？",
            "content": "正常回复",
            "score": 0.95,
            "experience_id": 1,
            "version": 1,
            "shadow": False,
        },
    ]
    content["summary"]["pending"] = 1
    content["summary"]["shadow_pending"] = 1
    with session_scope() as session:
        report = _report(session, content)

        card = build_report_card(session, report)
        raw = json.dumps(card, ensure_ascii=False)

    assert "影子草稿：仅记录决策，不生成发送。" in raw
    # 影子与待批在人话总结里各自可见（旧版为"待批 1｜影子 1"数字串）
    assert "1 条回复待批" in raw
    assert "1 条影子待观察" in raw
    assert raw.count('"content": "批准"') == 1
    assert '改 #901' not in raw
    assert '改 #902' in raw


def test_shadow_clusters_render_once_with_duplicate_count(settings):
    """影子同题聚类：重复条目只出现代表项，节末说明只出现一次。"""
    content = _large_content(count=1)
    content["pending_replies"] = [
        {
            "id": 911,
            "sender": "ou_abc***",
            "question": "年假要怎么申请呀",
            "content": "影子回复",
            "score": 0.4,
            "experience_id": 1,
            "version": 1,
            "shadow": True,
            "duplicate_count": 3,
        },
        {
            "id": 912,
            "sender": "ou_abc***",
            "question": "报销流程是什么",
            "content": "影子回复",
            "score": 0.2,
            "experience_id": 1,
            "version": 1,
            "shadow": True,
            "duplicate_count": 1,
        },
    ]
    content["summary"]["pending"] = 0
    content["summary"]["shadow_pending"] = 2
    with session_scope() as session:
        report = _report(session, content)

        card = build_report_card(session, report)
        raw = json.dumps(card, ensure_ascii=False)

    assert "（×3 同题）" in raw
    assert raw.count("影子草稿：仅记录决策，不生成发送。") == 1
    assert "2 条影子待观察（已合并 2 条同题重复）" in raw


def test_gap_clusters_render_count_and_masked_target(settings):
    """同类投递异常聚合成一行：带 ×N 与脱敏目标，不再逐条刷屏。"""
    content = _large_content(count=1)
    content["pending_replies"] = []
    content["gaps"] = [
        {"delivery": "reply", "target": "ou_abc***", "status": "failed",
         "error": "timeout", "count": 37},
    ]
    with session_scope() as session:
        report = _report(session, content)

        card = build_report_card(session, report)
        raw = json.dumps(card, ensure_ascii=False)

    assert "（×37）" in raw
    assert "ou_abc***" in raw
    assert "另有 37 项投递异常" in raw


def test_no_buttons_when_owner_open_id_unresolvable(settings, monkeypatch):
    """回归（2026-11-10 实机）：owner 只配了 chat_id 时，令牌 owner 曾为空，
    卡片照样渲染按钮，owner 一点就被 operator_mismatch 拒绝。

    现在的契约：解析不到 open_id 就不发按钮——宁缺勿滥，避免诱导点击。
    """
    from app.services import owner_bootstrap

    monkeypatch.setattr(settings, "feishu_owner_open_id", "")
    monkeypatch.setattr(settings, "feishu_owner_chat_id", "oc_owner_chat")
    monkeypatch.setattr(
        owner_bootstrap, "resolve_owner_open_id", lambda session: ""
    )

    content = _large_content(count=1)
    content["pending_replies"] = [
        {
            "id": 911,
            "sender": "ou_sender",
            "question": "请问年假怎么申请？",
            "content": "候选回复",
            "score": 0.95,
            "experience_id": 1,
            "version": 1,
            "shadow": False,
        }
    ]
    content["tasks"][0]["status"] = "open"
    content["summary"]["pending"] = 1
    with session_scope() as session:
        report = _report(session, content)
        report.owner = "oc_owner_chat"  # 只配 chat_id 的真实情形
        card = build_report_card(session, report)
        raw = json.dumps(card, ensure_ascii=False)

    assert '"tag": "button"' not in raw
    assert '"content": "批准"' not in raw
    # 内容本身仍要完整呈现，只是不能点
    assert "候选回复" in raw


def test_buttons_use_resolved_open_id_not_chat_id(settings, monkeypatch):
    """令牌必须绑定真实操作者 open_id。"""
    from app.services import owner_bootstrap
    from app.models import ActionToken

    monkeypatch.setattr(
        owner_bootstrap, "resolve_owner_open_id", lambda session: "ou_real_owner"
    )
    content = _large_content(count=1)
    content["pending_replies"] = [
        {
            "id": 912,
            "sender": "ou_sender",
            "question": "请问年假怎么申请？",
            "content": "候选回复 2",
            "score": 0.95,
            "experience_id": 1,
            "version": 1,
            "shadow": False,
        }
    ]
    content["tasks"][0]["status"] = "open"
    with session_scope() as session:
        report = _report(session, content)
        report.owner = "oc_owner_chat"
        card = build_report_card(session, report)
        raw = json.dumps(card, ensure_ascii=False)
        owners = {
            t.owner for t in session.query(ActionToken).all()
        }
    assert '"tag": "button"' in raw
    assert owners == {"ou_real_owner"}, owners
