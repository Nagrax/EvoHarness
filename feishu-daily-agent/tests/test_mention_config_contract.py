"""User-confirmed group defaults (2026-11-13): mention-free with an explicit opt-in gate."""
from app.core.config import Settings
from app.core.database import session_scope
from app.domain import admission
from tests.helpers import incoming


def test_default_is_mention_free_without_envfile(monkeypatch):
    monkeypatch.delenv("FEISHU_GROUP_REPLY_ONLY_MENTION", raising=False)
    assert Settings(_env_file=None).feishu_group_reply_only_mention is False


def test_explicit_mention_only_overrides_default(monkeypatch):
    monkeypatch.setenv("FEISHU_GROUP_REPLY_ONLY_MENTION", "1")
    assert Settings(_env_file=None).feishu_group_reply_only_mention is True


def _admit(settings, monkeypatch, *, only_mention, mentions):
    monkeypatch.setattr(settings, "feishu_group_reply_only_mention", only_mention)
    monkeypatch.setattr(settings, "feishu_chat_whitelist", "oc_contract")
    with session_scope() as session:
        admission.ensure_source(session, tenant="default", owner="ou_owner",
                                chat_id="oc_contract", source_type="group", reply_allowed=True)
        return admission.admit(session, incoming(
            "group question", "om_contract", chat_id="oc_contract", chat_type="group",
            sender="ou_member", mentions=mentions, create_ms=1000))


def test_default_group_without_mention_is_processed(settings, monkeypatch):
    result = _admit(settings, monkeypatch, only_mention=settings.feishu_group_reply_only_mention,
                    mentions=["ou_someone"])
    assert result.accepted
    assert result.reason == "group_no_mention_mode"
    assert result.should_process is True


def test_explicit_mention_only_without_mention_is_ingest_only(settings, monkeypatch):
    result = _admit(settings, monkeypatch, only_mention=True, mentions=["ou_someone"])
    assert result.accepted
    assert result.reason == "group_no_mention_ingest_only"
    assert result.should_process is False


def test_explicit_mention_only_with_bot_mention_is_processed(settings, monkeypatch):
    result = _admit(settings, monkeypatch, only_mention=True, mentions=["ou_bot"])
    assert result.accepted
    assert result.reason == "group_mention_ok"
    assert result.should_process is True
