"""E-02 回归：冻结评测协议（分母政策 / 平台不可用分类 / bootstrap CI / run manifest）。

历史问题：README(HLE 20.2)、eval/RESULTS.md(9.3)、当前判分器三套口径并存；评分只统计
「可判分」记录，把系统未完成任务的代价（超时/执行错误/无最终答案）隐藏出分母。本文件
把协议钉死为可离线复算的纯函数：

- 分类优先级 platform_unavailable → timeout → error → no_answer → correct/incorrect；
- 主指标 pass_at_1 = correct / (planned - platform_unavailable)（失败计入）；
- 有效作答准确率 = correct / scorable（单列，不与主指标混用）；
- bootstrap 95% CI 固定种子可复算（零模型）；
- run manifest 绑定代码版本/工作树差异/模型/权限/数据哈希/题单/预算/判分器版本。

全部纯函数 / 临时文件用例，零模型、零网络。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval.run_benchmark as rb  # noqa: E402
from eval.run_benchmark import (  # noqa: E402
    JUDGE_VERSION,
    bootstrap_ci95,
    build_run_manifest,
    classify_record,
    summarize_records,
)


def _rec(prediction, gold, **over):
    base = {
        "prediction": prediction,
        "gold": gold,
        "folds": 0,
        "tokens": {"input": 10, "output": 5},
        "has_final_marker": True,
        "timed_out": False,
        "error": "",
        "problem_type": "p",
        "level": 1,
        "answer_type": "",
    }
    base.update(over)
    return base


# ── 分类：平台不可用单列，其余失败计入主指标分母 ─────────────────────────────

def test_classify_platform_unavailable_is_separate():
    r = _rec("", "", has_final_marker=False, error="403 team_model_access_denied")
    assert classify_record(r, "gaia") == "platform_unavailable"
    r2 = _rec("", "", error="rate limit exceeded")
    assert classify_record(r2, "hle") == "platform_unavailable"


def test_classify_failure_priority():
    # 平台不可用优先于超时；超时优先于普通错误；无标记次之；最后才是对错。
    assert classify_record(_rec("x", "y", timed_out=True, error="quota exceeded"), "gaia") == "platform_unavailable"
    assert classify_record(_rec("x", "y", timed_out=True, error="boom"), "gaia") == "timeout"
    assert classify_record(_rec("x", "y", error="boom"), "gaia") == "error"
    assert classify_record(_rec("x", "y", has_final_marker=False), "gaia") == "no_answer"
    assert classify_record(_rec("cat", "cat"), "gaia") == "correct"
    assert classify_record(_rec("dog", "cat"), "gaia") == "incorrect"


def test_summarize_main_metric_counts_failures_and_excludes_platform():
    records = [
        _rec("cat", "cat"),                                     # correct
        _rec("dog", "cat"),                                     # incorrect
        _rec("cat", "cat", timed_out=True),                     # timeout（失败）
        _rec("cat", "cat", has_final_marker=False),             # no_answer（失败）
        _rec("cat", "cat", error="service unavailable"),        # platform_unavailable（单列）
    ]
    s = summarize_records(records, "gaia", planned=5)
    assert s["planned"] == 5
    assert s["platform_unavailable"] == 1
    # 分母剔除平台不可用：5 - 1 = 4 → 1 正确 / 4 = 25%
    assert s["effective_denominator"] == 4
    assert s["pass_at_1"] == 25.0
    assert s["scorable"] == 2
    assert s["valid_answer_accuracy"] == 50.0
    assert s["timed_out"] == 1 and s["no_answer"] == 1 and s["errored"] == 0


def test_summarize_planned_uses_planned_not_attempted():
    # 计划 10 题，只跑了 2 题（都正确）→ 主指标按计划分母 20%，而非 100%。
    s = summarize_records([_rec("cat", "cat"), _rec("cat", "cat")], "gaia", planned=10)
    assert s["attempted"] == 2
    assert s["planned"] == 10
    assert s["pass_at_1"] == 20.0
    assert s["valid_answer_accuracy"] == 100.0


# ── bootstrap CI：固定种子可复算、区间单调 ──────────────────────────────────

def test_bootstrap_ci_is_deterministic_and_ordered():
    flags = [True] * 60 + [False] * 40
    a = bootstrap_ci95(flags, seed=42, iters=500)
    b = bootstrap_ci95(flags, seed=42, iters=500)
    assert a == b  # 同种子复算一致
    assert a[0] <= 60.0 <= a[1]
    assert bootstrap_ci95([], seed=42) is None
    # 全对 → 区间退化为单点 100。
    assert bootstrap_ci95([True] * 10, seed=1, iters=200) == [100.0, 100.0]


# ── run manifest：冻结字段齐备且可复算 ──────────────────────────────────────

def test_build_run_manifest_has_frozen_fields(tmp_path):
    items = [{"task_id": "a"}, {"task_id": "b"}]
    m1 = build_run_manifest(
        run_id="RID", dataset="gaia", tag="gaia", items=items, limit=None, workers=2,
        max_turns=40, timeout_s=900, no_fold=False, fold_threshold=None,
        output_path=tmp_path / "predictions.jsonl",
    )
    assert m1["run_id"] == "RID"
    assert m1["dataset"] == "gaia"
    assert m1["judge_version"] == JUDGE_VERSION
    assert m1["budget"]["planned"] == 2
    assert "git_rev" in m1["code"] and "worktree_diff_sha256_16" in m1["code"]
    # 真实仓库必须解析出 HEAD 与差异哈希——Windows 默认 cp936 下不得静默退化为 unknown。
    assert m1["code"]["git_rev"] not in ("", "unknown")
    assert m1["code"]["worktree_diff_sha256_16"] != "unknown"
    assert m1["data"]["question_ids_sha256_16"]
    # 题单哈希可复算：同集合不同顺序 → 同哈希。
    m2 = build_run_manifest(
        run_id="RID2", dataset="gaia", tag="gaia", items=list(reversed(items)), limit=None,
        workers=2, max_turns=40, timeout_s=900, no_fold=False, fold_threshold=None,
        output_path=tmp_path / "predictions.jsonl",
    )
    assert m2["data"]["question_ids_sha256_16"] == m1["data"]["question_ids_sha256_16"]


def test_write_immutable_never_overwrites(tmp_path):
    p = tmp_path / "scores" / "run.json"
    name1 = rb._write_immutable(p, {"v": 1})
    name2 = rb._write_immutable(p, {"v": 2})
    assert name1 != name2
    assert json.loads((tmp_path / "scores" / name1).read_text(encoding="utf-8"))["v"] == 1
    assert json.loads((tmp_path / "scores" / name2).read_text(encoding="utf-8"))["v"] == 2


def test_provenance_score_run_id_matches_actual_file(tmp_path, monkeypatch):
    """score_run_id 必须与实际不可变产物文件名一致（同名冲突时不得指向旧文件）。"""
    monkeypatch.setattr(rb, "RUNS_DIR", tmp_path)
    tag_dir = tmp_path / "gaia"
    tag_dir.mkdir()
    (tag_dir / "predictions.jsonl").write_text(
        json.dumps({
            "prediction": "cat", "gold": "cat", "tokens": {"input": 1, "output": 1},
            "has_final_marker": True, "timed_out": False, "error": "",
            "problem_type": "p", "answer_type": "",
        }) + "\n",
        encoding="utf-8",
    )
    scores = tag_dir / "scores"
    scores.mkdir()
    (scores / "FIXEDID.json").write_text("{}", encoding="utf-8")  # 预置同名冲突
    monkeypatch.setattr(rb, "new_run_id", lambda: "FIXEDID")

    rb.score("gaia")
    summary = json.loads((tag_dir / "summary.json").read_text(encoding="utf-8"))
    sid = summary["provenance"]["score_run_id"]
    assert sid == "FIXEDID.1"
    assert (scores / f"{sid}.json").is_file()
    assert json.loads((scores / f"{sid}.json").read_text(encoding="utf-8"))["pass_at_1"] == summary["pass_at_1"]
