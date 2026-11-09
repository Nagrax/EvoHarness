"""F01 回归：ListChat 无 chat_type 属性时 chat_type_of 的推导行为。

真实 SDK 的 ListChat 对象没有 chat_type 属性（2026-11-07 实机发现），
transport.list_chats 曾因此抛 AttributeError 导致来源发现失败。
"""
from types import SimpleNamespace

from app.feishu.transport import chat_type_of


def test_sdk_listchat_without_chat_type_group():
    # 模拟真实 SDK ListChat：无 chat_type，群聊无 p2p_target_id
    item = SimpleNamespace(
        chat_id="oc_group1", name="g", p2p_target_id=None, chat_mode="group"
    )
    assert chat_type_of(item) == "group"


def test_sdk_listchat_without_chat_type_p2p():
    item = SimpleNamespace(
        chat_id="oc_pair", name="", p2p_target_id="ou_user1", chat_mode="group"
    )
    assert chat_type_of(item) == "p2p"


def test_dict_with_declared_chat_type_is_respected():
    assert chat_type_of({"chat_type": "group", "p2p_target_id": None}) == "group"
    assert chat_type_of({"chat_type": "p2p", "p2p_target_id": "ou_x"}) == "p2p"


def test_sdk_object_with_declared_chat_type_is_respected():
    item = SimpleNamespace(chat_id="oc_x", chat_type="p2p")
    assert chat_type_of(item) == "p2p"


def test_receive_id_type_by_prefix():
    from app.feishu.transport import receive_id_type_of

    assert receive_id_type_of("oc_group1") == "chat_id"
    assert receive_id_type_of("om_msg1") == "message_id"
    assert receive_id_type_of("ou_user1") == "open_id"
    assert receive_id_type_of("on_x") == "open_id"
    assert receive_id_type_of("") == "open_id"


def test_send_text_uses_chat_id_type_for_group(monkeypatch):
    from app.feishu import transport as transport_module

    calls = []

    def fake_create(self, receive_id_type, receive_id, msg_type, content, idem=""):
        calls.append(receive_id_type)
        return transport_module.TransportResult("sent", receipt_id="om_x")

    monkeypatch.setattr(transport_module.FeishuTransport, "_create", fake_create)
    t = transport_module.FeishuTransport()
    t.send_text("oc_group1", "hello", "k1")
    t.send_card("ou_owner", {"v": 1}, "k2")
    assert calls == ["chat_id", "open_id"]


def test_extract_mentions_supports_history_string_ids():
    from types import SimpleNamespace

    from app.feishu.messages import _extract_mentions

    # 历史 API：m.id 是字符串 open_id
    history_style = [SimpleNamespace(key="@_user_1", id="ou_bot123", name="EvoHarness")]
    ids, names = _extract_mentions(history_style)
    assert ids == ["ou_bot123"]
    assert names == ["EvoHarness"]

    # 事件 API：m.id 是带 open_id 的对象
    event_style = [SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id="ou_bot123"), name="EvoHarness")]
    ids2, names2 = _extract_mentions(event_style)
    assert ids2 == ["ou_bot123"]
    assert names2 == ["EvoHarness"]


def test_history_item_mentions_bot():
    from types import SimpleNamespace

    from app.feishu.messages import from_history_item
    from app.core import config

    settings = config.get_settings()
    bot_id = settings.feishu_bot_open_id
    item = SimpleNamespace(
        message_id="om_h1", chat_id="oc_g1", body=SimpleNamespace(
            content='{"text":"@_user_1 你好"}'),
        msg_type="text", sender=SimpleNamespace(id="ou_u1", sender_type="user", name="N"),
        create_time=1794027000000, thread_id="",
        mentions=[SimpleNamespace(key="@_user_1", id=bot_id, name="EvoHarness")],
    )
    msg = from_history_item(item)
    assert msg.mentions_bot is True
    assert msg.mention_ids == [bot_id]



def test_sim_logical_date_hook(monkeypatch):
    """SIM 钩子：设置 FEISHU_SIM_LOGICAL_DATE 时 current_logical_date 返回该日期；未设置走真实时钟。"""
    import datetime as dt

    from app.core import clock

    monkeypatch.setenv("FEISHU_SIM_LOGICAL_DATE", "2026-11-08")
    assert clock.current_logical_date("Asia/Shanghai") == dt.date(2026, 11, 8)

    monkeypatch.delenv("FEISHU_SIM_LOGICAL_DATE", raising=False)
    real = dt.datetime.now(dt.ZoneInfo("Asia/Shanghai")).date() if hasattr(dt, "ZoneInfo") else None
    from zoneinfo import ZoneInfo

    real = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
    assert clock.current_logical_date("Asia/Shanghai") == real


def test_sim_logical_date_invalid_raises(monkeypatch):
    from app.core import clock

    monkeypatch.setenv("FEISHU_SIM_LOGICAL_DATE", "not-a-date")
    import pytest

    with pytest.raises(ValueError):
        clock.current_logical_date("Asia/Shanghai")
