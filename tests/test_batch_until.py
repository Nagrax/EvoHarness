"""--until 上界回归：只靠 since 无法隔离相邻两批。

真实需求：W14 前半批（10-26 夜间）与后半批（10-29 凌晨）在同一目录，
只用 since 会把两批一起读进来 → 又回到混批。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.batch_results import load_batch


def test_until_excludes_later_batch():
    """区间完全落在未来 → 必须为空（证明上界真的生效，不是只过滤下界）。"""
    result = load_batch(since="2099-01-01T00:00", until="2099-12-31T00:00")
    assert result["ok"] is False
    assert "no-checkpoints-since" in result["reason"]


def test_until_window_that_contains_nothing_returns_empty():
    """把上界设在启动前 → 空。"""
    result = load_batch(since="2026-10-29T01:30", until="2026-10-29T01:31")
    assert result["ok"] is False


def test_window_returns_only_files_inside_bounds():
    import datetime as dt

    since = "2026-10-29T01:30"
    until = "2026-10-29T04:00"
    result = load_batch(since=since, until=until)
    if not result.get("ok"):
        return
    lower = dt.datetime.fromisoformat(since).timestamp()
    upper = dt.datetime.fromisoformat(until).timestamp()
    for arm_rows in result["detail"].values():
        for row in arm_rows:
            stamp = dt.datetime.fromisoformat(row["mtime"]).timestamp()
            assert lower <= stamp < upper, (
                f"{row['file']} 的 mtime={row['mtime']} 越界（[{since}, {until})）")


def test_result_echoes_bounds_for_traceability():
    """返回里必须回显 since/until，判读者能核对范围（不留模糊）。"""
    result = load_batch(since="2026-10-29T01:30", until="2026-10-29T04:00")
    if not result.get("ok"):
        return
    assert result.get("since") == "2026-10-29T01:30"
    assert result.get("until") == "2026-10-29T04:00"


def test_until_without_since_is_rejected():
    """只给上界等于"从头读到 until"，仍是无限范围 → 拒绝。"""
    result = load_batch(until="2026-10-29T04:00")
    assert result["ok"] is False
    assert result["reason"] == "need-tag-or-since"
