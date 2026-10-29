"""W12 回归：冻结协议守卫——协议完整性与预算前置门。

对应验收：
- "零预算/不足余额在请求前拒绝；并行预留不超总限；超时账单不能记0成本"
- "冻结E05四策略质量/成本约束和不劣效界值；原可选消融保持optional"
- 协议引用的工具必须真实存在（引用不存在的工具=纸面协议）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.frozen_protocol_guard as guard


def test_frozen_protocol_is_valid():
    """冻结协议必须结构完整且自洽（这是 W12 的主交付）。"""
    problems, data = guard.check_protocol()
    assert not problems, f"冻结协议存在问题：{problems}"
    assert data["version"] == 1
    # 关键冻结项必须真实存在
    assert data["sampleSizes"]["formal"]["repeats"] >= 3
    assert data["sampleSizes"]["formal"]["holdoutPerArm"] * \
        data["sampleSizes"]["formal"]["repeats"] >= 30


def test_protocol_declares_optional_ablations():
    """原可选消融必须显式保持 optional，不得静默缩范围。"""
    problems, data = guard.check_protocol()
    assert not problems, f"协议本身应有效：{problems}"
    optional = data.get("optionalAblations") or {}
    # status 允许带说明后缀，但必须以 optional 开头
    assert str(optional.get("status") or "").startswith("optional")
    assert optional.get("items"), "可选清单不得为空"


def test_protocol_declares_scope_reductions_honestly():
    """缩范围必须保留未执行清单——禁止用文档代替实现。"""
    problems, data = guard.check_protocol()
    assert not problems
    reductions = data.get("scopeReductions") or {}
    assert reductions.get("honestList"), "未执行清单不得为空"
    text = json.dumps(reductions, ensure_ascii=False)
    assert "未跑" in text or "未执行" in text


def test_tool_refs_must_exist():
    """协议引用不存在的工具必须被检出（纸面协议防线）。

    构造一份引用不存在工具的协议，验证 check_protocol 的文件存在性检查
    真的在比对文件系统，而不是走过场。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        fake_repo = Path(tmp)
        (fake_repo / "eval").mkdir()
        fake_protocol = fake_repo / "eval" / "FROZEN_PROTOCOL.json"
        fake_protocol.write_text(json.dumps({
            "version": 1, "arms": {}, "sampleSizes": {}, "statistics": {},
            "budget": {}, "qualityConstraints": {}, "scopeReductions": {},
            "changeControl": {},
        }), encoding="utf-8")
        # check_protocol 从模块级 PROTOCOL 读；替换为假协议目录下的文件，
        # 同时让 REQUIRED_TOOL_REFS 的相对路径在假 repo 下解析——但函数里用的是
        # 模块级 REPO，所以这里改为临时 monkeypatch 两个模块级路径。
        original_protocol, original_repo = guard.PROTOCOL, guard.REPO
        guard.PROTOCOL = fake_protocol
        guard.REPO = fake_repo
        try:
            problems, _ = guard.check_protocol()
        finally:
            guard.PROTOCOL, guard.REPO = original_protocol, original_repo
    assert any(p.startswith("tool-ref-missing:") for p in problems), (
        f"引用不存在工具的协议必须被检出，实际 problems={problems}")
    # 空壳协议的结构缺失也应被检出（arms/sampleSizes/quality 等字段校验）
    assert any("threeArm" in p or "effectiveN" in p or "judgeBiasGates" in p
               for p in problems), (
        f"空壳协议的字段校验必须被检出，实际 problems={problems}")


def test_budget_gate_rejects_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "LEDGER", tmp_path / "absent.json")
    code, detail = guard.check_budget(1000)
    assert code == 4
    assert detail["code"] == "budget-missing"


def test_budget_gate_rejects_insufficient_balance(tmp_path):
    ledger = tmp_path / "LIVE_BUDGET.json"
    ledger.write_text(json.dumps({"totalTokens": 100, "spentTokens": 80, "remainingTokens": 20}),
                      encoding="utf-8")
    original = guard.LEDGER
    guard.LEDGER = ledger
    try:
        code, detail = guard.check_budget(1000)
        assert code == 4
        assert detail["code"] == "insufficient-balance"
        assert "blocked" in detail.get("action", "")
        assert detail["remainingTokens"] == 20
    finally:
        guard.LEDGER = original


def test_budget_gate_allows_within_balance(tmp_path):
    ledger = tmp_path / "LIVE_BUDGET.json"
    ledger.write_text(json.dumps({"totalTokens": 300000000, "spentTokens": 9318225,
                                  "remainingTokens": 290681775}), encoding="utf-8")
    original = guard.LEDGER
    guard.LEDGER = ledger
    try:
        code, detail = guard.check_budget(25000000)
        assert code == 0
        assert detail["ok"] is True
    finally:
        guard.LEDGER = original


def test_real_ledger_supports_formal_curve():
    """真实台账必须能覆盖 25M 的正式曲线（W14 前置条件）。"""
    code, detail = guard.check_budget(25000000)
    assert code == 0, f"正式曲线预算不足：{detail}"
    assert detail["remainingTokens"] >= 25000000
