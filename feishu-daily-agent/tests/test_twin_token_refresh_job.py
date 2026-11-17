"""分身 token 主动续期任务回归。

背景：`refresh_token` 有效期 7 天且每次续期轮换。若只依赖"发消息时按需刷新"，
某个使用者连续 7 天不用就会失效，只能重新授权。定时任务负责兜住这个场景。
"""
from __future__ import annotations

import time

from app.core.database import session_scope
from app.models import TwinIdentity
from app.services import identity


def _seed_identity(settings, open_id: str, *, expires_at: float, authorized: bool = True):
    with session_scope() as session:
        row = TwinIdentity(
            tenant=settings.tenant_id,
            open_id=open_id,
            name="测试",
            authorized=authorized,
            scope="im:message",
            access_token_enc="",
            refresh_token_enc="",
            expires_at=expires_at,
        )
        session.add(row)
        session.flush()


def test_refresh_job_skips_when_nothing_authorized(settings, monkeypatch):
    """没有在册分身时不能报错。"""
    from app.pipeline.scheduler import _refresh_twin_tokens

    _refresh_twin_tokens()  # 不抛异常即通过


def test_refresh_job_runs_over_authorized_only(settings, monkeypatch):
    """只处理 authorized=True 的分身。"""
    from app.pipeline import scheduler as scheduler_module

    _seed_identity(settings, "ou_yes", expires_at=time.time() + 3600)
    _seed_identity(settings, "ou_no", expires_at=0, authorized=False)

    seen: list[str] = []
    real = identity.usable_access_token

    def _spy(session, open_id):
        seen.append(open_id)
        return real(session, open_id)

    monkeypatch.setattr(identity, "usable_access_token", _spy)
    scheduler_module._refresh_twin_tokens()

    assert "ou_yes" in seen
    assert "ou_no" not in seen, "未授权的分身不该被续期（也不该被当成可用）"


def test_refresh_job_does_not_raise_on_failure(settings, monkeypatch):
    """续期失败只记 last_error，不能把定时任务炸掉。"""
    from app.pipeline import scheduler as scheduler_module

    _seed_identity(settings, "ou_broken", expires_at=0)

    def _boom(session, open_id):
        raise RuntimeError("模拟续期异常")

    monkeypatch.setattr(identity, "usable_access_token", _boom)
    scheduler_module._refresh_twin_tokens()  # 内部捕获，不应抛到调用方
