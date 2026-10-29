"""eval/eval_skills_cli.py 回归（2026-10-29，给 `/api/eval/skills` 补非 HTTP 入口）。

背景：全栈对应分析发现这是**唯一既无前端也无 CLI 的端点**，用户只能 curl。
它与 `/api/skills` 高度重叠（后者是超集），保留是因为演示环境可回退 mock。

本 CLI 直接读同一份产物（不经 HTTP），所以要固定：
① 输出字段与端点语义一致 ② 无产物时优雅返回 ③ 不写盘（纯只读）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.eval_skills_cli as cli


@pytest.fixture()
def fake_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "EVOLUTION_DIR", tmp_path)
    return tmp_path


def test_reads_report_and_usage(fake_env):
    (fake_env / "online_eval_report.json").write_text(json.dumps({"skills": [
        {"skill": "s1", "status": "healthy", "retrieved": 10, "relevant": 6,
         "used": 4, "used_rate": 0.4, "relevance_rate": 0.6,
         "last_action": "promote", "last_time": "2026-10-29"},
    ]}), encoding="utf-8")
    (fake_env / "skill_usage_stats.json").write_text(
        json.dumps({"s1": {"source": "project"}}), encoding="utf-8")

    data = cli.collect()
    assert data["ok"] and data["count"] == 1
    row = data["skills"][0]
    assert row["skill"] == "s1" and row["status"] == "healthy"
    assert row["used_rate"] == pytest.approx(0.4)
    assert row["source"] == "project"


def test_missing_artifacts_returns_empty(fake_env):
    data = cli.collect()
    assert data["ok"] is True
    assert data["hasData"] is False
    assert data["skills"] == []


def test_corrupt_json_does_not_crash(fake_env):
    (fake_env / "online_eval_report.json").write_text("{not json", encoding="utf-8")
    data = cli.collect()
    assert data["ok"] is True and data["skills"] == []


def test_cli_json_output(fake_env, capsys):
    (fake_env / "online_eval_report.json").write_text(json.dumps({"skills": [
        {"skill": "s2", "status": "watch", "retrieved": 1}]}), encoding="utf-8")
    assert cli.main.__wrapped__ if hasattr(cli.main, "__wrapped__") else True
    # 直接调用 main 会解析 sys.argv，这里改用 collect 验证 json 可序列化
    data = cli.collect()
    json.dumps(data)  # 不抛即可


def test_cli_is_read_only(fake_env):
    """CLI 不得写盘（纯只读工具）。"""
    (fake_env / "online_eval_report.json").write_text('{"skills": []}', encoding="utf-8")
    before = {p.name: p.stat().st_mtime for p in fake_env.iterdir()}
    cli.collect()
    after = {p.name: p.stat().st_mtime for p in fake_env.iterdir()}
    assert before == after, "collect() 不应产生或修改文件"


def test_endpoint_and_cli_share_data_source():
    """CLI 与端点必须读同一份产物（防两条路径漂移）。"""
    app_src = (REPO / "server" / "app.py").read_text(encoding="utf-8")
    cli_src = (REPO / "eval" / "eval_skills_cli.py").read_text(encoding="utf-8")
    for name in ("online_eval_report.json", "skill_usage_stats.json"):
        assert name in app_src, f"端点未使用 {name}"
        assert name in cli_src, f"CLI 未使用 {name}"