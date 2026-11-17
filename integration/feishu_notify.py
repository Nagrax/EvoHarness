"""Control-service notifications use explicit modes and the existing durable outbox."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from integration.config import OUTBOX, FEISHU_AGENT, feishu_database_path, feishu_live, feishu_owner_target, load_feishu_env


class NotificationConfigurationError(RuntimeError):
    pass


def _dry_run(title: str, text: str) -> Path:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^\w.-]+", "-", title)[:64] or "notice"
    path = OUTBOX / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe_title}-{uuid.uuid4().hex[:8]}.md"
    path.write_text("mode: dryrun\ndelivered: false\n\n" + text, encoding="utf-8")
    return path


def notification_mode(mode: str | None = None, *, env: dict | None = None) -> str:
    selected = mode or os.environ.get("EVOHARNESS_NOTIFY_MODE", "auto")
    if selected not in {"auto", "disabled", "dryrun", "live"}:
        raise ValueError("invalid_notification_mode")
    if selected == "auto":
        return "live" if feishu_live(load_feishu_env() if env is None else env) else "dryrun"
    return selected


def _send_via_outbox(text: str, *, title: str, target: str, receive_type: str, env: dict) -> dict:
    # This adapter adds an intent to the original business outbox, never a second queue.
    if str(FEISHU_AGENT) not in sys.path:
        sys.path.insert(0, str(FEISHU_AGENT))
    from app.core import database
    from app.core.config import get_settings
    from app.domain import outbox
    from app.models import Outbox as OutboxRow

    settings = get_settings()
    # A root-cwd import must not silently create a second business database.
    configured = database.engine.url
    actual_path = Path(configured.database or "").resolve()
    expected_path = feishu_database_path(env)
    if configured.get_backend_name() != "sqlite" or actual_path != expected_path:
        raise NotificationConfigurationError("business_database_binding_mismatch")
    if settings.feishu_app_id != env.get("FEISHU_APP_ID") or not settings.writes_enabled:
        raise NotificationConfigurationError("business_notification_configuration_mismatch")
    digest = hashlib.sha256(json.dumps([settings.tenant_id, receive_type, target, title, text], ensure_ascii=False).encode()).hexdigest()
    with database.session_scope() as session:
        row = outbox.enqueue(session, intent=f"control-notice:{digest}", tenant=settings.tenant_id,
                             kind="notice", target_type=receive_type, target_id=target,
                             chat_id=target if receive_type == "chat_id" else "",
                             payload={"text": text, "title": title, "producer": "integration"},
                             policy_version=settings.policy_version)
        row_id = row.id
    # Restrict the SQL claim, so a report never drains somebody else's pending replies.
    outbox.dispatch_outbox(database.SessionLocal, outbox_id=row_id, limit=1)
    with database.session_scope() as session:
        row = session.get(OutboxRow, row_id)
        delivered = bool(row and row.status == "sent" and row.receipt_message_id)
        return {"ok": delivered, "mode": "live", "delivered": delivered,
                "status": row.status if row else "missing", "outboxId": row_id,
                "receipt": row.receipt_message_id if delivered else "",
                "error": row.last_error if row and not delivered else ""}


def send_text(text: str, *, title: str = "notice", chat_id: str = "", mode: str | None = None) -> dict:
    """disabled/dryrun never send; live succeeds only with the original outbox receipt."""
    if (mode or os.environ.get("EVOHARNESS_NOTIFY_MODE")) == "disabled":
        return {"ok": True, "mode": "disabled", "status": "disabled", "delivered": False}
    env = load_feishu_env()
    effective = notification_mode(mode, env=env)
    if effective == "dryrun":
        path = _dry_run(title, text)
        return {"ok": True, "mode": "dryrun", "status": "dryrun", "delivered": False, "file": str(path)}
    if not feishu_live(env):
        return {"ok": False, "mode": "live", "status": "waiting_configuration", "delivered": False,
                "error": "live_notification_configuration_missing"}
    receive_type, target = ("chat_id" if chat_id.startswith("oc_") else "open_id", chat_id) if chat_id else feishu_owner_target(env)
    if not target:
        return {"ok": False, "mode": "live", "status": "waiting_configuration", "delivered": False, "error": "no_owner_target"}
    try:
        return _send_via_outbox(text, title=title, target=target, receive_type=receive_type, env=env)
    except NotificationConfigurationError:
        return {"ok": False, "mode": "live", "status": "waiting_configuration", "delivered": False,
                "error": "canonical_business_configuration_mismatch"}
    except Exception as exc:
        # Never fall back to a second direct send after an ambiguous failure.
        return {"ok": False, "mode": "live", "status": "delivery_unknown", "delivered": False,
                "error": type(exc).__name__}


if __name__ == "__main__":
    message = sys.argv[1] if len(sys.argv) > 1 else "EvoHarness notification check"
    print(json.dumps(send_text(message, title="test"), ensure_ascii=False))
