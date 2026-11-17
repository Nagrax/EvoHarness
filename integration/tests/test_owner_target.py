"""integration 层：owner 投递目标解析回归。

背景（2026-11-10 实机接入）：飞书 open_id 是按应用隔离的，owner 的 open_id
在另一个应用里拿到的值对本应用无效（99992361 open_id cross app）；chat_id
与邮箱才是稳定可用的投递目标。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from integration import config as cfg  # noqa: E402
from integration import feishu_notify  # noqa: E402


def _env(**overrides: str) -> dict:
    base = {
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": "secret",
        "FEISHU_OWNER_OPEN_ID": "",
        "FEISHU_OWNER_CHAT_ID": "",
        "FEISHU_OWNER_EMAIL": "",
    }
    base.update(overrides)
    return base


def test_owner_target_prefers_chat_id():
    env = _env(FEISHU_OWNER_CHAT_ID="oc_1", FEISHU_OWNER_OPEN_ID="ou_1")
    assert cfg.feishu_owner_target(env) == ("chat_id", "oc_1")


def test_owner_target_falls_back_to_open_id_then_email():
    assert cfg.feishu_owner_target(_env(FEISHU_OWNER_OPEN_ID="ou_1")) == ("open_id", "ou_1")
    assert cfg.feishu_owner_target(_env(FEISHU_OWNER_EMAIL="a@b.com")) == ("email", "a@b.com")
    assert cfg.feishu_owner_target(_env()) == ("open_id", "")


def test_feishu_live_accepts_chat_id_without_open_id():
    assert cfg.feishu_live(_env(FEISHU_OWNER_CHAT_ID="oc_1")) is True
    assert cfg.feishu_live(_env(FEISHU_OWNER_EMAIL="a@b.com")) is True
    assert cfg.feishu_live(_env()) is False


def test_notify_dry_run_when_no_target(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_notify, "OUTBOX", tmp_path)
    monkeypatch.setattr(feishu_notify, "load_feishu_env", lambda: _env())
    result = feishu_notify.send_text("hello", title="t")
    assert result["mode"] == "dryrun"
    assert Path(result["file"]).is_file()
