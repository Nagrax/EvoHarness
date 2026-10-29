"""采样科学与指标漂移告警（纯 stdlib）。

- required_sample_size：按标准公式由目标置信区间半宽反推样本量。
- detect_drift：对带置信区间的指标时序做相邻点显著性比较，输出漂移告警。
"""

from __future__ import annotations

import math


def required_sample_size(ci_pp: float, p: float = 0.5, z: float = 1.96) -> int:
    """目标 ±ci_pp 百分点半宽所需样本量。

    标准公式：n = ceil(z^2 * p(1-p) / (ci_pp/100)^2)。
    ci_pp 是百分点数值（1 表示 ±1pp）。例子：ci_pp=1, p=0.5, z=1.96 → 9604。
    """
    if ci_pp <= 0:
        raise ValueError("ci_pp must be positive")
    if not 0.0 <= p <= 1.0:
        raise ValueError("p must be within [0, 1]")
    if z <= 0:
        raise ValueError("z must be positive")
    half_width = ci_pp / 100.0
    return math.ceil(z * z * p * (1.0 - p) / (half_width * half_width))


def _nonneg_ci_half(point: dict) -> float:
    """读取并校验单点的 ci_half：必须是数值且非负，否则抛 ValueError。

    ci_half 为负会使 threshold 非正，导致恒告警（假阳性）；非数值则无法比较。
    """
    raw = point.get("ci_half")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("ci_half must be a non-negative number")
    value = float(raw)
    if value < 0:
        raise ValueError("ci_half must be non-negative")
    return value


def detect_drift(series: list) -> list:
    """时序漂移告警：相邻两点差值的绝对值超过两点 ci_half 的合成标准误差即判漂移。

    参数：
        series: [{"t": iso_str, "value": float, "ci_half": float}]，按时间升序。
    返回：
        [{"t", "value", "prev", "delta", "threshold"}]，delta = value - prev（带符号），
        threshold = sqrt(prev_ci^2 + cur_ci^2)。独立误差合成比简单相加（旧实现）更严格，
        可减少漏报；两点差值的标准误应按平方和开方合成。
    校验：ci_half 必须为非负数值，否则抛 ValueError（负值会使 threshold 非正、
    导致恒告警的假阳性）。
    """
    alerts: list[dict] = []
    for prev, cur in zip(series, series[1:]):
        prev_value = float(prev["value"])
        value = float(cur["value"])
        delta = value - prev_value
        prev_ci = _nonneg_ci_half(prev)
        cur_ci = _nonneg_ci_half(cur)
        threshold = math.sqrt(prev_ci * prev_ci + cur_ci * cur_ci)
        if abs(delta) > threshold:
            alerts.append(
                {
                    "t": cur["t"],
                    "value": value,
                    "prev": prev_value,
                    "delta": delta,
                    "threshold": threshold,
                }
            )
    return alerts
