"""M2 归因工具：按层×子项的全量失败分布 + 正则前后对比。

纯 stdlib，惰性依赖 agents.skill_eval_rubric 的 assign_layer（避免 import 期强耦合）。
"""

from __future__ import annotations

import re


def _assign(rule_id: str) -> tuple:
    try:
        from agents.skill_eval_rubric import assign_layer

        return assign_layer(rule_id)
    except Exception:
        return "CNT", "generic"


def _ratio(numerator: int, denominator: int) -> float:
    denominator = int(denominator or 0)
    if denominator <= 0:
        return 0.0
    return round(float(numerator or 0) / denominator, 4)


def macro_breakdown(outcomes: list) -> dict:
    """按层×子项统计失败分布。

    必须基于**全量** outcomes 计算——绝不允许只吃被截断的 failures[:20]
    （摘要里的 failures 有 20 条帽，用它统计会系统性低估长尾失败）。
    """
    rows = [item for item in (outcomes or []) if isinstance(item, dict)]
    total = len(rows)
    failed_total = 0
    layers: dict = {}
    for row in rows:
        layer = str(row.get("layer") or "").strip().upper()
        sub_item = str(row.get("sub_item") or "").strip()
        if not layer or not sub_item:
            assigned_layer, assigned_sub = _assign(row.get("rule_id"))
            layer = layer or assigned_layer
            sub_item = sub_item or assigned_sub
        passed = bool(row.get("passed"))
        entry = layers.setdefault(layer, {"layer": layer, "total": 0, "failed": 0, "sub_items": {}})
        entry["total"] += 1
        cell = entry["sub_items"].setdefault(
            sub_item, {"layer": layer, "sub_item": sub_item, "total": 0, "failed": 0}
        )
        cell["total"] += 1
        if not passed:
            entry["failed"] += 1
            cell["failed"] += 1
            failed_total += 1
    for entry in layers.values():
        entry["passed"] = entry["total"] - entry["failed"]
        entry["pass_rate"] = _ratio(entry["passed"], entry["total"])
        for cell in entry["sub_items"].values():
            cell["passed"] = cell["total"] - cell["failed"]
            cell["pass_rate"] = _ratio(cell["passed"], cell["total"])
    return {
        "total": total,
        "failed": failed_total,
        "passed": total - failed_total,
        "pass_rate": _ratio(total - failed_total, total),
        "layers": layers,
    }


def _count_matches(texts: object, pattern: re.Pattern) -> int:
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, (list, tuple)):
        return 0
    hits = 0
    for item in texts:
        hits += len(pattern.findall(str(item or "")))
    return hits


def reproducible_compare(texts_before: object, texts_after: object, pattern: str) -> dict:
    """正则命中数前后对比：返回 before/after/delta/improved。"""
    compiled = re.compile(str(pattern or ""))
    before = _count_matches(texts_before, compiled)
    after = _count_matches(texts_after, compiled)
    return {
        "pattern": str(pattern or ""),
        "before": before,
        "after": after,
        "delta": after - before,
        "improved": after > before,
    }
