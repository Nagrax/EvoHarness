"""飞书日报与经验回复 Agent 入口。

用法：
    python main.py            # 启动完整服务（长连接 + 定时日报）
    python main.py daily      # 手动执行一次每日流水线
    python main.py worker     # 只跑 outbox 重试 worker
    python main.py health     # 健康探针（非零退出表示不健康）
    python main.py init-db    # 仅初始化数据库表
"""
from __future__ import annotations

import argparse
import json
import time

from app.core.config import get_settings
from app.core.database import engine, session_scope
from app.core.logging import setup_logging
from app.domain import health
from app.domain import outbox as outbox_module
from app.models import Base
from app.pipeline.daily import run_daily_pipeline
from app.pipeline.scheduler import start_scheduler, stop_scheduler


def init_db() -> None:
    from app.core.migrations import migrate

    migrate(engine)


def _message_recalled(data) -> None:
    from app.core.database import session_scope
    from app.domain import intake

    event = data.event
    with session_scope() as session:
        intake.mark_recalled(
            session,
            tenant=get_settings().tenant_id,
            message_id=getattr(event, "message_id", ""),
            event_time_ms=int(getattr(event, "recall_time", 0) or 0),
        )


def run_service() -> None:
    import lark_oapi as lark

    from app.handlers.card_callback import handle_card_action
    from app.handlers.events import handle_message_event

    settings = get_settings()
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message_event)
        .register_p2_im_message_recalled_v1(_message_recalled)
        .register_p2_card_action_trigger(handle_card_action)
        .build()
    )
    start_scheduler()
    ws_client = lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    try:
        ws_client.start()  # 阻塞，直到进程结束
    finally:
        stop_scheduler()


def run_manual_daily() -> dict:
    """手动日报也是一次真实流水线执行：写 daily 心跳，避免健康探针误报漏跑。

    调度器 `_daily_job` 与本函数共用 heartbeat("daily", ...) 语义；失败同样落
    status=error 心跳，供健康探针与值守区分"漏跑"与"跑了但失败"。
    """
    try:
        summary = run_daily_pipeline()
    except Exception as exc:  # noqa: BLE001 — 失败也要落心跳
        health.heartbeat("daily", {"status": "error", "error": str(exc), "trigger": "manual"})
        raise
    health.heartbeat("daily", {"status": "ok", "summary": summary, "trigger": "manual"})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书日报与经验回复 Agent")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "daily", "worker", "health", "init-db"],
    )
    args = parser.parse_args()
    setup_logging()
    init_db()

    if args.command == "init-db":
        print("数据库表已初始化")
    elif args.command == "daily":
        print(json.dumps(run_manual_daily(), ensure_ascii=False, indent=2))
    elif args.command == "worker":
        while True:
            outbox_module.dispatch_outbox(
                __import__("app.core.database", fromlist=["SessionLocal"]).SessionLocal,
                limit=20,
            )
            time.sleep(30)
    elif args.command == "health":
        with session_scope() as session:
            report = health.probe(session)
        print(json.dumps({"ok": report.ok, "issues": report.issues, **report.details}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report.ok else 1)
    else:
        run_service()


if __name__ == "__main__":
    main()
