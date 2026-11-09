"""F01 飞书读写探针：只读与发信分开执行，输出脱敏矩阵。

凭据只从环境变量读取；缺少凭据时返回 blocked，不做任何网络调用。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.clock import utcnow  # noqa: E402
from app.feishu.transport import FeishuTransport  # noqa: E402


def _redact(value: str) -> str:
    return value[-6:] if value else ""


def _blocked(reason: str) -> dict:
    return {
        "ok": False,
        "blocked": reason,
        "required": [
            "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_OWNER_OPEN_ID",
            "应用 scope: im:message, im:message:send_as_bot, im:chat:readonly",
            "可读测试群/机器人私聊；写探针需显式 --target",
        ],
        "at": utcnow().isoformat(),
    }


def read_probe(transport: FeishuTransport) -> dict:
    started = time.monotonic()
    chats = transport.list_chats()
    bot_id = transport.bot_open_id()
    samples = []
    for chat in chats[:10]:
        sample = {
            "chat": _redact(chat["chat_id"]),
            "type": chat.get("chat_type"),
            "nameHash": hashlib.sha256((chat.get("name") or "").encode()).hexdigest()[:12],
        }
        try:
            page = transport.fetch_page(
                chat["chat_id"], "", int(time.time() * 1000) - 7 * 86400_000,
                int(time.time() * 1000), "",
            )
            sample.update({
                "sampleMessages": len(page.items),
                "hasMore": page.has_more,
                "messageTypes": sorted({item.msg_type for item in page.items}),
            })
        except Exception as exc:  # noqa: BLE001 — 权限缺口按来源记录，不中断矩阵
            sample["historyError"] = str(exc)[:200]
        samples.append(sample)
    return {
        "ok": True,
        "mode": "read",
        "botOpenIdSuffix": _redact(bot_id),
        "chatsSeen": len(chats),
        "samples": samples,
        "elapsedSeconds": round(time.monotonic() - started, 2),
        "identity": "bot",
        "at": utcnow().isoformat(),
    }


def write_probe(transport: FeishuTransport, target: str, message: str) -> dict:
    key = f"f01-probe-{int(time.time())}"
    result = transport.send_text(target, message, key)
    return {
        "ok": result.outcome == "sent",
        "mode": "write",
        "outcome": result.outcome,
        "receiptSuffix": _redact(result.receipt_id),
        "idempotencyKey": key,
        "error": result.error,
        "at": utcnow().isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--target", default="")
    parser.add_argument("--message", default="EvoHarness F01 探针")
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    if not args.read and not args.write:
        parser.error("必须显式选择 --read 或 --write（两者分开执行）")
    if not (os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET")
            and os.environ.get("FEISHU_OWNER_OPEN_ID")):
        payload = _blocked("missing_credentials")
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    transport = FeishuTransport()
    try:
        if args.read:
            payload = read_probe(transport)
        else:
            if not args.target:
                parser.error("--write 必须提供 --target")
            payload = write_probe(transport, args.target, args.message)
    except Exception as exc:  # noqa: BLE001
        payload = {"ok": False, "mode": "read" if args.read else "write", "error": str(exc)}
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
