"""首次接入引导：第一条 p2p 消息认主，且只认一次。

回归背景：应用刚发布时没有 im:chat:readonly，服务端无法列举会话；
如果没有引导，owner/chat_id 只能靠人工查控制台，接入会卡住。
"""
from __future__ import annotations

from pathlib import Path

from app.core.database import session_scope
from app.domain import admission
from app.services import owner_bootstrap
from tests.helpers import incoming


def _seed_env(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "# keep me\nFEISHU_APP_ID=cli_x\nFEISHU_OWNER_OPEN_ID=\n"
        "FEISHU_CHAT_WHITELIST=\nFEISHU_REPLY_ALLOWED_CHATS=\n",
        encoding="utf-8",
    )
    return path


def _enable(settings, monkeypatch, *, owner: str = "") -> None:
    monkeypatch.setattr(settings, "feishu_owner_bootstrap", True)
    monkeypatch.setattr(settings, "feishu_owner_open_id", owner)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", "")
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "")
    monkeypatch.setattr(settings, "feishu_reply_allowed_chats", "")


def test_first_p2p_message_claims_owner_and_persists_env(
    settings, monkeypatch, tmp_path
):
    _enable(settings, monkeypatch)
    env_path = _seed_env(tmp_path)
    msg = incoming("你好", "om_boot_1", chat_id="oc_p2p_1", sender="ou_owner1")

    result = owner_bootstrap.maybe_bootstrap_owner(msg, env_path=env_path)

    assert result["bootstrapped"] is True
    assert result["owner"] == "ou_owner1"
    assert settings.feishu_owner_open_id == "ou_owner1"
    assert settings.feishu_chat_whitelist == "oc_p2p_1"
    text = env_path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "FEISHU_OWNER_OPEN_ID=ou_owner1" in text
    assert "FEISHU_CHAT_WHITELIST=oc_p2p_1" in text
    assert "FEISHU_REPLY_ALLOWED_CHATS=oc_p2p_1" in text

    with session_scope() as session:
        accepted = admission.admit(session, msg)
    assert accepted.accepted
    assert accepted.should_process


def test_bootstrap_is_one_shot_even_after_env_reset(settings, monkeypatch, tmp_path):
    _enable(settings, monkeypatch)
    env_path = _seed_env(tmp_path)
    first = incoming("你好", "om_boot_1", chat_id="oc_p2p_1", sender="ou_owner1")
    assert owner_bootstrap.maybe_bootstrap_owner(first, env_path=env_path)["bootstrapped"]

    # 模拟 .env 被清空/重启：DB 里的认主标记仍应阻止第二个 owner。
    monkeypatch.setattr(settings, "feishu_owner_open_id", "")
    second = incoming("我也是 owner", "om_boot_2", chat_id="oc_p2p_2", sender="ou_stranger")
    result = owner_bootstrap.maybe_bootstrap_owner(second, env_path=env_path)

    assert result["bootstrapped"] is False
    assert result["reason"] == "owner_already_claimed"
    assert settings.feishu_owner_open_id == ""


def test_bootstrap_disabled_by_default(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "feishu_owner_bootstrap", False)
    monkeypatch.setattr(settings, "feishu_owner_open_id", "")
    env_path = _seed_env(tmp_path)
    result = owner_bootstrap.maybe_bootstrap_owner(
        incoming("你好", "om_boot_1", chat_id="oc_p2p_1", sender="ou_owner1"),
        env_path=env_path,
    )
    assert result == {"bootstrapped": False, "reason": "disabled"}
    assert "FEISHU_OWNER_OPEN_ID=ou_owner1" not in env_path.read_text(encoding="utf-8")


def test_group_message_never_claims_owner(settings, monkeypatch, tmp_path):
    _enable(settings, monkeypatch)
    result = owner_bootstrap.maybe_bootstrap_owner(
        incoming("群消息", "om_g", chat_id="oc_g", chat_type="group", sender="ou_owner1"),
        env_path=_seed_env(tmp_path),
    )
    assert result["bootstrapped"] is False
    assert result["reason"] == "not_p2p"
    assert settings.feishu_owner_open_id == ""


def test_bot_echo_never_claims_owner(settings, monkeypatch, tmp_path):
    _enable(settings, monkeypatch)
    result = owner_bootstrap.maybe_bootstrap_owner(
        incoming("回声", "om_b", chat_id="oc_p2p_1", sender="ou_bot", sender_type="app"),
        env_path=_seed_env(tmp_path),
    )
    assert result["bootstrapped"] is False
    assert result["reason"] == "bot_echo"


def test_existing_owner_is_not_overwritten(settings, monkeypatch, tmp_path):
    _enable(settings, monkeypatch, owner="ou_existing")
    result = owner_bootstrap.maybe_bootstrap_owner(
        incoming("你好", "om_boot_1", chat_id="oc_p2p_1", sender="ou_stranger"),
        env_path=_seed_env(tmp_path),
    )
    assert result["bootstrapped"] is False
    assert result["reason"] == "owner_already_set"
    assert settings.feishu_owner_open_id == "ou_existing"


def test_configured_owner_chat_rejects_other_chats(settings, monkeypatch, tmp_path):
    """显式配置了 owner 会话时，其他会话不能抢认主（防止共享应用被顶号）。"""
    _enable(settings, monkeypatch)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", "oc_expected")
    result = owner_bootstrap.maybe_bootstrap_owner(
        incoming("你好", "om_boot_1", chat_id="oc_other", sender="ou_stranger"),
        env_path=_seed_env(tmp_path),
    )
    assert result["bootstrapped"] is False
    assert result["reason"] == "chat_not_owner_configured"
    assert settings.feishu_owner_open_id == ""


def test_configured_owner_chat_accepts_matching_chat(settings, monkeypatch, tmp_path):
    _enable(settings, monkeypatch)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", "oc_expected")
    result = owner_bootstrap.maybe_bootstrap_owner(
        incoming("你好", "om_boot_2", chat_id="oc_expected", sender="ou_owner_real"),
        env_path=_seed_env(tmp_path),
    )
    assert result["bootstrapped"] is True
    assert settings.feishu_owner_open_id == "ou_owner_real"
