"""审计链：协同层每一个派活/催办/报告动作落 audit.jsonl（L9041）。

责任边界原则（docs/DIGITAL-EMPLOYEE-PLAYBOOK.md §4）：
协同层代人派活，必须可追溯——谁（actor）、经什么入口（channel）、
触发哪条消息（source_message_id）、产生了什么结果（result）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from integration.config import DATA

AUDIT = DATA / "audit.jsonl"


def record(action: str, *, actor: str = "", channel: str = "",
           source_message_id: str = "", result: str = "",
           item_id: str = "", **extra) -> None:
    """追加一条审计记录。失败只打印，不影响主流程。"""
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action,
        "actor": actor or "feishu-owner",
        "channel": channel or "feishu",
        "sourceMessageId": source_message_id,
        "itemId": item_id,
        "result": result,
    }
    entry.update(extra)
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[audit] write failed: {exc}")
