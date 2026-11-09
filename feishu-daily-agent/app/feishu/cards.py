"""飞书卡片 1.0：日报、待办、待批回复、经验与缺口。

卡片 2.0 需要飞书 V7.20+ 客户端，网页版会降级成“请升级客户端”占位；
1.0 在所有客户端可渲染且同样支持卡片回调。按钮只携带一次性 action_id；
操作者、目标、版本与有效期都在数据库令牌里。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain import review as review_module
from app.models import DailyReport


def _md(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _button(label: str, btn_type: str, action_id: str) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": btn_type,
        "value": {"action_id": action_id},
    }


def _actions(*buttons: dict) -> dict:
    return {"tag": "action", "actions": list(buttons)}


def _token(
    session: Session,
    report: DailyReport,
    *,
    action: str,
    target_type: str,
    target_id: int | str,
    target_version: int = 0,
    payload: dict | None = None,
) -> str:
    token = review_module.create_action_token(
        session,
        owner=report.owner,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_version=target_version,
        card_id=report.report_key,
        payload=payload,
        ttl_minutes=get_settings().draft_ttl_hours * 60,
        tenant=report.tenant,
    )
    return token.action_id


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _omitted(shown: int, total: int, noun: str) -> list[dict]:
    if total <= shown:
        return []
    return [_md(f"…另有 {total - shown} 条{noun}未展示（共 {total} 条）。")]


def _render_report_elements(
    session: Session, report: DailyReport, content: dict, limit: int
) -> tuple[list[dict], str]:
    elements: list[dict] = []
    summary = content.get("summary", {})

    elements.append(_md("**我待处理**"))
    tasks = content.get("tasks") or []
    if tasks:
        for task in tasks[:limit]:
            marker = "〔待确认〕" if task.get("needs_confirmation") else ""
            due = f"｜截止 {task['due']}" if task.get("due") else ""
            source = task["sources"][0]["text"] if task.get("sources") else ""
            elements.append(
                _md(
                    f"- {marker}{_clip(task['title'], 120)}{due}\n"
                    f"  来源：{_clip(source, 80) or '无原文'}"
                )
            )
            elements.append(
                _actions(
                    _button(
                        "标记完成", "primary",
                        _token(
                            session, report, action="complete_task",
                            target_type="task", target_id=task["id"],
                        ),
                    )
                )
            )
        elements.extend(_omitted(limit, len(tasks), "待办"))
    else:
        elements.append(_md("暂无待办。"))
    elements.append({"tag": "hr"})

    elements.append(_md("**待批回复**"))
    pending = content.get("pending_replies") or []
    if pending:
        for draft in pending[:limit]:
            tag = "〔影子〕" if draft.get("shadow") else ""
            elements.append(
                _md(
                    f"- #{draft['id']} {tag}来自 {_clip(draft.get('sender') or '未知', 40)}：{_clip(draft.get('question'), 160)}\n"
                    f"  拟回复：{_clip(draft.get('content'), 160)}\n"
                    f"  相似度 {draft['score']}｜经验 {draft.get('experience_id') or '无'}"
                )
            )
            elements.append(
                _md(f"修改：发送「改 #{draft['id']} 新内容」给机器人")
            )
            elements.append(
                _actions(
                    _button(
                        "批准", "primary",
                        _token(
                            session, report, action="approve",
                            target_type="draft", target_id=draft["id"],
                            target_version=draft["version"],
                        ),
                    ),
                    _button(
                        "驳回", "danger",
                        _token(
                            session, report, action="reject",
                            target_type="draft", target_id=draft["id"],
                            target_version=draft["version"],
                        ),
                    ),
                )
            )
        elements.extend(_omitted(limit, len(pending), "待批回复"))
    else:
        elements.append(_md("暂无待批回复。"))
    elements.append({"tag": "hr"})

    elements.append(_md("**已发回复**"))
    sent_replies = content.get("sent_replies") or []
    if sent_replies:
        for sent in sent_replies[:limit]:
            receipt = _clip(sent.get("receipt") or "回执未知", 32)
            elements.append(
                _md(
                    f"- #{sent['id']}：{_clip(sent.get('question'), 160)}\n"
                    f"  回复：{_clip(sent.get('content'), 160)}\n"
                    f"  回执：{receipt}"
                )
            )
            elements.append(
                _md(f"纠正：发送「纠正 #{sent['id']} 新内容」给机器人")
            )
        elements.extend(_omitted(limit, len(sent_replies), "已发回复"))
    else:
        elements.append(_md("昨日无已发回复。"))
    elements.append({"tag": "hr"})

    elements.append(_md("**经验变更**"))
    changes = content.get("experience_changes") or []
    if changes:
        for change in changes[:limit]:
            elements.append(
                _md(
                    f"- 经验 #{change['experience_id']} v{change['version']} "
                    f"[{change['status']}] {change['action']}：{_clip(change['question'], 120)}"
                )
            )
            if change["status"] == "candidate":
                elements.append(
                    _actions(
                        _button(
                            "验证并允许受限自动回复", "primary",
                            _token(
                                session, report, action="verify_experience",
                                target_type="experience", target_id=change["experience_id"],
                                target_version=change["version"],
                            ),
                        )
                    )
                )
            elif change["status"] == "approved_auto":
                elements.append(
                    _actions(
                        _button(
                            "暂停", "danger",
                            _token(
                                session, report, action="pause_experience",
                                target_type="experience", target_id=change["experience_id"],
                                target_version=change["version"],
                            ),
                        )
                    )
                )
        elements.extend(_omitted(limit, len(changes), "经验变更"))
    else:
        elements.append(_md("无经验变更。"))
    elements.append({"tag": "hr"})

    gaps = content.get("gaps") or []
    if gaps:
        elements.append(_md("**采集与投递缺口**"))
        for gap in gaps[:limit]:
            if gap.get("delivery"):
                elements.append(
                    _md(
                        f"- {_clip(gap['delivery'], 60)} {_clip(gap['target'], 40)}：{gap['status']} {_clip(gap.get('error'), 100)}"
                    )
                )
            else:
                elements.append(
                    _md(
                        f"- 会话 {gap.get('chat_id')}：{gap.get('status')}"
                        f"{'（已撤权）' if gap.get('revoked') else ''} {_clip(gap.get('error'), 100)}"
                    )
                )
        elements.extend(_omitted(limit, len(gaps), "缺口"))
        elements.append({"tag": "hr"})

    summary_line = (
        f"待办 {summary.get('tasks', 0)}｜待批 {summary.get('pending', 0)}"
        f"｜已发 {summary.get('sent', 0)}｜经验 {summary.get('experience_changes', 0)}"
        f"｜缺口 {summary.get('gaps', 0)}"
    )
    return elements, summary_line


def build_report_card(session: Session, report: DailyReport) -> dict:
    """日报卡片：每类明细限量展示，整体压到飞书卡片长度上限以内。

    历史实现把全部待批/待办塞进一张卡，积压到 457 条时 payload 达 290KB，
    飞书返回 230025 导致日报连续投递失败。超过上限时逐级减少明细条数。
    """
    settings = get_settings()
    content = json.loads(report.content_json or "{}")
    limit = max(1, int(settings.daily_card_max_items or 6))
    max_bytes = max(4000, int(settings.daily_card_max_bytes or 24000))
    attempts = [limit]
    while attempts[-1] > 1:
        attempts.append(max(1, attempts[-1] // 2))
    card: dict = {}
    for attempt in attempts:
        elements, summary_line = _render_report_elements(session, report, content, attempt)
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"飞书日报 · {content.get('date', '')}"},
                "template": "blue",
            },
            "elements": [_md(summary_line)] + elements,
        }
        if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= max_bytes:
            break
    return card


def build_daily_card(date_label: str, tasks: List[dict], auto_replies: List[dict],
                     new_experiences: List[dict], pending_count: int) -> dict:
    """兼容旧调用：只用于展示，不生成动作按钮。"""
    sections = [
        _md("**我待处理**"),
        _md("\n".join(f"- {t.get('text', '')}" for t in tasks) or "暂无待办。"),
        {"tag": "hr"},
        _md("**待批回复**"),
        _md(f"{pending_count} 条待批。"),
        {"tag": "hr"},
        _md("**已发回复**"),
        _md("\n".join(f"- #{r.get('draft_id')}：{r.get('content', '')}" for r in auto_replies) or "无。"),
        {"tag": "hr"},
        _md("**经验变更**"),
        _md("\n".join(f"- #{e.get('id')}：{e.get('question_pattern', '')}" for e in new_experiences) or "无。"),
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"飞书日报 · {date_label}"}},
        "elements": sections,
    }


def build_draft_card(draft: dict) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"待审批回复 #{draft.get('draft_id')}"}},
        "elements": [
            _md(f"**问题**：{draft.get('question', '')}\n**拟回复**：{draft.get('content', '')}")
        ],
    }


def replace_draft_card_result(card: dict, approved: bool, note: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": card.get("header", {}).get("title", {}),
            "template": "green" if approved else "red",
        },
        "elements": [_md(f"**{'已批准' if approved else '已驳回'}**\n{note}")],
    }


def conclusion_card(report_key: str, title: str, body: str, template: str = "green") -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": [_md(body), _md(f"日报：{report_key}")],
    }
