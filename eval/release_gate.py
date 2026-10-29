"""发布门禁（Release Gate）：纯确定性阈值阻断与报告。

职责边界（严格遵守）：本模块**只做阻断判定与报告**，绝不执行任何 skill
归档动作——不导入、不调用任何归档相关函数。门禁只回答"指标是否达标"。

用法：
    from eval.release_gate import load_gates, evaluate_gate

    gate = evaluate_gate({"accuracy": 0.91}, load_gates("gates.json"))
    if not gate["passed"]:
        print(gate["violations"])  # 阻断信号，交由上游决定处置
"""

from __future__ import annotations

import json
import operator
from pathlib import Path

_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


def _is_numeric(value) -> bool:
    # bool is a subclass of int, but it is a type mismatch for a numeric gate.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def evaluate_gate(metrics: dict, gates: list) -> dict:
    """对 metrics 逐条门禁求值，返回是否通过及违反清单。

    参数：
        metrics: {metric: value}，指标当前取值。
        gates:   [{"metric", "op", "threshold"}]，op ∈ {">=", "<=", ">", "<"}。

    返回：
        {"passed": bool, "violations": [{"metric","value","op","threshold"}]}。
        门禁未命中（指标缺失或比较不成立）一律记入 violations；纯确定性，无副作用。
    """
    violations: list[dict] = []
    for gate in gates:
        metric = gate["metric"]
        op = gate["op"]
        threshold = gate["threshold"]
        if op not in _OPS:
            raise ValueError(f"unsupported op: {op!r}")
        value = metrics.get(metric)
        if not _is_numeric(value):
            # Non-numeric / wrong type (bool included: for a numeric gate it is a type
            # mismatch and must not pass as 1.0/0.0) -> record a violation, never raise;
            # consistent with "anything not met is recorded as a violation".
            violations.append(
                {"metric": metric, "value": value, "op": op, "threshold": threshold}
            )
            continue
        try:
            hit = bool(_OPS[op](value, threshold))
        except TypeError:
            # Threshold/type mismatch makes the comparison fail -> violation, no crash.
            hit = False
        if not hit:
            violations.append(
                {"metric": metric, "value": value, "op": op, "threshold": threshold}
            )
    return {"passed": not violations, "violations": violations}


def load_gates(path) -> list:
    """读取 JSON 门禁清单。支持顶层为数组，或 {"gates": [...]} 包装。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("gates"), list):
        return data["gates"]
    raise ValueError(f"invalid gates manifest: {path}")
