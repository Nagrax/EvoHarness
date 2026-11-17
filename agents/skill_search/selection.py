"""DE-33 选择：确定性活动前沿、领先 Case 加权父代抽样与全池搜索冠军。

对应 SKILL-EVOLUTION-SEARCH §10.3/§10.4（确定性活动前沿与父代选择）：

- **活动前沿 F ≠ 完整 Pareto 集 ≠ 均分 top-k**：每个 Case 取池中最高分
  ``best_i``，候选领先集合 ``W(c) = {i : |score(c,i) - best_i| <= epsilon}``；
  确定性贪心覆盖——反复选择覆盖最多**尚未覆盖 Case** 的候选（互补低均分
  候选凭 Case 领先覆盖贡献进入前沿），决胜顺序固定：全 S 均分 → 更高最差分
  → 更早创建序号 → ID 字典序。Case 领先但无新增覆盖贡献的候选不进前沿。
- **父代抽样权重 = len(W(c))**（全部领先 Case 数量，不是贪心步骤里的新增
  覆盖数）；概率 ``len(W(c)) / sum(len(W(f)))``。权重与数值 epsilon 不是
  统计置信区间。
- **可恢复 RNG**：``ParentSampler`` 保存 seed、算法版本、完整 RNG state 与
  draw index；状态恢复后继续给出同一父代序列；并发评测完成顺序不能改变
  下一次随机选择（抽样只依赖前沿权重与 RNG 状态）。
- **搜索冠军 C 从全部池条目选**（不只是前沿）：并列按协议冻结的分数量化键
  → 更高最差分 → 通过 Case 数 → **相对种子累计修改量** → 创建序号 → ID。
  相对父 diff 只用于解释一次变异，不用于低估多代累计改动。
- **fail-closed**：空池/全零权重不除零、不采样，返回明确原因；Case 无法被
  任何候选覆盖时前沿判定数据不完整（``frontier_incomplete``），不降级采样。
- **确定性**：候选遍历按固定 tie 键排序归一（与池输入排列无关）；Case 聚合
  按排序 Case 序 + ``math.fsum``（与 Case 完成顺序无关）；无墙钟时间、无
  隐式全局状态（RNG 状态显式保存在 sampler 内并可序列化恢复）。
- **作用域边界（S4）**：本模块只对池记录做纯计算，产出结构化选择结果
  （frontier/weights/probabilities/search_champion），复用原 variant ID 与
  champion 展示语义（``search_champion`` 即池候选 ID）；不写任何生产技能
  制品、usage/tier 表，不与生产 ``discover_skills`` 交互，不触发晋级。

真实模型能力实验（预算/审核真值/隔离执行环境）另行 blockedOn，见任务 DE-33；
本模块为纯离线合成可验证逻辑。
"""
from __future__ import annotations

import math
import random
from fractions import Fraction
from typing import Any

from .pool import SCORE_EPSILON

SELECTION_SCHEMA_VERSION = 1
FRONTIER_ALGORITHM_VERSION = "case-lead-greedy-cover-v1"
SAMPLER_ALGORITHM_VERSION = "lead-weighted-parent-v1"
CHAMPION_ALGORITHM_VERSION = "pool-wide-tie-chain-v1"
CHAMPION_QUANTIZATION_DECIMALS = 9

FRONTIER_INCOMPLETE = "frontier_incomplete"
FRONTIER_EMPTY = "frontier_empty"


def _usable(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """可用池条目：已入池且未标记不可用（不可用记录不参与前沿/冠军/抽样）。"""
    return [
        dict(record)
        for record in records
        if record.get("admitted") and not record.get("unusable")
    ]


def _case_order(case_ids: list[str]) -> list[str]:
    return sorted({str(cid) for cid in case_ids})


def _quantize(value: float, decimals: int = CHAMPION_QUANTIZATION_DECIMALS) -> float:
    """冠军并列用的分数量化键（协议冻结的小数位，缺省 9 位）。"""
    return round(float(value), int(decimals))


def _candidate_scores(record: dict[str, Any], case_ids: list[str]) -> dict[str, float] | None:
    vector = record.get("score_vector")
    if not isinstance(vector, dict):
        return None
    if {str(k) for k in vector} != set(case_ids):
        return None  # 与冻结 Case 集不一致的记录不参与（fail-closed，不猜分）
    try:
        values = {cid: float(vector[cid]) for cid in case_ids}
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(v) for v in values.values()):
        return None
    return values


def _frontier_sort_key(record: dict[str, Any]) -> tuple:
    """前沿/贪心候选的固定遍历序：均分降序 → 最差分降序 → 创建序号升序 → ID。"""
    return (
        -float(record.get("average_score") or 0.0),
        -float(record.get("worst_score") or 0.0),
        int(record.get("created_sequence") or 0),
        str(record.get("candidate_id") or ""),
    )


