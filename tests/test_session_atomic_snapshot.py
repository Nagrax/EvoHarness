"""W06 回归：会话快照原子写、上一有效版本与损坏/不兼容区分。

历史缺陷（已修）：
- `save_session` 直接 write_text 覆盖：写盘中断留下半截 JSON，下次启动"会话消失"；
- `load_session` 把损坏与缺失都吞成 None，用户看不到任何诊断；
- 折叠记忆 latest 文件同样是覆盖写，无原子性。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents.session as S


@pytest.fixture()
def session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SESSION_DIR", tmp_path)
    return tmp_path


# ─── ① 原子写与上一有效版本 ────────────────────────────────


def test_save_keeps_previous_valid_version(session_dir):
    S.save_session("s1", {"note": "v1"})
    assert json.loads((session_dir / "s1.json").read_text(encoding="utf-8"))["note"] == "v1"

    S.save_session("s1", {"note": "v2"})
    live = json.loads((session_dir / "s1.json").read_text(encoding="utf-8"))
    prev = json.loads((session_dir / "s1.json.prev").read_text(encoding="utf-8"))
    assert live["note"] == "v2"
    assert prev["note"] == "v1"  # 上一份有效版本被保留


def test_save_leaves_no_temp_file(session_dir):
    S.save_session("s1", {"note": "x"})
    leftovers = [p.name for p in session_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_interrupted_replace_keeps_previous_snapshot(session_dir, monkeypatch):
    """replace 失败（模拟进程中断/磁盘满）不得破坏上一份有效快照。"""
    S.save_session("s1", {"note": "good"})
    original_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("simulated disk full")

    monkeypatch.setattr(S.os, "replace", failing_replace)
    with pytest.raises(OSError):
        S.save_session("s1", {"note": "lost"})

    monkeypatch.setattr(S.os, "replace", original_replace)
    data = json.loads((session_dir / "s1.json").read_text(encoding="utf-8"))
    assert data["note"] == "good", "中断后必须仍读到上一份有效快照"
    assert not [p for p in session_dir.iterdir() if p.name.endswith(".tmp")]


# ─── ② 损坏 / 缺失 / 不兼容必须区分 ──────────────────────


def test_load_distinguishes_missing_corrupt_incompatible(session_dir):
    assert S.load_session_detailed("absent") == (None, "missing")

    (session_dir / "broken.json").write_text("{not json", encoding="utf-8")
    assert S.load_session_detailed("broken") == (None, "corrupt")

    (session_dir / "future.json").write_text(
        json.dumps({"schemaVersion": S.SCHEMA_VERSION + 1}), encoding="utf-8")
    assert S.load_session_detailed("future") == (None, "incompatible")

    (session_dir / "ok.json").write_text(
        json.dumps({"schemaVersion": S.SCHEMA_VERSION, "note": "中文内容"}, ensure_ascii=False),
        encoding="utf-8")
    data, reason = S.load_session_detailed("ok")
    assert reason == "ok" and data["note"] == "中文内容"


def test_unreadable_bytes_reported_not_silently_none(session_dir):
    path = session_dir / "bad.json"
    path.write_bytes(b"\xff\xfe\x00\x00")
    data, reason = S.load_session_detailed("bad")
    assert data is None
    assert reason in {"unreadable", "corrupt"}


# ─── ③ 损坏后可从上一次备份恢复 ──────────────────────────


def test_corrupt_session_recovers_from_backup(session_dir):
    S.save_session("s1", {"note": "v1"})
    S.save_session("s1", {"note": "v2"})
    (session_dir / "s1.json").write_text("{truncated", encoding="utf-8")

    data, reason = S.load_session_fallback("s1")
    assert reason == "recovered-from-backup"
    assert data["note"] == "v1"


def test_missing_session_does_not_fake_recovery(session_dir):
    assert S.load_session_fallback("nope") == (None, "missing")


def test_incompatible_session_does_not_fall_back(session_dir):
    S.save_session("s1", {"note": "v1"})
    S.save_session("s1", {"note": "v2"})
    (session_dir / "s1.json").write_text(json.dumps({"schemaVersion": 999}), encoding="utf-8")
    data, reason = S.load_session_fallback("s1")
    assert data is None and reason == "incompatible"


# ─── ④ 既有契约保持：load_session 仍返回 dict|None、UTF-8 显式 ────


def test_load_session_contract_preserved(session_dir):
    (session_dir / "sess-1.json").write_text(
        json.dumps({"note": "会话中文内容"}, ensure_ascii=False), encoding="utf-8")
    assert S.load_session("sess-1") == {"note": "会话中文内容"}
    assert S.load_session("missing") is None
    assert S.load_session("../escape") is None


def test_save_session_rejects_unsafe_id(session_dir):
    S.save_session("../escape", {"note": "x"})
    assert not list(session_dir.glob("*.json"))


def test_folded_memory_latest_is_atomic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    S.save_folded_session_memory("sess1", {"note": "一"})
    S.save_folded_session_memory("sess1", {"note": "二"})
    d = tmp_path / ".evoharness" / "sessions"
    latest = json.loads((d / "sess1.folded-memory.latest.json").read_text(encoding="utf-8"))
    assert latest["note"] == "二"
    lines = (d / "sess1.folded-memory.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]


# ─── ⑤ 坏会话文件不得静默从列表消失 ──────────────────


def test_list_sessions_reports_corrupt_files(session_dir):
    S.save_session("good", {"metadata": {"id": "good", "title": "正常"}})
    (session_dir / "broken.json").write_text("{not json", encoding="utf-8")
    (session_dir / "nometa.json").write_text(json.dumps({"foo": 1}), encoding="utf-8")

    rows = S.list_sessions()
    assert any(r.get("id") == "good" for r in rows)
    reasons = {e["file"]: e["reason"] for e in S.last_list_errors}
    assert "broken.json" in reasons
    assert "nometa.json" in reasons
    assert reasons["nometa.json"] == "no-metadata"


def test_list_sessions_clean_case_has_no_errors(session_dir):
    S.save_session("only", {"metadata": {"id": "only", "title": "唯一"}})
    S.list_sessions()
    assert S.last_list_errors == []
