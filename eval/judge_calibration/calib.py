"""M3 judge 可信度校准：无人工近似（no-human approximation）的一致性纯函数。

三个函数全部为确定性纯函数、仅依赖 stdlib、模块级零 I/O、零模型调用：
- cohen_kappa(pairs)        手算 Cohen's kappa（judge vs 参考判定的成对一致度，扣除机遇一致）
- swap_consistency(a, b)    同批样本「原序 vs 交换序」两次判定的一致率 / 翻转率
- length_controlled_diff()  按输出长度分层校正后的 before/after 通过率与差值

口径声明（与 golden_pairs.example.json 一致）：
「参考判定」是 golden/程序化近似而非人工标注。这里的一致性只衡量 judge 与
可复现基线（规则/合成）之间的稳定度，不宣称等价于人类评审的 ground truth。

生产接线（可选、默认关闭）：
本模块是**可选离线工具**，默认不参与任何评测流程、不改变既有 report/口径。
如需在评测 report 中体现 judge 校准，必须显式 opt-in：
    python -m eval.run_benchmark score --dataset gaia \
        --judge-calibration eval/judge_calibration/golden_pairs.example.json
此时 eval.run_benchmark.judge_calibration() 会读取成对判定并写入 summary 的
"judge_calibration" 字段；不传该参数则不写该字段（默认行为不变）。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

__all__ = ["cohen_kappa", "swap_consistency", "length_controlled_diff"]


def _mean(values: Sequence[bool]) -> float:
    """bool 序列的均值（0.0 表示空序列）。"""
    if not values:
        return 0.0
    return sum(1 for v in values if v) / len(values)


def cohen_kappa(pairs: Iterable[Tuple[object, object]]) -> float:
    """Cohen's kappa：pairs=[(label_a, label_b), ...] 的成对一致度。

    kappa = (po - pe) / (1 - pe)，po 为观察一致率，pe 为机遇期一致率
    （两侧边际分布逐标签相乘求和）。

    边界（确定性约定）：
    - 空输入                     -> 0.0
    - 完全一致且非退化           -> 1.0
    - pe == 1（期望一致率为 1，退化；含全同标签）-> 0.0，避免除零。
      此时 kappa 数学上为 0/0，本函数冻结为 0.0（不返回 NaN/1.0）。
    """
    seq: List[Tuple[object, object]] = list(pairs)
    n = len(seq)
    if n == 0:
        return 0.0

    agree = 0
    count_a: Dict[object, int] = {}
    count_b: Dict[object, int] = {}
    for a, b in seq:
        if a == b:
            agree += 1
        count_a[a] = count_a.get(a, 0) + 1
        count_b[b] = count_b.get(b, 0) + 1

    po = agree / n
    pe = 0.0
    for label, ca in count_a.items():
        pe += (ca / n) * (count_b.get(label, 0) / n)

    denom = 1.0 - pe
    if denom <= 0.0:
        return 0.0
    return (po - pe) / denom


def swap_consistency(
    ordered_a: Sequence[object], ordered_b: Sequence[object]
) -> Dict[str, float]:
    """同批样本「原序 vs 交换序」两次判定的一致性。

    ordered_a / ordered_b 为同一批样本在两种呈现顺序下被判定的逐样本判定序列，
    调用前已按样本的规范顺序回对齐（index i ↔ 同一份样本 i），因此逐位比较即为
    逐样本比较。

    返回 {"agreement": 一致率, "flip_rate": 翻转率}：
    - agreement = 判定相同的位置数 / 参与比较的位置数
    - flip_rate = 判定改变的位置数 / 参与比较的位置数（二值判定下 = 1 - agreement）
    空输入或无可比位置 -> {"agreement": 0.0, "flip_rate": 0.0}。
    长度不等时按可配对的最小长度比较，不补位、不报错。
    """
    agreed = 0
    flipped = 0
    compared = 0
    for a, b in zip(ordered_a, ordered_b):
        compared += 1
        if a == b:
            agreed += 1
        else:
            flipped += 1

    if compared == 0:
        return {"agreement": 0.0, "flip_rate": 0.0}
    return {"agreement": agreed / compared, "flip_rate": flipped / compared}


def length_controlled_diff(records: Iterable[Dict[str, object]]) -> Dict[str, float]:
    """按输出长度分层校正的 before/after 通过率与差值。

    records=[{"passed": bool, "length": int, "group": "before"|"after"}, ...]。

    长度是混杂因子：after 组输出普遍更长时，原始通过率差值会把长度效应算进去。
    本函数用「精确长度分层 + 层内配对」消除该混杂：
    - 仅在同时含 before 与 after 样本的长度层内比较；
    - 层权重 = 该层 before 样本数 + after 样本数；
    - 校正后通过率 = Σ(层权重 × 层内通过率) / Σ层权重。

    层权重取舍（记录，不改变现有数值）：亦可取 min(n_before, n_after) 以抑制「大样本层
    主导」偏差，但会改变与既有分层回归断言绑定的口径（例如 exposed_confound 用例在总数
    权重下为 before 0.75 / after 0.5，换 min 权重后两率相等、diff 变为 0），因此这里保留
    总数权重并显式记录该取舍，避免静默改动已固化口径。

    返回：
    - before_rate / after_rate：校正（长度分层）后的两组通过率
    - diff：校正后 after_rate - before_rate（本函数的主输出）
    - raw_diff：未校正的 after - before（对照，暴露长度混杂量）
    - matched_strata：参与校正的共有长度层数
    无共有长度层（或无输入）时，校正值退化为对应原始通过率，diff == raw_diff。
    """
    strata: Dict[object, Dict[str, List[bool]]] = {}
    for rec in records:
        group = rec.get("group")
        if group not in ("before", "after"):
            continue
        bucket = strata.setdefault(rec.get("length"), {"before": [], "after": []})
        bucket[group].append(bool(rec.get("passed")))

    # 原始通过率按全样本（不分层）统计
    all_before: List[bool] = []
    all_after: List[bool] = []
    for bucket in strata.values():
        all_before.extend(bucket["before"])
        all_after.extend(bucket["after"])
    raw_before = _mean(all_before)
    raw_after = _mean(all_after)

    weight_sum = 0
    weighted_before = 0.0
    weighted_after = 0.0
    matched_strata = 0
    for bucket in strata.values():
        if not bucket["before"] or not bucket["after"]:
            continue
        matched_strata += 1
        weight = len(bucket["before"]) + len(bucket["after"])
        weight_sum += weight
        weighted_before += weight * _mean(bucket["before"])
        weighted_after += weight * _mean(bucket["after"])

    if weight_sum == 0:
        before_rate = raw_before
        after_rate = raw_after
    else:
        before_rate = weighted_before / weight_sum
        after_rate = weighted_after / weight_sum

    return {
        "before_rate": before_rate,
        "after_rate": after_rate,
        "diff": after_rate - before_rate,
        "raw_diff": raw_after - raw_before,
        "matched_strata": matched_strata,
    }
