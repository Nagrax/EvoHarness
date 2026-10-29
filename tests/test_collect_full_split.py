"""collect 全部拆分后的完整回归（2026-10-29）。

collect: 440 → 41 行（纯编排）。拆分过程暴露**两个隐式耦合**，都在这条测试里固定：
① ⑫ 段曾复用 ⑪ 段的局部 `stability_path` → 改为共享 helper，段序不再有约束
② ⑫ 段曾复用 collect 的 `round_id` 与 `state` → 改为显式参数

这两个 bug 的共同点：**它们只在"代码被搬动"时才暴露**。所以测试要固定的是
"每个采集器都是自洽的"（不依赖外部作用域），而不只是"collect 还能跑"。
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.loop as loop_mod

ALL_COLLECTORS = [
    "_collect_backend", "_collect_frontend", "_collect_module_scan",
    "_collect_memory", "_collect_harness2", "_collect_calibration",
    "_collect_variance", "_collect_skill_combo", "_collect_judge_probe",
    "_collect_lifecycle_gate", "_collect_stability", "_finalize_round",
]


def test_collect_is_pure_orchestration():
    """collect 应只剩编排（≤60 行），信号采集全在采集器里。"""
    tree = ast.parse((REPO / "eval" / "loop.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "collect":
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            assert length <= 60, f"collect 又膨胀了：{length} 行（应为纯编排）"
            return
    raise AssertionError("collect 不存在")


def test_every_collector_is_self_contained():
    """采集器不得引用未定义的全局名（round_id/state 这类隐式耦合就是这么漏的）。"""
    src = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_globals = set(dir(loop_mod)) | {"__file__", "__name__"}
    problems = []
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and node.name in ALL_COLLECTORS):
            continue
        params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        assigned = {n.id for n in ast.walk(node)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        loaded = {n.id for n in ast.walk(node)
                  if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        # 只关心看起来像"本模块局部变量"的名字
        for name in sorted(loaded - params - assigned - module_globals):
            if name in {"findings", "runs", "state", "round_id"}:
                problems.append(f"{node.name} 引用了外部变量 {name}")
    assert not problems, "采集器仍有隐式外部依赖：" + "; ".join(problems)


def test_benchmark_failures_takes_state_and_round():
    sig = inspect.signature(loop_mod._finalize_round)
    assert "state" in sig.parameters, "必须显式接收 state"
    assert "round_id" in sig.parameters, "必须显式接收 round_id"
    assert sig.parameters["state"].default is None, "state 应有默认值便于单独测试"
    assert sig.parameters["round_id"].default == ""


def test_benchmark_failures_works_without_state():
    """state=None 时不得崩（内部兜空结构）。"""
    findings: list[dict] = []
    runs: dict = {}
    loop_mod._finalize_round(findings, runs, None, "rTEST")  # 不应抛
    assert isinstance(findings, list)


def test_all_collectors_callable_with_findings_and_runs():
    """统一契约：每个采集器都能以 (findings, runs) 调用（⑫ 额外参数有默认值）。"""
    for name in ALL_COLLECTORS:
        func = getattr(loop_mod, name)
        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        assert len(params) >= 2, f"{name} 至少要接收 findings 与 runs"
        for extra in params[2:]:
            assert extra.default is not inspect.Parameter.empty, (
                f"{name} 的额外参数 {extra.name} 必须有默认值")


def test_collect_runs_and_returns_round(tmp_path, monkeypatch):
    """collect 返回 round_record（含 roundId / sourceDigest / bySource）。

    必须隔离：collect 会写 BACKLOG.json，测试里绝不能碰生产台账
    （2026-10-29 踩过——真实 backlog 被测试冲掉，连带 4 个无关测试失败）。
    """
    # BACKLOG 是模块级常量（import 时由 HOME 算出），patch HOME 无效——
    # 必须直接 patch BACKLOG，否则测试会清空生产台账（真实踩过）。
    monkeypatch.setattr(loop_mod, "BACKLOG", tmp_path / "BACKLOG.json")
    monkeypatch.setattr(loop_mod, "HOME", tmp_path)
    monkeypatch.setattr(loop_mod, "RUNS", tmp_path)
    for name in ALL_COLLECTORS[:-1]:
        monkeypatch.setattr(loop_mod, name, lambda findings, runs: None)
    monkeypatch.setattr(loop_mod, "run_cmd", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(loop_mod, "load", lambda: {"items": [], "events": [], "rounds": []})

    result = loop_mod.collect("rTEST-SPLIT")
    assert isinstance(result, dict)
    assert result.get("roundId") == "rTEST-SPLIT", f"返回值应带 roundId：{list(result)[:6]}"
    assert isinstance(result.get("bySource") or {}, dict)
    # 生产台账不得出现测试轮次（隔离验证）
    production = REPO / "eval" / "loop" / "BACKLOG.json"
    if production.is_file():
        assert "rTEST-SPLIT" not in production.read_text(encoding="utf-8"), \
            "测试污染了生产 BACKLOG（必须 monkeypatch HOME/RUNS）"
