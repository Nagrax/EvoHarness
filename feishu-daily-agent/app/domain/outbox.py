"""Legacy channel adapter over the single product outbox implementation."""
import logging
from evoharness import outbox as _core
from evoharness.outbox import *
from evoharness.governance import decisions as _governance
from app.core.config import get_settings
from app.feishu.transport import get_transport
logger = logging.getLogger(__name__)

def _precheck(session, row):
    ok, reason = _core._precheck(session, row, policy=get_settings())
    if not ok:
        return False, reason
    # DE-41 发送前治理重验：撤权/暂停及时传到发送层（决定被撤回/被替代或
    # 所在组织已暂停即拦截）。无治理绑定的意图沿用既有口径；delivery_unknown
    # 等真实回执状态不被本检查改写。
    return _governance.check_send_allowed(session, row)

def recover_expired_leases(session, *, now=None):
    return _core.recover_expired_leases(session, now=now, policy=get_settings())

def _resolve_user_token(session_factory, payload: dict) -> str:
    """解析这条回复该用谁的 user_access_token。

    优先用 payload 里显式带的 `twin_open_id`（草稿归属人）；解析失败或异常
    返回空串（= token 不可用）。是否降级机器人身份**不在这里决定**：核心
    发送链在发送前做身份冻结（R09），token 不可用且批准未显式 allow_bot_
    fallback 时意图转 waiting_authorization，只有获批才允许 bot fallback。
    """
    open_id = (payload.get("twin_open_id") or "").strip()
    if not open_id:
        return ""
    session = session_factory()
    try:
        from app.services import identity as identity_module

        return identity_module.usable_access_token(session, open_id) or ""
    except Exception as exc:  # noqa: BLE001 — 失败静默返回空串，可查日志
        logger.warning("分身身份解析失败（token 不可用，交身份冻结决策）：%s", exc)
        return ""
    finally:
        session.close()



def dispatch_outbox(session_factory, *, transport=None, limit=20, now=None, outbox_id=None):
    return _core.dispatch_outbox(session_factory, transport=transport or get_transport(),
        limit=limit, now=now, outbox_id=outbox_id, policy=get_settings(),
        precheck=_precheck, user_token_resolver=_resolve_user_token)
