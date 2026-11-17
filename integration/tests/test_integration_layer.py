"""integration 层回归测试（不依赖运行中服务与真实飞书）。

覆盖：L9040 主题前缀解析、标题清理、L9039 催办组装、审计记录格式。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "integration"))

import nudge  # noqa: E402
import task_intake  # noqa: E402


def test_parse_theme_prefix_before_task():
    text, theme = task_intake._parse_theme("主题：页脚改版 任务：取服务端时间")
    assert theme == "页脚改版"
    assert text == "任务：取服务端时间"


def test_parse_theme_prefix_after_task():
    text, theme = task_intake._parse_theme("任务：修bug 主题：支付链路")
    assert theme == "支付链路"
    # 任务前缀的二次剥离由 run() 负责（见 test_task_prefix_strip_double）
    assert text == "任务：修bug"


def test_parse_theme_no_theme():
    text, theme = task_intake._parse_theme("任务：单纯的活")
    assert theme is None
    assert text == "任务：单纯的活"


def test_parse_theme_alone():
    text, theme = task_intake._parse_theme("主题：只有主题")
    assert theme == "只有主题"
    assert text == ""


def test_task_prefix_strip_double():
    """主题在前时，任务前缀要被二次剥离（L9044 实测教训）。"""
    text, theme = task_intake._parse_theme("主题：T 任务：干活")
    for prefix in task_intake.TASK_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    assert text == "干活"
    assert theme == "T"


def test_nudge_compose_with_stuck_items():
    text = nudge.compose({
        "highOpen": [{"id": "L9001", "title": "高优"}],
        "stale": [{"id": "L9002", "ageDays": 3, "title": "挂起"}],
        "draftsStale": [],
    })
    assert text is not None
    assert "L9001" in text and "L9002" in text
    assert text.startswith("🔔")


def test_nudge_compose_empty_returns_none():
    assert nudge.compose({}) is None
    assert nudge.compose({"highOpen": [], "stale": [], "draftsStale": []}) is None


def test_audit_record_appends_jsonl(tmp_path, monkeypatch):
    from integration import audit
    monkeypatch.setattr(audit, "AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit, "DATA", tmp_path)
    audit.record("task-intake", actor="tester", source_message_id="om_x",
                 item_id="L9999", result="ok")
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["action"] == "task-intake"
    assert entry["actor"] == "tester"
    assert entry["itemId"] == "L9999"
    assert "at" in entry
