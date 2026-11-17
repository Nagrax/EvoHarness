"""回归：owner bootstrap 不得在测试模式写真实 .env。

背景（2026-11-10 实机接入踩到）：测试驱动真实事件处理器时，owner bootstrap
会把夹具里的假 owner/chat（ou_owner1、oc_owner_p2p）写进部署用 .env，
导致真实服务的白名单与 owner 被静默替换。
"""
from __future__ import annotations

from app.core.database import session_scope
from app.services import owner_bootstrap
from tests.helpers import incoming


def test_test_mode_skips_env_write(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "feishu_owner_bootstrap", True)
    monkeypatch.setattr(settings, "feishu_owner_open_id", "")
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "test_mode", True)
    # 把"部署用 .env"指向临时文件，模拟测试环境误写真实配置的场景。
    deploy_env = tmp_path / ".env"
    deploy_env.write_text("FEISHU_APP_ID=cli_x\nFEISHU_OWNER_OPEN_ID=\n", encoding="utf-8")
    monkeypatch.setattr(owner_bootstrap, "DEFAULT_ENV_PATH", deploy_env)

    result = owner_bootstrap.maybe_bootstrap_owner(
        incoming("你好", "om_boot_x", chat_id="oc_p2p_x", sender="ou_owner_x"),
    )

    assert result["bootstrapped"] is True
    # 内存里认主仍生效（本次进程可用），但磁盘配置保持原样。
    assert settings.feishu_owner_open_id == "ou_owner_x"
    assert "ou_owner_x" not in deploy_env.read_text(encoding="utf-8")


def test_non_test_mode_writes_env(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "feishu_owner_bootstrap", True)
    monkeypatch.setattr(settings, "feishu_owner_open_id", "")
    monkeypatch.setattr(settings, "test_mode", False)
    env_path = tmp_path / ".env"
    env_path.write_text("FEISHU_APP_ID=cli_x\n", encoding="utf-8")

    owner_bootstrap.maybe_bootstrap_owner(
        incoming("你好", "om_boot_y", chat_id="oc_p2p_y", sender="ou_owner_y"),
        env_path=env_path,
    )
    text = env_path.read_text(encoding="utf-8")
    assert "FEISHU_OWNER_OPEN_ID=ou_owner_y" in text
    assert "FEISHU_OWNER_CHAT_ID=oc_p2p_y" in text


def test_bootstrap_registers_source_before_admission(settings, monkeypatch, tmp_path):
    """认主后同一条消息必须能通过准入（否则首条消息被 source_missing 丢掉）。"""
    monkeypatch.setattr(settings, "feishu_owner_bootstrap", True)
    monkeypatch.setattr(settings, "feishu_owner_open_id", "")
    monkeypatch.setattr(settings, "feishu_owner_chat_id", "")
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "")
    monkeypatch.setattr(settings, "test_mode", True)

    from app.handlers.events import persist_event

    accepted, reason, should_process = persist_event(
        incoming("请问年假怎么申请", "om_boot_first", chat_id="oc_p2p_z", sender="ou_owner_z")
    )
    assert accepted and should_process
    assert reason == "p2p_ok"
    with session_scope() as session:
        from app.models import Source

        source = session.query(Source).filter(Source.chat_id == "oc_p2p_z").one()
        assert source.reply_allowed is True
