"""诊断：用真实 .env 配置执行一次准入判定，打印原因（不改数据）。"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from app.core.config import get_settings  # noqa: E402
from app.core.database import session_scope  # noqa: E402
from app.domain import admission  # noqa: E402
from app.handlers.events import persist_event  # noqa: E402
from tests.helpers import incoming  # noqa: E402


def main() -> int:
    settings = get_settings()
    chat = settings.feishu_owner_chat_id or "oc_db403b7c20788b53e46d128e1d185e5b"
    msg = incoming("请问年假怎么申请？", "om_diag_1", chat_id=chat, sender="ou_diag_user")
    print("chat", chat, "whitelisted", settings.is_whitelisted(chat))
    print("bot_open_id", settings.feishu_bot_open_id)
    with session_scope() as session:
        result = admission.admit(session, msg)
        print("admit ->", result.accepted, result.reason, result.should_process,
              "source", getattr(result.source, "chat_id", None),
              getattr(result.source, "source_type", None))
    accepted, reason, should_process = persist_event(msg)
    print("persist ->", accepted, reason, should_process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
