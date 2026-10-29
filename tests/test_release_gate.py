"""M4 门禁（Release Gate）回归测试：命中与未命中、缺失指标、多门禁、清单加载。

纯确定性：合成 metrics/gates 直接断言，不依赖模型或网络。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.release_gate import evaluate_gate, load_gates


def test_evaluate_gate_passes_when_all_met():
    metrics = {"pass_at_1": 0.91, "avg_latency_s": 12.0}
    gates = [
        {"metric": "pass_at_1", "op": ">=", "threshold": 0.90},
        {"metric": "avg_latency_s", "op": "<=", "threshold": 15.0},
    ]
    result = evaluate_gate(metrics, gates)
    assert result == {"passed": True, "violations": []}


def test_evaluate_gate_flags_violation():
    metrics = {"pass_at_1": 0.85, "avg_latency_s": 20.0}
    gates = [
        {"metric": "pass_at_1", "op": ">=", "threshold": 0.90},
        {"metric": "avg_latency_s", "op": "<=", "threshold": 15.0},
    ]
    result = evaluate_gate(metrics, gates)
    assert result["passed"] is False
    assert result["violations"] == [
        {"metric": "pass_at_1", "value": 0.85, "op": ">=", "threshold": 0.90},
        {"metric": "avg_latency_s", "value": 20.0, "op": "<=", "threshold": 15.0},
    ]


def test_evaluate_gate_strict_and_boundary_ops():
    # 边界：>= / <= 含等号通过，> / < 不含等号则未命中
    assert evaluate_gate({"x": 5}, [{"metric": "x", "op": ">=", "threshold": 5}])["passed"] is True
    assert evaluate_gate({"x": 5}, [{"metric": "x", "op": "<=", "threshold": 5}])["passed"] is True
    assert evaluate_gate({"x": 5}, [{"metric": "x", "op": ">", "threshold": 5}])["passed"] is False
    assert evaluate_gate({"x": 5}, [{"metric": "x", "op": "<", "threshold": 5}])["passed"] is False


def test_evaluate_gate_missing_metric_is_violation():
    result = evaluate_gate({}, [{"metric": "accuracy", "op": ">=", "threshold": 0.9}])
    assert result["passed"] is False
    assert result["violations"] == [
        {"metric": "accuracy", "value": None, "op": ">=", "threshold": 0.9}
    ]


def test_evaluate_gate_empty_gates_pass():
    assert evaluate_gate({"anything": 1}, []) == {"passed": True, "violations": []}


def test_load_gates_list_and_wrapped(tmp_path):
    raw = [
        {"metric": "pass_at_1", "op": ">=", "threshold": 0.9},
        {"metric": "drift_alerts", "op": "<=", "threshold": 0},
    ]
    plain = tmp_path / "gates.json"
    plain.write_text(json.dumps(raw), encoding="utf-8")
    assert load_gates(plain) == raw
    assert load_gates(str(plain)) == raw

    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"gates": raw}), encoding="utf-8")
    assert load_gates(wrapped) == raw


def test_load_gates_roundtrip_into_evaluate_gate(tmp_path):
    path = tmp_path / "gates.json"
    path.write_text(
        json.dumps([{"metric": "pass_at_1", "op": ">=", "threshold": 0.9}]),
        encoding="utf-8",
    )
    result = evaluate_gate({"pass_at_1": 0.95}, load_gates(path))
    assert result == {"passed": True, "violations": []}

def test_evaluate_gate_non_numeric_value_is_violation_without_crash():
    # 指标为非数值（字符串）时不得抛 TypeError，一律记 violation（与自述一致）。
    result = evaluate_gate({"x": "a"}, [{"metric": "x", "op": ">=", "threshold": 0.5}])
    assert result["passed"] is False
    assert result["violations"] == [
        {"metric": "x", "value": "a", "op": ">=", "threshold": 0.5}
    ]


def test_evaluate_gate_bool_value_is_type_mismatch_violation():
    # bool 对数值门禁属类型不符：True 不得按 1.0 过门。
    result = evaluate_gate({"x": True}, [{"metric": "x", "op": ">=", "threshold": 0.5}])
    assert result["passed"] is False
    assert result["violations"] == [
        {"metric": "x", "value": True, "op": ">=", "threshold": 0.5}
    ]
    # False 亦不得按 0.0 过门。
    assert evaluate_gate({"x": False}, [{"metric": "x", "op": ">=", "threshold": 0.0}])["passed"] is False


def test_evaluate_gate_non_numeric_threshold_is_violation_without_crash():
    # 阈值类型不符导致比较失败时同样记 violation，不崩溃。
    result = evaluate_gate({"x": 1.0}, [{"metric": "x", "op": ">=", "threshold": "high"}])
    assert result["passed"] is False
    assert result["violations"] == [
        {"metric": "x", "value": 1.0, "op": ">=", "threshold": "high"}
    ]
