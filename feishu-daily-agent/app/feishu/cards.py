"""飞书卡片 1.0：日报、待办、待批回复、经验与缺口。

卡片 2.0 需要飞书 V7.20+ 客户端，网页版会降级成“请升级客户端”占位；
1.0 在所有客户端可渲染且同样支持卡片回调。按钮只携带一次性 action_id；
操作者、目标、版本与有效期都在数据库令牌里。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import List

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain import review as review_module
from app.models import DailyReport


def _md(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _button(label: str, btn_type: str, action_id: str) -> dict | None:
    """构造按钮；没有有效 action_id 时返回 None（不发出点不动的按钮）。

    2026-11-10 实机：令牌 owner 为空时卡片照样渲染出按钮，owner 点了必然
    被 `operator_mismatch` 拒绝——比没有按钮更糟，因为它诱导人去点。
    """
    if not action_id:
        return None
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": btn_type,
        "value": {"action_id": action_id},
    }


def _actions(*buttons: dict | None) -> dict | None:
    usable = [b for b in buttons if b]
    return {"tag": "action", "actions": usable} if usable else None


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
    # 令牌 owner 必须是**有权批准这条的人**（open_id）。
    # 产品定位（2026-11-11）：待批归被提问者——只有他知道该怎么答；
    # 管理员保留兜底权（提问者已离职/失效时仍能推进）。
    #
    # 历史缺陷（2026-11-10 实机）：owner 只配了 chat_id 时 report.owner 是 oc_ 值，
    # 令牌 owner 为空/错值，卡片看起来正常但点不动。这里优先用草稿归属人。
    from app.services.owner_bootstrap import resolve_owner_open_id

    approver = ""
    if target_type == "draft":
        try:
            from app.models import ReplyDraft

            draft = session.get(ReplyDraft, int(target_id))
            if draft is not None:
                approver = (draft.approver_id or "").strip()
        except (TypeError, ValueError):
            approver = ""
    admin = resolve_owner_open_id(session) or (
        report.owner if (report.owner or "").startswith("ou_") else ""
    )
    # 没有归属人时退回管理员（历史草稿、无法判定提问者的场景）
    owner = approver or admin
    if not owner:
        # 宁可让卡片不带按钮，也不要发出一个"点了必然被拒"的按钮。
        return ""
    token = review_module.create_action_token(
        session,
        owner=owner,
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


def _summary_sentence(content: dict) -> str:
    """人话总结：数字嵌进自然句式，只数需要人知道的事。

    影子草稿单列一项（需要人知晓但不需要动作）；投递异常只在存在时提及。
    全部为空时给"一切正常"心跳句，替代旧版"待办 0｜待批 0｜…"数字串。
    """
    tasks = content.get("tasks") or []
    pending_all = content.get("pending_replies") or []
    pending = [d for d in pending_all if not d.get("shadow")]
    shadow = [d for d in pending_all if d.get("shadow")]
    sent = content.get("sent_replies") or []
    changes = content.get("experience_changes") or []
    gaps = content.get("gaps") or []

    def gap_clause() -> str:
        total = sum(int(g.get("count") or 1) for g in gaps)
        classes = len(gaps)
        return (
            f"另有 {total} 项"
            + (f"（{classes} 类）" if classes > 1 else "")
            + "投递异常"
        )

    parts: list[str] = []
    if tasks:
        parts.append(f"{len(tasks)} 项待办")
    if pending:
        parts.append(f"{len(pending)} 条回复待批")
    if shadow:
        merged = sum(int(item.get("duplicate_count") or 1) - 1 for item in shadow)
        parts.append(
            f"{len(shadow)} 条影子待观察"
            + (f"（已合并 {merged} 条同题重复）" if merged else "")
        )
    if changes:
        parts.append(f"{len(changes)} 条经验待验证")
    if not parts:
        line = "今日无待处理事项，一切正常。"
        if sent:
            line += f"昨日已发出 {len(sent)} 条回复。"
        if gaps:
            line += gap_clause() + "待排查。"
        return line
    line = "今日 " + "、".join(parts) + "。"
    if gaps:
        line += gap_clause() + "。"
    return line


def _render_report_elements(
    session: Session, report: DailyReport, content: dict, limit: int
) -> tuple[list[dict], str]:
    """日报分节：只渲染有内容的节；按钮只留给真正需要人决策的条目。

    排版原则（docs/digital-employee 三方体验 + DE 卡片重排裁决）：
    - 空节不占位——"暂无待办。"这类占位行在飞书里只会稀释注意力；
    - 待办的「完成」按钮降为 default：一排 primary 按钮会淹没真正需要
      决策的批准/驳回；
    - 逐条重复的指令收敛为节末一句，条目内保留与该条 id 绑定的短指令。
    """
    elements: list[dict] = []
    sections: list[list[dict]] = []

    tasks = content.get("tasks") or []
    if tasks:
        sec: list[dict] = [_md("**我待处理**")]
        for task in tasks[:limit]:
            marker = "〔待确认〕" if task.get("needs_confirmation") else ""
            due = f"｜截止 {task['due']}" if task.get("due") else ""
            source = task["sources"][0]["text"] if task.get("sources") else ""
            sec.append(
                _md(
                    f"- {marker}{_clip(task['title'], 120)}{due}\n"
                    f"  来源：{_clip(source, 80) or '无原文'}"
                )
            )
            sec.append(
                _actions(
                    _button(
                        "完成", "default",
                        _token(
                            session, report, action="complete_task",
                            target_type="task", target_id=task["id"],
                        ),
                    )
                )
            )
        sec.extend(_omitted(limit, len(tasks), "待办"))
        sections.append(sec)

    pending_all = content.get("pending_replies") or []
    if pending_all:
        sec = [_md("**待批回复**")]
        shadow_seen = False
        for draft in pending_all[:limit]:
            tag = "〔影子〕" if draft.get("shadow") else ""
            dup = int(draft.get("duplicate_count") or 1)
            same = f"（×{dup} 同题）" if dup > 1 else ""
            sec.append(
                _md(
                    f"- #{draft['id']} {tag}{same}来自 {_clip(draft.get('sender') or '未知', 40)}：{_clip(draft.get('question'), 160)}\n"
                    f"  拟回复：{_clip(draft.get('content'), 160)}\n"
                    f"  经验 {draft.get('experience_id') or '无'} · 相似 {draft['score']}"
                )
            )
            if draft.get("shadow"):
                shadow_seen = True
            else:
                sec.append(_md(f"修改：发送「改 #{draft['id']} 新内容」给机器人"))
                sec.append(
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
        sec.extend(_omitted(limit, len(pending_all), "待批回复"))
        if shadow_seen:
            # 影子说明每条重复一遍只会稀释注意力，收敛到节末一次
            sec.append(_md("影子草稿：仅记录决策，不生成发送。"))
        sections.append(sec)

    sent_replies = content.get("sent_replies") or []
    if sent_replies:
        sec = [_md("**已发回复**")]
        for sent in sent_replies[:limit]:
            receipt = _clip(sent.get("receipt") or "回执未知", 32)
            sec.append(
                _md(
                    f"- #{sent['id']}：{_clip(sent.get('question'), 160)}\n"
                    f"  回复：{_clip(sent.get('content'), 160)}\n"
                    f"  回执：{receipt}"
                )
            )
        sec.extend(_omitted(limit, len(sent_replies), "已发回复"))
        # 指令收敛：不再逐条重复"纠正"，节末一句即可（条目里的 #id 用于定位）
        sec.append(_md("如需修改已发回复：发送「纠正 #编号 新内容」给机器人。"))
        sections.append(sec)

    changes = content.get("experience_changes") or []
    if changes:
        sec = [_md("**经验变更**")]
        for change in changes[:limit]:
            sec.append(
                _md(
                    f"- 经验 #{change['experience_id']} v{change['version']} "
                    f"[{change['status']}] {change['action']}：{_clip(change['question'], 120)}"
                )
            )
            if change["status"] == "candidate":
                sec.append(
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
                sec.append(
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
        sec.extend(_omitted(limit, len(changes), "经验变更"))
        sections.append(sec)

    gaps = content.get("gaps") or []
    if gaps:
        sec = [_md("**采集与投递异常**")]
        for gap in gaps[:limit]:
            dup = int(gap.get("count") or 1)
            same = f"（×{dup}）" if dup > 1 else ""
            if gap.get("delivery"):
                sec.append(
                    _md(
                        f"- {_clip(gap['delivery'], 60)} {_clip(gap['target'], 40)}{same}：{gap['status']} {_clip(gap.get('error'), 100)}"
                    )
                )
            else:
                sec.append(
                    _md(
                        f"- 会话 {gap.get('chat_id')}{same}：{gap.get('status')}"
                        f"{'（已撤权）' if gap.get('revoked') else ''} {_clip(gap.get('error'), 100)}"
                    )
                )
        sec.extend(_omitted(limit, len(gaps), "异常"))
        sections.append(sec)

    for i, sec in enumerate(sections):
        # 空 token 时 _actions 返回 None（2026-11-10 实机修复）；None 进 elements 会损坏卡片 JSON
        sec[:] = [e for e in sec if e is not None]
        if i:
            elements.append({"tag": "hr"})
        elements.extend(sec)

    return elements, _summary_sentence(content)


def build_report_card(session: Session, report: DailyReport) -> dict:
    """日报卡片：每类明细限量展示，整体压到飞书卡片长度上限以内。

    历史实现把全部待批/待办塞进一张卡，积压到 457 条时 payload 达 290KB，
    飞书返回 230025 导致日报连续投递失败。超过上限时逐级减少明细条数。
    """
    settings = get_settings()
    content = json.loads(report.content_json or "{}")
    date_label = str(content.get("date") or "")
    try:
        simulated = date.fromisoformat(date_label) > datetime.now(
            ZoneInfo(settings.daily_report_timezone)
        ).date()
    except (ValueError, TypeError):
        simulated = False
    title_text = f"飞书日报 · {date_label}{'（模拟）' if simulated else ''}"
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
                "title": {"tag": "plain_text", "content": title_text},
                "template": "turquoise",  # 品牌青绿，与 Web 工作台强调色一致
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
