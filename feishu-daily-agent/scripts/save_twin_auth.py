"""把一次 OAuth 结果落库（加密），并验证可取用 token。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.database import session_scope  # noqa: E402
from app.services import identity  # noqa: E402

RESULT = Path(r"D:\claudecode\scratch\oauth_result.json")


def main() -> int:
    data = json.loads(RESULT.read_text(encoding="utf-8"))
    settings = get_settings()

    scope = {"tenant": settings.tenant_id, "app_id": settings.feishu_app_id}
    with session_scope() as session:
        ident = identity.save_authorization(
            session, tenant=scope["tenant"], app_id=scope["app_id"], result=data
        )
        print("已落库:", ident.open_id[-8:], ident.name,
              "app_id=", ident.app_id, "authorized=", ident.authorized)
        print("token 密文长度:", len(ident.access_token_enc))

    with session_scope() as session:
        state = identity.authorization_state(
            session, data["user"]["openId"], **scope
        )
        print("状态:", json.dumps(
            {k: v for k, v in state.items() if k != "openId"}, ensure_ascii=False))
        token = identity.usable_access_token(
            session, data["user"]["openId"], **scope
        )
        print("可取用 token:", bool(token), "长度:", len(token or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
