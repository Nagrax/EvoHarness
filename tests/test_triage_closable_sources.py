"""triage 可闭合 source 回归（2026-10-29 真实事件）。

事件：`/api/skills` 的泄漏检查误报建了 `backend:` high 工单；修掉误报后
契约 21/21 通过，但工单**永远挂 pending**——triage 的可闭合 source 白名单
只有 `{harness2, variance, calibration, lifecycleNoiseGate, failureStability}`，
漏了 `backend` / `frontend`。

这类"单次检测型"信号的语义是：**没有 occurrence 概念，检测通过即问题消失**。
漏掉它们 → 修好了工单还在 → 排期永远脏。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

LOOP = REPO / "eval" / "loop.py"


def test_triage_closable_sources_include_single_shot_kinds():
    src = LOOP.read_text(encoding="utf-8")
    idx = src.find("def cmd_triage")
    assert idx > 0
    body = src[idx: idx + 4000]
    for source in ("backend", "frontend", "variance", "calibration",
                   "lifecycleNoiseGate", "failureStability"):
        assert f'"{source}"' in body, f"triage 可闭合来源漏了 {source}"


def test_real_backlog_has_no_stale_backend_pending():
    """真实 backlog：修好的 backend/frontend 信号不应仍是 pending。"""
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    stale = [i["id"] for i in data["items"]
             if i.get("status") != "done"
             and str(i.get("source") or "") in {"backend", "frontend"}]
    assert not stale, f"这些单次检测型工单修好后仍挂 pending：{stale}"


def test_contract_passes_so_signal_is_gone():
    """契约产物必须是通过状态（否则上面的断言前提不成立）。"""
    path = REPO / "eval" / "runs" / "backend-contract.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ok"] is True, f"契约仍未通过：{data['failed']} 个失败"