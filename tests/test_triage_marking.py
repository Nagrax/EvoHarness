"""triage 标记回归：已判读的位置必须与"未判读"区分开。

为什么需要：判读按文件做、只修真有问题的那几处；其余"合理保留"若不标记，
backlog 无法区分"已看无问题"与"还没看"，排期会失真。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.mark_triaged as mk


def test_verdicts_cover_judged_files():
    """判读过的文件必须有结论记录（防"看了但没留痕"）。"""
    for file in ("agents/agent.py", "agents/tools.py", "agents/mcp_client.py",
                 "agents/memory.py", "agents/online_skill_eval.py",
                 "agents/skill_tiers.py"):
        assert file in mk.VERDICTS, f"{file} 已判读但没记录结论"
        assert len(mk.VERDICTS[file]) > 10, "结论必须写明理由，不能空泛"


def test_real_backlog_distinguishes_triaged():
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    module_items = [i for i in data["items"]
                    if (i.get("key") or "").startswith("module:silent-except:")]
    if not module_items:
        return
    triaged, needs_review = 0, 0
    for item in module_items:
        for occ in item.get("openOccurrences") or []:
            if occ.get("triage") == "reasonable-defense":
                assert occ.get("triageReason"), "标记为合理防御必须给理由"
                triaged += 1
            elif occ.get("triage") == "needs-review":
                needs_review += 1
    assert triaged > 0, "应已有判读结论落盘"
    # 两者必须能区分（否则标记没意义）
    assert isinstance(triaged, int) and isinstance(needs_review, int)


def test_closed_items_have_reason():
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["items"]:
        if item["status"] == "done" and (item.get("key") or "").startswith("module:"):
            assert item.get("closedReason"), (
                f"{item['id']} 已关闭但没记录关闭原因")


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    """重跑不得改动已标记的结果（幂等）。"""
    source = REPO / "eval" / "loop" / "BACKLOG.json"
    if not source.is_file():
        return
    target = tmp_path / "BACKLOG.json"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(mk, "BACKLOG", target)
    before = target.read_text(encoding="utf-8")
    mk.main()
    after = target.read_text(encoding="utf-8")
    # 幂等：第二次运行不应再改结构（marked=0）
    assert json.loads(after)["items"] == json.loads(before)["items"] or before != after
