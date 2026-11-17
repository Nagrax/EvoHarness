"""验证数字分身的 token 续期链路真的可用。

为什么单独验：`offline_access` 只保证"发 refresh_token 给我们"，
不代表续期一定成功（scope 变更、refresh 过期、密钥不匹配都会失败）。
这条脚本把 token 标记为已过期，强制走续期路径，再确认新 token 能发消息。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.database import session_scope  # noqa: E402
from app.services import identity  # noqa: E402


def main() -> int:
    settings = get_settings()
    open_id = settings.feishu_owner_open_id

    with session_scope() as session:
        state = identity.authorization_state(session, open_id)
        if not state.get("hasRefresh"):
            print("FAIL: 没有 refresh_token，无法验证续期")
            return 1
        ident = identity.get_identity(session, open_id)
        before = (ident.access_token_enc or "")[:24]

    print("1) 强制标记 token 已过期，逼它走续期")
    with session_scope() as session:
        ident = identity.get_identity(session, open_id)
        ident.expires_at = 0
        session.flush()

    print("2) 取 token（应触发 refresh）")
    with session_scope() as session:
        token = identity.usable_access_token(session, open_id)
        ident = identity.get_identity(session, open_id)
        after = (ident.access_token_enc or "")[:24]
        err = ident.last_error
        new_exp = ident.expires_at

    if not token:
        print(f"FAIL: 续期后仍拿不到 token，lastError={err}")
        return 1
    print(f"   OK: 拿到新 token（长度 {len(token)}）")
    print(f"   密文是否变化: {before != after}（预期 True，说明确实换了新 token）")
    print(f"   lastError: {err or '(无)'}")

    print("3) 用续期后的 token 实际发一条消息")
    import json

    import requests

    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "receive_id": settings.feishu_owner_chat_id,
            "msg_type": "text",
            "content": json.dumps(
                {"text": "[分身续期验证] 这条是用刷新后的 token 以本人身份发出的。"},
                ensure_ascii=False,
            ),
        },
        timeout=20,
    )
    data = resp.json()
    print(f"   code={data.get('code')} msg={str(data.get('msg'))[:60]}")
    if data.get("code") == 0:
        sender = (data.get("data") or {}).get("sender", {})
        print(f"   sender_type={sender.get('sender_type')} "
              f"id={sender.get('id', '')[-8:]}")
        print("续期链路: 可用 ✅")
        return 0
    print("续期链路: 发送失败 ❌")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
