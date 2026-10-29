"""rebuild_occurrences 的 locatedLine 刷新回归（2026-10-29 抓到的两个真缺陷）。

1. 原实现把"指纹未变"当"无变化"→ 不写回 → 代码移动后 locatedLine 仍指旧行
2. 修 1 时又用 (file, line) 作 key 比较旧值，但 line 本身会漂移 → key 匹配不上
   → 被当成"没变化" → 仍然不刷新

正确语义：locatedLine / staleLocation 是派生值，每次按当前文件重算；
结构是否变化用**指纹集合**判断（指纹稳定，行号不稳定）。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.rebuild_occurrences as rebuild_mod


@pytest.fixture()
def temp_backlog(tmp_path, monkeypatch):
    source = REPO / "eval" / "loop" / "BACKLOG.json"
    if not source.is_file():
        pytest.skip("BACKLOG 不存在")
    target = tmp_path / "BACKLOG.json"
    shutil.copyfile(source, target)
    monkeypatch.setattr(rebuild_mod, "BACKLOG", target)
    return target


def test_located_line_refreshes_when_stale(temp_backlog):
    """把 locatedLine 人为改错，重建必须纠正它。"""
    data = json.loads(temp_backlog.read_text(encoding="utf-8"))
    touched = 0
    for item in data["items"]:
        for occ in item.get("openOccurrences") or []:
            if occ.get("fingerprint") and occ.get("locatedLine"):
                occ["locatedLine"] = 1  # 明显的错误值
                touched += 1
                break
        if touched:
            break
    assert touched, "样本里应有可定位的 occurrence"
    temp_backlog.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")

    result = rebuild_mod.rebuild(apply=True)
    assert result["itemsChanged"] >= 1, "locatedLine 变错必须被检出并修正"
    after = json.loads(temp_backlog.read_text(encoding="utf-8"))
    bad = [o for i in after["items"] for o in (i.get("openOccurrences") or [])
           if o.get("fingerprint") and o.get("locatedLine") == 1]
    assert not bad, "错误 locatedLine 必须被纠正"


def test_structural_change_detected_by_fingerprint(temp_backlog):
    """指纹集合变化必须被检出（结构变化）。"""
    data = json.loads(temp_backlog.read_text(encoding="utf-8"))
    for item in data["items"]:
        occs = item.get("openOccurrences") or []
        if occs and occs[0].get("fingerprint"):
            occs[0]["fingerprint"] = "deadbeefcafe"
            break
    temp_backlog.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    result = rebuild_mod.rebuild(apply=True)
    assert result["itemsChanged"] >= 1


def test_source_uses_fingerprint_as_key():
    text = (REPO / "eval" / "rebuild_occurrences.py").read_text(encoding="utf-8")
    assert "(f, fp) in old_lines" in text, "必须用指纹作 key（line 会漂移）"
    assert "locatedLine/staleLocation 是派生值" in text, "必须写明这是派生值"
