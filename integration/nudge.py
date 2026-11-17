"""PM 主动推进（L9039）：没人交代，也知道该往哪儿推。

扫描三类卡点并主动推送给 owner：
1. 台账 pending 超 48h 未动（firstSeenRound 是日期字符串，按此估龄）
2. high 严重度且未认领的项
3. 飞书草稿积压（reply_drafts pending 超 48h——等你批示的草稿）

降级：台账/DB 读不到时返回 ok=False + 原因，不推送假数据。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from integration.audit import record as audit_record  # noqa: E402
from integration.config import FEISHU_AGENT  # noqa: E402
import loop as loop_mod  # noqa: E402

STALE_DAYS = 2
DRAFT_STALE_HOURS = 48


def _item_age_days(item: dict) -> float:
    """按 firstSeenRound 里嵌入的日期估龄；解析不了返回 0（不误报）。"""
    raw = str(item.get("firstSeenRound") or "")
    m = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
    if not m:
        return 0.0
    try:
        first = date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return 0.0
    return (date.today() - first).days


def collect() -> dict:
    # 来源健康（DE-21）：读取失败标 unknown、积压尾部年龄可见，不与业务空态混同。
    backlog_read = {"source": "backlog", "ok": True, "error": "", "tail_age_seconds": None}
    try:
        state = loop_mod.load()
    except Exception as exc:  # noqa: BLE001 — 台账读不到时不推送假数据
        backlog_read["ok"] = False
        backlog_read["error"] = f"{type(exc).__name__}: {exc}"[:120]
        state = {}
    stale, high_open = [], []
    for item in state.get("items") or []:
        if item.get("status") != "pending":
            continue
        if item.get("source") == "insight-digital-employee":
            # 内部洞察项由自迭代节奏管理，不进催办（避免每次推送重复噪音）
            continue
        age = _item_age_days(item)
        if age >= STALE_DAYS:
            stale.append({"id": item["id"], "ageDays": age, "title": item.get("title", "")})
        if str(item.get("severity")) == "high":
            high_open.append({"id": item["id"], "title": item.get("title", "")})
    # 积压尾部年龄 = 最老一条未闭合台账项的估龄（loop_report 同口径）。
    if stale:
        backlog_read["tail_age_seconds"] = max(i["ageDays"] for i in stale) * 86400.0

    drafts_stale: list[dict] = []
    drafts_read = {"source": "replyDrafts", "ok": True, "error": "", "tail_age_seconds": None}
    db_path = FEISHU_AGENT / "data" / "agent.db"
    if db_path.is_file():
        try:
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            cutoff = int((time.time() - DRAFT_STALE_HOURS * 3600) * 1000)
            rows = db.execute(
                "SELECT d.id, d.created_at, m.content_text FROM reply_drafts d "
                "JOIN messages m ON m.message_id=d.message_id "
                "WHERE d.status='pending' LIMIT 50").fetchall()
            oldest_hours = None
            for r in rows:
                created = str(r["created_at"] or "")
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(created)
                    hours = (datetime.now() - dt).total_seconds() / 3600
                except ValueError:
                    continue
                oldest_hours = hours if oldest_hours is None else max(oldest_hours, hours)
                if hours >= DRAFT_STALE_HOURS:
                    drafts_stale.append({"id": r["id"], "hoursOld": round(hours),
                                         "text": (r["content_text"] or "")[:60]})
            if oldest_hours is not None:
                drafts_read["tail_age_seconds"] = oldest_hours * 3600.0
            db.close()
        except sqlite3.Error as exc:
            drafts_read["ok"] = False
            drafts_read["error"] = f"sqlite: {exc}"[:120]
    else:
        drafts_read["ok"] = False
        drafts_read["error"] = "agent.db-missing"

    try:
        from evoharness.work.wakeups import source_health
        health = source_health(reads=[backlog_read, drafts_read])
    except Exception:  # noqa: BLE001 — 健康汇总不可用时保留原字段，不阻断催办
        health = {"sources": [], "healthy": True, "tailAgeSeconds": None, "alerts": []}

    return {"stale": stale, "highOpen": high_open, "draftsStale": drafts_stale,
            "sourceHealth": health}


def compose(payload: dict) -> str | None:
    lines = []
    if payload.get("highOpen"):
        lines.append(f"⚠️ {len(payload['highOpen'])} 项 high 待办未处理：")
        lines += [f"  • {i['id']} {i['title'][:48]}" for i in payload["highOpen"][:5]]
    if payload.get("stale"):
        lines.append(f"⏰ {len(payload['stale'])} 项待办已挂 {STALE_DAYS} 天以上：")
        lines += [f"  • {i['id']}（挂 {i['ageDays']} 天）{i['title'][:44]}"
                  for i in payload["stale"][:5]]
    if payload.get("draftsStale"):
        lines.append(f"📝 {len(payload['draftsStale'])} 条日报草稿等你批示超 {DRAFT_STALE_HOURS} 小时：")
        lines += [f"  • 草稿#{d['id']} {d['text'][:40]}" for d in payload["draftsStale"][:5]]
    # 来源健康（DE-21）：来源失联单独亮出，不与业务空态混同（无卡点也要告警）。
    health = payload.get("sourceHealth") or {}
    flagged = False
    for source in health.get("sources") or []:
        if source.get("state") not in {"unknown", "stale"}:
            continue
        if not flagged:
            lines.append("⚠️ 信号源健康异常（读取失败不视为空闲）：")
            flagged = True
        tail = source.get("tailAgeSeconds")
        tail_note = f"，尾部年龄 {int(tail)}s" if tail is not None else ""
        error = f"（{source['error'][:40]}）" if source.get("error") else ""
        lines.append(f"  • {source['source']}: {source['state']}{tail_note}{error}")
    if not lines:
        return None
    lines.insert(0, "🔔 数字员工主动提醒（PM 推进）")
    lines.append("——回复「任务：…」派新活，或在日报卡片批示。")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
