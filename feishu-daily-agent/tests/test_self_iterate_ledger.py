"""自迭代引擎的台账回归：草稿查询、周期去重、自动回复配额。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import self_iterate as si


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY, message_id TEXT, is_from_bot INTEGER);
        CREATE TABLE reply_drafts (id INTEGER PRIMARY KEY, message_id TEXT, status TEXT, mode TEXT);
        CREATE TABLE reply_decisions (
            id INTEGER PRIMARY KEY, message_id TEXT, decision TEXT,
            reason_codes TEXT, experience_id INTEGER
        );
        CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    db.commit()
    db.close()
    monkeypatch.setattr(si, "DB", db_path)
    return db_path


def test_latest_draft_matches_text_message_id(temp_db):
    db = sqlite3.connect(temp_db)
    db.execute("INSERT INTO messages (id, message_id, is_from_bot) VALUES (7, 'om_x_receipt42', 0)")
    db.execute(
        "INSERT INTO reply_drafts (id, message_id, status, mode) "
        "VALUES (1, 'om_x_receipt42', 'pending', 'draft')"
    )
    db.commit()
    db.close()

    draft = si.latest_draft_for_receipt("receipt42")

    assert draft is not None
    assert draft["message_id"] == "om_x_receipt42"
    assert draft["status"] == "pending"


def test_cycles_ledger_dedupes_same_outbox_delivery(tmp_path, monkeypatch):
    ledger = tmp_path / "SELF-ITERATE"
    (ledger.parent / "F08").mkdir(parents=True)
    cycles_path = ledger.parent / "F08" / "cycles.json"
    cycles_path.write_text(
        json.dumps(
            {
                "cycles": [
                    {
                        "n": 1,
                        "delivery": "outbox#100 kind=daily sent 回执…aaaaaa",
                        "date": "2026-11-08",
                    }
                ],
                "count": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(si, "LEDGER", ledger)
    rows = [
        {"id": 101, "rc": "bbbbbb", "d": "2026-11-08"},
        {"id": 100, "rc": "aaaaaa", "d": "2026-11-08"},
    ]

    si.update_cycles_ledger(7, rows)
    first = json.loads(cycles_path.read_text(encoding="utf-8"))
    assert first["count"] == 2
    assert any("outbox#101" in item["delivery"] for item in first["cycles"])

    si.update_cycles_ledger(7, rows)
    second = json.loads(cycles_path.read_text(encoding="utf-8"))
    assert second["count"] == 2
    assert [item["delivery"] for item in second["cycles"]] == [
        item["delivery"] for item in first["cycles"]
    ]


def test_auto_quota_state_uses_logical_day_key(temp_db, monkeypatch):
    db = sqlite3.connect(temp_db)
    key = f"auto_quota:default:oc_1:{si._logical_day()}"
    db.execute("INSERT INTO system_state (key, value) VALUES (?, '2')", (key,))
    db.commit()
    db.close()
    monkeypatch.setattr(si, "P2P", "oc_1")
    monkeypatch.setattr(
        si,
        "ENVV",
        {"FEISHU_TENANT_ID": "default", "AUTO_REPLY_MAX_PER_CHAT_PER_DAY": "3"},
    )

    state = si.auto_quota_state()

    assert state["cap"] == 3
    assert state["used"] == 2
    assert state["remaining"] == 1
    assert state["day"] == si._logical_day()


def test_adjudication_requires_correction_unless_candidate():
    assert si.adjudication_needs_correction(None) is True
    assert si.adjudication_needs_correction({"status": "paused"}) is True
    assert si.adjudication_needs_correction({"status": "approved_auto"}) is True
    assert si.adjudication_needs_correction({"status": "candidate"}) is False


def test_variant_approval_is_deterministic_and_skips_shadow():
    assert si.should_approve_variant(
        {"shadow": 0, "mode": "draft"}, approved_clusters=set(), cluster="a", budget=6
    )
    assert not si.should_approve_variant(
        {"shadow": 1, "mode": "shadow"}, approved_clusters=set(), cluster="a", budget=6
    )
    assert not si.should_approve_variant(
        {"shadow": 0, "mode": "draft"}, approved_clusters={"a"}, cluster="a", budget=6
    )


def test_auto_window_result_attributes_by_decision():
    exact = si.auto_window_result(
        exact_decisions=["auto_eligible", "auto_eligible", "auto_eligible"],
        variant_decision="needs_review",
    )
    assert exact["autoSent"] == 3
    assert exact["variantAutoSent"] == 0

    delayed = si.auto_window_result(
        exact_decisions=[None, "auto_eligible"],
        variant_decision=None,
    )
    assert delayed["autoSent"] == 1
    assert delayed["variantAutoSent"] == 0

    severe = si.auto_window_result(
        exact_decisions=["auto_eligible"],
        variant_decision="auto_eligible",
    )
    assert severe["variantAutoSent"] == 1


def test_decision_for_receipt_uses_latest(temp_db):
    db = sqlite3.connect(temp_db)
    db.execute("INSERT INTO messages (id, message_id, is_from_bot) VALUES (1, 'om_x_abc', 0)")
    db.execute(
        "INSERT INTO reply_decisions (id, message_id, decision) VALUES (1, 'om_x_abc', 'draft_only')"
    )
    db.execute(
        "INSERT INTO reply_decisions (id, message_id, decision) VALUES (2, 'om_x_abc', 'auto_eligible')"
    )
    db.commit()
    db.close()

    row = si.decision_for_receipt("abc")

    assert row is not None
    assert row["decision"] == "auto_eligible"
