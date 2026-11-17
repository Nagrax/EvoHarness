"""实测：群消息在「进群 / @ / 白名单」各组合下的真实下场。

只读诊断——用假传输与隔离数据库判定准入与处理分支，不连飞书、不改生产库。
"""
from __future__ import annotations

import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

import os  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="grp-probe-")
os.environ["FEISHU_TEST_MODE"] = "1"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.replace(chr(92), '/')}/probe.db"
os.environ["FEISHU_OWNER_OPEN_ID"] = "ou_owner"
os.environ["FEISHU_OWNER_CHAT_ID"] = "oc_owner_p2p"
os.environ["FEISHU_BOT_OPEN_ID"] = "ou_bot"
os.environ["FEISHU_DEPLOYMENT_MODE"] = "single_approval"
os.environ["FEISHU_SHADOW_MODE"] = "0"
os.environ["FEISHU_AUTO_REPLY_ENABLED"] = "0"
os.environ["FEISHU_GROUP_REPLY_ONLY_MENTION"] = "1"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9"
os.environ["FEISHU_ALLOW_LIVE_MODEL"] = "0"

from app.core.config import get_settings, reset_settings_cache  # noqa: E402

reset_settings_cache()

from app.core.database import build_engine, configure_engine  # noqa: E402
from app.models import Base  # noqa: E402

configure_engine(os.environ["DATABASE_URL"], test_mode=True)
from app.core.database import engine  # noqa: E402

Base.metadata.create_all(engine)

from app.core.database import session_scope  # noqa: E402
from app.domain import admission  # noqa: E402
from app.models import Message, ReplyDraft, Task  # noqa: E402
from tests.helpers import incoming  # noqa: E402

GROUP = "oc_group_probe"


def run(label: str, *, whitelisted: bool, at_bot: bool) -> None:
    settings = get_settings()
    settings.feishu_chat_whitelist = f"oc_owner_p2p,{GROUP}" if whitelisted else "oc_owner_p2p"
    settings.feishu_reply_allowed_chats = settings.feishu_chat_whitelist
    settings.feishu_chat_blacklist = ""
    with session_scope() as session:
        admission.ensure_source(
            session, tenant=settings.tenant_id, owner="ou_owner", chat_id=GROUP,
            source_type="group", reply_allowed=True,
        )
    from app.handlers.events import persist_event

    mid = f"om_probe_{abs(hash(label)) % 100000}"
    accepted, reason, should_process = persist_event(
        incoming("请问年假怎么申请？", mid, chat_id=GROUP, chat_type="group",
                 sender="ou_someone", mentions=["ou_bot"] if at_bot else [])
    )
    with session_scope() as session:
        categorized = session.query(Message).filter(Message.message_id == mid).one_or_none()
        drafts = session.query(ReplyDraft).filter(ReplyDraft.message_id == mid).count()
        tasks = session.query(Task).filter(Task.chat_id == GROUP).count()
    # persist_event 只做准入+入库；真正产生草稿的是后续处理（线上由线程池触发）。
    if should_process:
        from app.services.reply_engine import process_incoming

        with session_scope() as session:
            draft = process_incoming(
                session,
                incoming("请问年假怎么申请？", mid, chat_id=GROUP, chat_type="group",
                         sender="ou_someone", mentions=["ou_bot"] if at_bot else []),
            )
            produced = draft is not None
    else:
        produced = False
    with session_scope() as session:
        drafts = session.query(ReplyDraft).filter(ReplyDraft.message_id == mid).count()
    print(f"{label}")
    print(f"  准入={accepted!s:5s} 原因={reason}")
    print(f"  会进入处理链路={should_process}")
    print(f"  入库={'是' if categorized else '否'} "
          f"分类={categorized.category if categorized else '-'} "
          f"生成草稿={'是' if produced else '否'}（{drafts} 条）")
    print()


def main() -> int:
    run("① 机器人进群 + 群在白名单 + 不 @", whitelisted=True, at_bot=False)
    run("② 机器人进群 + 群在白名单 + @机器人", whitelisted=True, at_bot=True)
    run("③ 机器人进群 + 群不在白名单 + @机器人", whitelisted=False, at_bot=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
