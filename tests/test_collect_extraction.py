"""collect 拆分回归（2026-10-29）。

背景：`collect` 原 440 行 / 52 分支，每加一个信号源都要改它。按 long-function
评估结论，这是**该拆**清单里收益最大的一处，但风险也最高（闭环核心）。

本轮拆出前两个信号源（后端契约 / 前端逐按钮）。测试要保证：
① 提取后信号仍被采集（key/severity/title 形状不变）
② 缺产物时安全返回（不抛）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.loop as loop_mod


def test_backend_collector_emits_expected_shape(tmp_path, monkeypatch):
    findings: list[dict] = []
    runs: dict = {}
    report = {"findings": [
        {"endpoint": "/api/chat", "id": "missing-field", "detail": "x"},
    ]}
    out = tmp_path / "backend-contract.json"
    out.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(loop_mod, "RUNS", tmp_path)
    monkeypatch.setattr(loop_mod, "run_cmd", lambda *_a, **_k: {"ok": True})

    loop_mod._collect_backend(findings, runs)
    assert runs.get("backend") is not None
    assert len(findings) == 1
    item = findings[0]
    assert item["source"] == "backend"
    assert item["severity"] == "high"
    assert item["key"].startswith("backend:/api/chat:")
    assert "/api/chat" in item["title"]


def test_backend_collector_no_artifact_returns_silently(tmp_path, monkeypatch):
    findings: list[dict] = []
    runs: dict = {}
    monkeypatch.setattr(loop_mod, "RUNS", tmp_path)
    monkeypatch.setattr(loop_mod, "run_cmd", lambda *_a, **_k: {"ok": True})
    loop_mod._collect_backend(findings, runs)  # 不应抛
    assert findings == []


def test_frontend_collector_marks_dead_button_high(tmp_path, monkeypatch):
    """真实调用采集器：dead-button 高优先、其他中优先。"""
    findings: list[dict] = []
    runs: dict = {}
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    (ui_dir / "ui-current.json").write_text(json.dumps({"findings": [
        {"id": "button.chat", "kind": "dead-button"},
        {"id": "button.toggle", "kind": "no-effect"},
    ]}), encoding="utf-8")

    fake_script = tmp_path / "evo_ui_buttons.mjs"
    fake_script.write_text("// stub\n", encoding="utf-8")

    monkeypatch.setattr(loop_mod, "RUNS", tmp_path)
    monkeypatch.setattr(loop_mod, "run_cmd", lambda *_a, **_k: {"ok": True})
    monkeypatch.setenv("EVO_UI_SCRIPT", str(fake_script))

    loop_mod._collect_frontend(findings, runs)
    assert runs.get("ui") is not None, "有脚本时应真的跑一次采集"
    assert len(findings) == 2
    assert findings[0]["severity"] == "high", "dead-button 必须高优先"
    assert findings[1]["severity"] == "medium"
    assert findings[0]["source"] == "frontend"
    assert findings[0]["key"] == "button.chat"


def test_frontend_collector_skips_when_no_script(tmp_path, monkeypatch):
    """脚本不存在时安全返回（不抛、不产生 finding）。"""
    findings: list[dict] = []
    runs: dict = {}
    monkeypatch.setattr(loop_mod, "RUNS", tmp_path)
    monkeypatch.setenv("EVO_UI_SCRIPT", str(tmp_path / "missing.mjs"))
    loop_mod._collect_frontend(findings, runs)
    assert findings == []
    assert "ui" not in runs


def test_collect_still_calls_both_collectors(monkeypatch):
    """collect 必须仍然调用这两个采集器（提取不能把信号丢掉）。"""
    called = {"backend": 0, "frontend": 0}

    def _b(findings, runs):
        called["backend"] += 1

    def _f(findings, runs):
        called["frontend"] += 1

    monkeypatch.setattr(loop_mod, "_collect_backend", _b)
    monkeypatch.setattr(loop_mod, "_collect_frontend", _f)
    # 其余信号源全部短路，避免跑真实评测
    for name in dir(loop_mod):
        if name.startswith("_collect_") and name not in {"_collect_backend", "_collect_frontend"}:
            monkeypatch.setattr(loop_mod, name, lambda findings, runs: None)
    monkeypatch.setattr(loop_mod, "run_cmd", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(loop_mod, "save", lambda state: None)
    monkeypatch.setattr(loop_mod, "load", lambda: {"items": [], "events": []})
    try:
        loop_mod.collect("test")
    except Exception:
        pass  # 其他信号源的真实 I/O 可能失败，不影响本断言
    assert called["backend"] >= 1, "collect 必须调用后端契约采集器"
    assert called["frontend"] >= 1, "collect 必须调用前端采集器"


def test_collect_is_shorter_than_before():
    """提取后 collect 应明显变短（回归防止有人又把它堆回去）。"""
    import ast

    tree = ast.parse((REPO / "eval" / "loop.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "collect":
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            assert length < 430, f"collect 又变长了：{length}"
            return
    raise AssertionError("collect 不存在")