def _record_summary(record: dict[str, Any], values: dict[str, float]) -> dict[str, Any]:
    ordered = sorted(values)
    scored = [values[cid] for cid in ordered]
    total = math.fsum(scored)
    if record.get("average_score") is not None:
        average = float(record["average_score"])
    else:
        average = total / len(scored) if scored else 0.0
    if record.get("worst_score") is not None:
        worst = float(record["worst_score"])
    else:
        worst = min(scored) if scored else 0.0
    if record.get("passed_case_count") is not None:
        passed = int(record["passed_case_count"])
    else:
        passed = sum(1 for cid in ordered if values[cid] >= 1.0)
    return {
        "average_score": average,
        "worst_score": worst,
        "passed_case_count": passed,
    }


# ── 确定性活动前沿（贪心 Case 领先覆盖） ────────────────────────────────────


def compute_lead_sets(
    records: list[dict[str, Any]],
    *,
    case_ids: list[str],
    epsilon: float = SCORE_EPSILON,
) -> dict[str, Any]:
    """逐 Case 池内最高分 ``best_i`` 与候选领先集合 ``W(c)``（§10.3 口径）。

    返回 ``{ok, best, lead_sets: {candidate_id: sorted[case_id]}}``；
    输入含与冻结 Case 集不一致/含非法分数的记录时整体 fail-closed。
    """
    ids = _case_order(case_ids)
    if not ids:
        return {"ok": False, "error": "case_ids_missing"}
    usable = _usable(records)
    if not usable:
        return {"ok": False, "error": FRONTIER_EMPTY, "reason": "no usable pool candidates"}
    seen_ids: set[str] = set()
    scores: dict[str, dict[str, float]] = {}
    for record in usable:
        cid = str(record.get("candidate_id") or "")
        if not cid or cid in seen_ids:
            return {"ok": False, "error": "duplicate_or_missing_candidate_id", "candidate_id": cid}
        seen_ids.add(cid)
        values = _candidate_scores(record, ids)
        if values is None:
            return {
                "ok": False,
                "error": "candidate_score_vector_invalid",
                "candidate_id": cid,
                "reason": "score_vector 与冻结 Case 集不一致或含非法分数（fail-closed）",
            }
        scores[cid] = values
    best = {
        case_id: max(scores[cid][case_id] for cid in seen_ids)
        for case_id in ids
    }
    lead_sets = {
        cid: sorted(
            case_id
            for case_id in ids
            if abs(scores[cid][case_id] - best[case_id]) <= epsilon
        )
        for cid in sorted(seen_ids)
    }
    return {"ok": True, "best": best, "lead_sets": lead_sets, "case_ids": ids, "epsilon": float(epsilon)}


def compute_active_frontier(
    records: list[dict[str, Any]],
    *,
    case_ids: list[str],
    epsilon: float = SCORE_EPSILON,
) -> dict[str, Any]:
    """确定性活动前沿（§10.3）：Case 领先集合的贪心覆盖，不是 Pareto/top-k。

    - 候选遍历按 ``_frontier_sort_key`` 固定序归一，池输入排列不改变结果；
    - 每步选择覆盖最多**尚未覆盖 Case** 的候选，平局按固定序取先者；
    - 有 Case 无法被任何候选覆盖 → ``ok=False``（数据不完整，不降级采样）；
    - 权重 = ``len(W(c))``（全部领先 Case 数），概率 = 权重/总权重；
    - ``coverage_order`` 记录贪心选择顺序（供报告展示与抽样确定性走查）。
    """
    leads = compute_lead_sets(records, case_ids=case_ids, epsilon=epsilon)
    if not leads.get("ok"):
        return {
            "ok": False,
            "error": leads.get("error") or "lead_sets_failed",
            "reason": leads.get("reason") or "lead sets unavailable",
            "frontier": [],
            "weights": {},
            "probabilities": {},
            "algorithm_version": FRONTIER_ALGORITHM_VERSION,
        }
    ids: list[str] = leads["case_ids"]
    lead_sets: dict[str, list[str]] = leads["lead_sets"]
    usable = [dict(r) for r in _usable(records)]
    usable.sort(key=_frontier_sort_key)

    uncovered = set(ids)
    coverage_order: list[str] = []
    while uncovered:
        best_pick: dict[str, Any] | None = None
        best_gain = 0
        for record in usable:
            cid = str(record.get("candidate_id") or "")
            gain = len(set(lead_sets[cid]) & uncovered)
            if gain > best_gain:
                best_pick, best_gain = record, gain
        if best_pick is None:
            return {
                "ok": False,
                "error": FRONTIER_INCOMPLETE,
                "reason": "存在无任何候选覆盖的 Case：数据不完整，不能除零采样",
                "uncovered_cases": sorted(uncovered),
                "frontier": list(coverage_order),
                "weights": {},
                "probabilities": {},
                "algorithm_version": FRONTIER_ALGORITHM_VERSION,
            }
        pick_id = str(best_pick["candidate_id"])
        coverage_order.append(pick_id)
        uncovered -= set(lead_sets[pick_id])

    weights = {cid: len(lead_sets[cid]) for cid in coverage_order}
    total_weight = int(sum(weights.values()))
    probabilities = {
        cid: weights[cid] / total_weight if total_weight else 0.0 for cid in coverage_order
    }
    weight_fractions = (
        {cid: str(Fraction(weights[cid], total_weight)) for cid in coverage_order}
        if total_weight
        else {}
    )
    return {
        "ok": True,
        "error": "",
        "frontier": list(coverage_order),
        "coverage_order": list(coverage_order),
        "lead_sets": {cid: list(lead_sets[cid]) for cid in sorted(lead_sets)},
        "best_per_case": dict(leads["best"]),
        "weights": weights,
        "total_weight": total_weight,
        "probabilities": probabilities,
        "weight_fractions": weight_fractions,
        "excluded_count": len(usable) - len(coverage_order),
        "epsilon": float(epsilon),
        "algorithm_version": FRONTIER_ALGORITHM_VERSION,
        "note": "前沿=贪心领先覆盖；不是完整 Pareto 集也不是均分 top-k；权重为全部领先 Case 数",
    }


