"""_collect_module_scan 回归（2026-10-29 提取 ③ 模块扫描段）。

提取时必须一字不改地保住三个语义：
① high → 每处一条，key = module:<rule>:<file>
② medium → 按 rule 聚合成一条，key = module-medium:<rule>，保留 files 与前 40 处 locations
③ silent-except 类在 medium 分支跳过（避免与 high 重复计数）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.loop as loop_mod


def _findings_for(tmp_path, monkeypatch, findings: list[dict]) -> list[dict]:
    out = tmp_path / "module-scan.json"
    out.write_text(json.dumps({"findings": findings}), encoding="utf-8")
    monkeypatch.setattr(loop_mod, "RUNS", tmp_path)
    monkeypatch.setattr(loop_mod, "run_cmd", lambda *_a, **_k: {"ok": True})
    collected: list[dict] = []
    runs: dict = {}
    loop_mod._collect_module_scan(collected, runs)
    assert runs.get("moduleScan") is not None
    return collected


def test_high_becomes_one_item_per_location(tmp_path, monkeypatch):
    items = _findings_for(tmp_path, monkeypatch, [
        {"rule": "silent-except", "file": "a.py", "line": 10, "severity": "high"},
        {"rule": "silent-except", "file": "a.py", "line": 20, "severity": "high"},
    ])
    assert len(items) == 2, "high 每处一条（同文件不同位置也要分开）"
    assert items[0]["key"] == "module:silent-except:a.py"
    assert items[0]["severity"] == "high"
    assert items[0]["source"] == "module"


def test_medium_aggregates_by_rule(tmp_path, monkeypatch):
    items = _findings_for(tmp_path, monkeypatch, [
        {"rule": "long-function", "file": "a.py", "line": 1, "severity": "medium"},
        {"rule": "long-function", "file": "b.py", "line": 2, "severity": "medium"},
        {"rule": "magic-threshold", "file": "c.py", "line": 3, "severity": "medium"},
    ])
    keys = sorted(i["key"] for i in items)
    assert keys == ["module-medium:long-function", "module-medium:magic-threshold"]
    long_item = next(i for i in items if i["key"] == "module-medium:long-function")
    assert long_item["detail"]["count"] == 2
    assert long_item["detail"]["files"] == ["a.py", "b.py"]
    assert len(long_item["detail"]["locations"]) == 2
    assert "（2 处 / 2 文件）" in long_item["title"]


def test_medium_silent_except_not_double_counted(tmp_path, monkeypatch):
    items = _findings_for(tmp_path, monkeypatch, [
        {"rule": "silent-except", "file": "a.py", "line": 1, "severity": "medium"},
        {"rule": "silent-except-return-none", "file": "b.py", "line": 2,
         "severity": "medium"},
        {"rule": "long-function", "file": "c.py", "line": 3, "severity": "medium"},
    ])
    keys = [i["key"] for i in items]
    assert not any("silent-except" in k for k in keys), (
        f"静默异常类不应出现在 medium 聚合里：{keys}")
    assert "module-medium:long-function" in keys


def test_low_severity_ignored(tmp_path, monkeypatch):
    items = _findings_for(tmp_path, monkeypatch, [
        {"rule": "noise", "file": "a.py", "line": 1, "severity": "low"},
    ])
    assert items == []


def test_locations_capped_at_forty(tmp_path, monkeypatch):
    many = [{"rule": "many", "file": f"f{i}.py", "line": i, "severity": "medium"}
            for i in range(60)]
    items = _findings_for(tmp_path, monkeypatch, many)
    assert len(items) == 1
    assert items[0]["detail"]["count"] == 60
    assert len(items[0]["detail"]["locations"]) == 40, "只保留前 40 处定位"


def test_missing_artifact_returns_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "RUNS", tmp_path)
    monkeypatch.setattr(loop_mod, "run_cmd", lambda *_a, **_k: {"ok": True})
    collected: list[dict] = []
    runs: dict = {}
    loop_mod._collect_module_scan(collected, runs)  # 不应抛
    assert collected == []


def test_collect_still_calls_module_collector(monkeypatch):
    called = {"n": 0}

    def _m(findings, runs):
        called["n"] += 1

    monkeypatch.setattr(loop_mod, "_collect_module_scan", _m)
    for name in dir(loop_mod):
        if name.startswith("_collect_") and name != "_collect_module_scan":
            monkeypatch.setattr(loop_mod, name, lambda findings, runs: None)
    monkeypatch.setattr(loop_mod, "run_cmd", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(loop_mod, "save", lambda state: None)
    monkeypatch.setattr(loop_mod, "load", lambda: {"items": [], "events": []})
    try:
        loop_mod.collect("test")
    except Exception:
        pass
    assert called["n"] >= 1, "collect 必须调用模块扫描采集器"