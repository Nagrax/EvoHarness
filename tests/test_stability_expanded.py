"""扩样本后的两个回归：跨批次结论反转 + title 必须刷新。

真实教训：
1. 3 题批次给出 stable=1/flaky=2，10 题批次给出 stable=8/flaky=1/disappeared=1
   → **小样本结论会反转**，比例类结论必须够样本
2. 已闭合项的 title 若不刷新，会一直显示旧批次的数字（真实踩过）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load(name: str) -> dict | None:
    path = REPO / "eval" / "runs" / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def test_small_batch_and_large_batch_differ():
    """两批结论必须如实并存（不得用新批覆盖旧批的观测）。"""
    small = _load("failure-stability-1.json")
    large = _load("failure-stability-2.json")
    if not (small and large):
        return
    assert small["tasks"] < large["tasks"], "第二批样本应更大"
    assert small["statusCounts"] != large["statusCounts"], (
        "小样本与大样本结论不同——这正是必须扩样本的证据")
    # 大样本里稳定占比应显著（本批 8/10）
    assert large["statusCounts"]["stable"] >= 5, (
        f"10 题样本应显示多数稳定失败，实际 {large['statusCounts']}")


def test_large_batch_has_source_marker():
    """必须记录题源，否则两批不可比时会被误合并。"""
    large = _load("failure-stability-2.json")
    if not large:
        return
    assert large.get("source") == "full"


def test_loop_prefers_latest_artifact(tmp_path, monkeypatch):
    """必须读最新一批稳定性产物。

    2026-10-29：collect 拆分时把"取最新产物"抽成了 `_latest_stability_artifact()`，
    原断言绑定的是具体变量名（`_stability_files[-1]`）→ 假失败。
    改为**行为断言**：真实造两份产物，验证选中的是排序后最后一份。
    这比检查源码文本更强——文本对了但行为错也能过。
    """
    import json as _json

    import eval.loop as loop_mod

    for name in ("failure-stability-1.json", "failure-stability-2.json"):
        (tmp_path / name).write_text(_json.dumps({"statusCounts": {}}), encoding="utf-8")
    monkeypatch.setattr(loop_mod, "RUNS", tmp_path)
    assert loop_mod._latest_stability_artifact().name == "failure-stability-2.json"

    # 且 helper 的实现确实按名字排序取最后一份（防止换成"取第一个"这类退化）
    source = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert 'sorted(RUNS.glob("failure-stability-*.json"))' in source
    assert "files[-1]" in source, "必须取排序后最后一个"


def test_title_refreshes_on_recollect():
    """同 key 再次出现时 title 必须刷新（否则旧数字常驻）。"""
    source = (REPO / "eval" / "loop.py").read_text(encoding="utf-8")
    assert 'existing["title"] = finding["title"]' in source


def test_backlog_title_matches_latest_batch():
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    large = _load("failure-stability-2.json")
    if not (path.is_file() and large):
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    items = [i for i in data["items"] if i.get("source") == "failureStability"]
    if not items:
        return
    counts = large["statusCounts"]
    expected = f"{counts['stable']} 稳定 / {counts['flaky']} 偶发 / {counts['disappeared']} 未复现"
    assert expected in items[0]["title"], (
        f"backlog title 必须反映最新批次，期望含 {expected!r}，实际 {items[0]['title']!r}")