# ── 父代抽样：领先 Case 数加权，RNG 状态可保存/恢复 ─────────────────────────


class ParentSampler:
    """按前沿权重抽父代；seed/RNG state/draw index 全部可序列化恢复。

    - 抽样只依赖前沿权重与自身 RNG 状态：Case 完成顺序、池输入排列、并发
      返回顺序都不能改变下一次随机选择；
    - ``draw`` 每次精确消耗一个 ``random.Random.random()``；累计走查按前沿
      覆盖顺序（确定性）；
    - 总权重为零（空池/全零权重）→ ``ok=False`` 明确原因，**不除零不采样**。
    """

    def __init__(self, *, seed: int = 0, state: dict[str, Any] | None = None):
        self._seed = int(seed)
        self._rng = random.Random(self._seed)
        self._draw_index = 0
        if state is not None:
            self._load_state(state)

    def _load_state(self, state: dict[str, Any]) -> None:
        self._seed = int(state.get("seed") or 0)
        self._draw_index = int(state.get("draw_index") or 0)
        self._rng = random.Random(self._seed)
        version = int(state.get("python_random_version") or 3)
        internal = tuple(int(x) for x in (state.get("internal_state") or []))
        gauss = state.get("gauss_next")
        self._rng.setstate((version, internal, gauss))

    def get_state(self) -> dict[str, Any]:
        version, internal, gauss = self._rng.getstate()
        return {
            "sampler_version": SAMPLER_ALGORITHM_VERSION,
            "seed": self._seed,
            "draw_index": self._draw_index,
            "python_random_version": int(version),
            "internal_state": [int(x) for x in internal],
            "gauss_next": gauss,
        }

    def draw(self, frontier_result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(frontier_result, dict) or not frontier_result.get("ok"):
            return {
                "ok": False,
                "error": "frontier_not_ok",
                "reason": str((frontier_result or {}).get("reason") or "frontier unavailable"),
                "parent_id": "",
            }
        weights: dict[str, int] = dict(frontier_result.get("weights") or {})
        frontier_order: list[str] = list(frontier_result.get("frontier") or [])
        total = int(sum(int(weights.get(cid) or 0) for cid in frontier_order))
        if total <= 0 or not frontier_order:
            return {
                "ok": False,
                "error": "no_weight_total_zero",
                "reason": "无可用权重（空前沿/全零权重）：不除零、不采样",
                "parent_id": "",
                "draw_index": self._draw_index,
                "sampler_version": SAMPLER_ALGORITHM_VERSION,
            }
        state_before = self.get_state()
        pick_value = self._rng.random()
        chosen = frontier_order[-1]
        cumulative = 0.0
        for cid in frontier_order:
            cumulative += int(weights.get(cid) or 0) / total
            if pick_value < cumulative:
                chosen = cid
                break
        self._draw_index += 1
        return {
            "ok": True,
            "parent_id": chosen,
            "draw_index": self._draw_index,
            "random_value": pick_value,
            "weights": weights,
            "probabilities": dict(frontier_result.get("probabilities") or {}),
            "frontier_order": list(frontier_order),
            "total_weight": total,
            "seed": self._seed,
            "sampler_version": SAMPLER_ALGORITHM_VERSION,
            "rng_state_before": state_before,
            "rng_state_after": self.get_state(),
            "algorithm": "cumulative_walk_on_deterministic_frontier_order",
        }


# ── 搜索冠军：从全部池条目按固定 tie 链选取 ─────────────────────────────────


def select_search_champion(
    records: list[dict[str, Any]],
    *,
    case_ids: list[str],
    epsilon: float = SCORE_EPSILON,
    quantization_decimals: int = CHAMPION_QUANTIZATION_DECIMALS,
) -> dict[str, Any]:
    """搜索冠军从**全部池条目**（不只前沿）按 §10.4 tie 链选取：

    分数量化键（均分）→ 量化最差分 → 通过 Case 数 → **相对种子累计修改量**
    → 创建序号 → ID。``cumulative_seed_diff`` 对齐 DE-32 谱系计数
    （``{added_bytes, ops, generations}``，也可为数值）；量化位数由协议冻结
    （缺省 9 位）。返回复用原 champion 展示语义：``search_champion`` 即池
    候选 ID（variant ID），并给出 tie 链各键明细；不产任何 tier/晋级字段。
    """
    ids = _case_order(case_ids)
    usable = _usable(records)
    if not usable or not ids:
        return {
            "ok": False,
            "error": "champion_empty",
            "reason": "无可用池条目，无法选搜索冠军",
            "search_champion": "",
            "ranking": [],
            "algorithm_version": CHAMPION_ALGORITHM_VERSION,
        }
    seen: set[str] = set()
    entries: list[tuple[tuple, dict[str, Any], dict[str, Any]]] = []
    for record in usable:
        cid = str(record.get("candidate_id") or "")
        if not cid or cid in seen:
            return {
                "ok": False,
                "error": "duplicate_or_missing_candidate_id",
                "candidate_id": cid,
                "search_champion": "",
                "ranking": [],
                "algorithm_version": CHAMPION_ALGORITHM_VERSION,
            }
        seen.add(cid)
        values = _candidate_scores(record, ids)
        if values is None:
            return {
                "ok": False,
                "error": "candidate_score_vector_invalid",
                "candidate_id": cid,
                "reason": "score_vector 与冻结 Case 集不一致或含非法分数（fail-closed）",
                "search_champion": "",
                "ranking": [],
                "algorithm_version": CHAMPION_ALGORITHM_VERSION,
            }
        summary = _record_summary(record, values)
        added, ops, generations = _seed_diff_parts(record.get("cumulative_seed_diff"))
        key = (
            -_quantize(summary["average_score"], quantization_decimals),
            -_quantize(summary["worst_score"], quantization_decimals),
            -int(summary["passed_case_count"]),
            added,
            ops,
            generations,
            int(record.get("created_sequence") or 0),
            cid,
        )
        breakdown = {
            "quantized_average": _quantize(summary["average_score"], quantization_decimals),
            "quantized_worst": _quantize(summary["worst_score"], quantization_decimals),
            "passed_case_count": int(summary["passed_case_count"]),
            "cumulative_seed_diff": record.get("cumulative_seed_diff"),
            "created_sequence": int(record.get("created_sequence") or 0),
            "candidate_id": cid,
        }
        entries.append((key, record, breakdown))
    entries.sort(key=lambda item: item[0])
    ranking = [
        {"candidate_id": breakdown["candidate_id"], "tie_key": breakdown}
        for _key, _record, breakdown in entries
    ]
    champion_id = ranking[0]["candidate_id"]
    return {
        "ok": True,
        "search_champion": champion_id,
        "champion_tie_key": ranking[0]["tie_key"],
        "ranking": ranking,
        "usable_count": len(usable),
        "epsilon": float(epsilon),
        "quantization_decimals": int(quantization_decimals),
        "algorithm_version": CHAMPION_ALGORITHM_VERSION,
        "note": "冠军从全池选取（不只前沿）；相对父 diff 只解释单次变异，并列规则用相对种子累计修改量",
    }


def _seed_diff_parts(cumulative_seed_diff: Any) -> tuple[float, float, float]:
    if isinstance(cumulative_seed_diff, dict):
        def _num(key: str) -> float:
            try:
                return float(cumulative_seed_diff.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return (_num("added_bytes"), _num("ops"), _num("generations"))
    if isinstance(cumulative_seed_diff, (int, float)):
        try:
            value = float(cumulative_seed_diff)
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)
        if math.isfinite(value):
            return (value, 0.0, 0.0)
    return (0.0, 0.0, 0.0)
