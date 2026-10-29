"""批次读取工具回归：按批次严格取结果，绝不混批。

真实事故背景：W14 前后两批写同名 checkpoint，覆盖后无法从文件分辨归属。
写侧已改带 tag（下批生效），但历史批次仍是旧命名 → 读侧必须支持
「tag 精确匹配」与「since 时间过滤」两种口径，且**拒绝无过滤条件的读取**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.batch_results import list_batches, load_batch


def test_refuses_to_load_without_scope():
    """不给 tag 也不给 since 必须拒绝——否则就是"读全部"，会混批。"""
    result = load_batch()
    assert result["ok"] is False
    assert result["reason"] == "need-tag-or-since"


def test_unknown_tag_returns_empty_not_all():
    result = load_batch(tag="no-such-batch-tag-xyz")
    assert result["ok"] is False
    assert "no-checkpoints-for-tag" in result["reason"]


def test_since_filter_excludes_old_batches():
    """用未来时间过滤必须拿不到任何结果（证明过滤真的生效）。"""
    result = load_batch(since="2099-01-01T00:00")
    assert result["ok"] is False
    assert "no-checkpoints-since" in result["reason"]


def test_since_filter_returns_only_that_batch():
    path = REPO / "eval" / "runs" / "evolution"
    if not any(path.glob("checkpoint-*-r*.json")):
        return
    result = load_batch(since="2026-10-29T01:30")
    if not result.get("ok"):
        return
    # 所有命中文件的 mtime 都必须晚于过滤点
    import datetime as dt
    cutoff = dt.datetime.fromisoformat("2026-10-29T01:30").timestamp()
    for arm_rows in result["detail"].values():
        for row in arm_rows:
            stamp = dt.datetime.fromisoformat(row["mtime"]).timestamp()
            assert stamp >= cutoff, f"{row['file']} 早于过滤点却被读到"


def test_list_batches_reports_naming_schemes():
    info = list_batches()
    assert "batches" in info
    assert "tagged" in info and "legacyFiles" in info
    if info["legacyFiles"]:
        legacy = [b for b in info["batches"] if b.get("naming") == "legacy"]
        assert legacy and "提示" not in legacy[0], "legacy 批次必须提示需要 since 过滤"
        assert "hint" in legacy[0]


def test_pass_rate_math():
    result = load_batch(since="2026-10-29T01:30")
    if not result.get("ok"):
        return
    for arm, summary in result["arms"].items():
        if summary["total"]:
            expected = round(summary["hits"] / summary["total"] * 100, 1)
            assert summary["passRate"] == expected, f"{arm} 通过率算错"
