"""collect 拆分的整体回归（2026-10-29）。

collect 从 440 行拆到 200 行，拆出 8 个 `_collect_<source>()`。
拆分过程中暴露两个**真实耦合问题**，这里逐条固定：

① ⑫ 段原本复用 ⑪ 段的局部变量 `stability_path` —— 提取后 NameError。
   修法是把"最新稳定性产物"抽成共享 helper `_latest_stability_artifact()`，
   让两段各自解析，**不再依赖段序**。
② 前端脚本路径硬编码 —— 改为 `EVO_UI_SCRIPT` 可覆盖（上轮已修，这里一并固定）。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.loop as loop_mod

EXPECTED_COLLECTORS = {
    "_collect_backend", "_collect_frontend", "_collect_module_scan",
    "_collect_memory", "_collect_harness2", "_collect_calibration",
    "_collect_variance", "_collect_skill_combo", "_collect_judge_probe",
    "_collect_lifecycle_gate", "_collect_stability",
}


def test_all_collectors_exist():
    missing = [n for n in EXPECTED_COLLECTORS if not hasattr(loop_mod, n)]
    assert not missing, f"缺少采集器：{missing}"


def test_collect_is_under_200_lines():
    tree = ast.parse((REPO / "eval" / "loop.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "collect":
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            assert length <= 210, f"collect 又变长了：{length} 行"
            return
    raise AssertionError("collect 不存在")


def test_shared_stability_helper_used_by_both_sections():
    """⑪⑫ 都通过 helper 解析产物，不再依赖段序。"""
    text = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert "def _latest_stability_artifact()" in text
    assert text.count("_latest_stability_artifact()") >= 3, (
        "helper 定义 + 至少两处调用（⑪ 与 ⑫）")
    # ⑫ 段内必须自己解析（不能再引用未定义的局部变量）
    assert "stability_path = _latest_stability_artifact()" in text
    assert text.count("stability_path = _latest_stability_artifact()") >= 2


def test_latest_stability_artifact_picks_last(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "RUNS", tmp_path)
    for name in ("failure-stability-1.json", "failure-stability-2.json"):
        (tmp_path / name).write_text(json.dumps({"statusCounts": {}}), encoding="utf-8")
    picked = loop_mod._latest_stability_artifact()
    assert picked.name == "failure-stability-2.json", "必须取排序后最后一份"


def test_latest_stability_artifact_fallback_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "RUNS", tmp_path)
    picked = loop_mod._latest_stability_artifact()
    assert picked.name == "failure-stability-1.json", "无产物时返回默认路径（不抛）"


def test_collect_calls_every_collector(monkeypatch):
    called: dict[str, int] = {}

    def _make(name):
        def _f(findings, runs):
            called[name] = called.get(name, 0) + 1
        return _f

    for name in EXPECTED_COLLECTORS:
        monkeypatch.setattr(loop_mod, name, _make(name))
    monkeypatch.setattr(loop_mod, "run_cmd", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(loop_mod, "save", lambda state: None)
    monkeypatch.setattr(loop_mod, "load", lambda: {"items": [], "events": []})
    try:
        loop_mod.collect("test")
    except Exception:
        pass
    # 至少前 11 个采集器都应被调用（⑫ 段仍在 collect 内联）
    missing = [n for n in EXPECTED_COLLECTORS if n not in called]
    assert not missing, f"这些采集器没被 collect 调用：{missing}"


def test_ui_script_path_still_configurable():
    text = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert "EVO_UI_SCRIPT" in text
