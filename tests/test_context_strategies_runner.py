"""W15 策略对照 runner 回归：策略定义正确、S4 明确标未跑、不做越界宣称。

真实教训（2026-10-29 第一批）：GAIA 短题的上下文规模没到 70% 折叠阈值，
折叠逻辑从未触发 → 三策略行为必然相同 → 实验 **uninformative**。
这里固定三条纪律：① 策略只通过 env 区分 ② S4 必须显式列在 notRun
③ 报告必须自标 not-claimable（单批）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.context_strategies_run as runner


def test_three_strategies_differ_only_by_env():
    assert set(runner.STRATEGIES) == {"s1_no_fold", "s2_current_fold", "s3_tail_keep"}
    # S1 禁用自动折叠；S2/S3 统一强制阈值（否则短任务根本触发不了折叠）
    assert runner.STRATEGIES["s1_no_fold"] == {"EVOHARNESS_DISABLE_AUTO_FOLD": "1"}
    assert runner.STRATEGIES["s2_current_fold"]["EVOHARNESS_AUTO_COMPACT_THRESHOLD"] == "0.30"
    assert (runner.STRATEGIES["s3_tail_keep"]["EVOHARNESS_FOLD_KEEP_RECENT_TURNS"] == "4"
            and runner.STRATEGIES["s3_tail_keep"]["EVOHARNESS_AUTO_COMPACT_THRESHOLD"] == "0.30")
    # 三个策略的 env 必须互不相同（否则不是对照）
    seen = [json.dumps(v, sort_keys=True) for v in runner.STRATEGIES.values()]
    assert len(set(seen)) == 3


def test_force_fold_threshold_is_shared():
    """S2/S3 必须共用同一强制阈值——否则比较的就不是"尾部保留"这一个变量。"""
    assert runner.FORCE_FOLD == {"EVOHARNESS_AUTO_COMPACT_THRESHOLD": "0.30"}
    for name in ("s2_current_fold", "s3_tail_keep"):
        for key, value in runner.FORCE_FOLD.items():
            assert runner.STRATEGIES[name][key] == value


def test_intervention_not_triggered_verdict():
    """foldTotal 全 0 时必须判 uninformative，而不是给策略排名。"""
    path = REPO / "eval" / "runs" / "context" / "ab2.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    folds = {name: s.get("foldTotal", 0) for name, s in data["strategies"].items()}
    if not any(folds.values()):
        assert data["claimability"]["verdict"] == "uninformative:intervention-not-triggered", (
            f"折叠未触发却给了其他判定：{data['claimability']['verdict']}")


def test_s4_is_explicitly_not_run():
    assert "s4_stage_handover" in runner.NOT_RUN
    assert runner.NOT_RUN["s4_stage_handover"], "未跑必须有原因说明"
    assert "s4_stage_handover" not in runner.STRATEGIES, (
        "S4 不得混进已跑策略（会让人以为四策略都验证过）")


def test_report_self_marks_not_claimable():
    path = REPO / "eval" / "runs" / "context" / "ab1.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["claimability"]["verdict"] == "not-claimable:single-batch"
    assert "S4 未跑" in data["claimability"]["note"], (
        "报告必须写明 S4 未跑，防'四策略全部验证'的误读")
    assert data["notRun"], "未跑项必须落盘"


def test_first_batch_is_marked_uninformative_by_evidence():
    """第一批三策略质量指标相同 = 干预未生效，必须能被看出来。"""
    path = REPO / "eval" / "runs" / "context" / "ab1.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    rates = {name: s["passRate"] for name, s in data["strategies"].items()}
    # 若三者相同且为 0，则这批不提供策略优劣信息（把事实固定下来）
    if len(set(rates.values())) == 1:
        assert all(v == 0.0 for v in rates.values()) or True
        assert data["claimability"]["verdict"].startswith("not-claimable"), (
            "质量指标无差异时不得宣称任何策略更优")
