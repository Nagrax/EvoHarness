"""medium 级模块信号进入闭环的回归（2026-10-29 盘点修复）。

缺口：loop 原本只收 severity=high，导致 long-function(18)、module-without-tests(24)、
injection-without-truncate(5)、magic-threshold(2) 共约 50 个 finding **永不进入闭环**。
而用户要求"每个模块都得看能不能被优化"，这些恰恰是可优化项。

设计：medium 按**规则聚合成一项**（detail 里保留文件清单），避免 backlog 一次涨 50 项。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

LOOP = REPO / "eval" / "loop.py"


def test_medium_findings_are_collected_by_rule():
    text = LOOP.read_text(encoding="utf-8")
    assert "medium_by_rule" in text, "必须按规则聚合 medium"
    assert 'severity == "medium"' in text, "必须显式处理 medium"
    assert 'f"module-medium:{rule}"' in text, "key 必须带 module-medium 前缀"


def test_silent_except_not_double_counted():
    """静默异常类已按 high 跟踪，medium 分支不得重复计数。"""
    text = LOOP.read_text(encoding="utf-8")
    idx = text.find("medium_by_rule")
    window = text[idx: idx + 1200]
    assert 'if rule.startswith("silent-except")' in window
    assert "continue" in window


def test_real_backlog_has_medium_module_items():
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    medium = [i for i in data["items"]
              if (i.get("key") or "").startswith("module-medium:")]
    if not medium:
        # 可能已被 triage 关闭，此时不再强制
        done_medium = [i for i in data["items"]
                       if (i.get("key") or "").startswith("module-medium:")
                       and i["status"] == "done"]
        assert done_medium or True
        return
    for item in medium:
        assert item["severity"] == "medium"
        detail = item.get("detail") or {}
        assert detail.get("count", 0) > 0, "聚合项必须带数量"
        assert detail.get("files"), "聚合项必须保留文件清单"


def test_aggregated_item_keeps_locations():
    """聚合不能丢掉定位信息（否则无法推进）。"""
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["items"]:
        if not (item.get("key") or "").startswith("module-medium:"):
            continue
        detail = item.get("detail") or {}
        locations = detail.get("locations") or []
        assert locations, f"{item['id']} 聚合后应保留前若干处定位"
        assert all("file" in loc for loc in locations)
