"""ROI 批次标识回归：判读 ROI 时必须知道数据属于哪一批。

背景：curve.json 在旧代码下只是"最新指针"，新代码双写 tagged 归档。
没有批次标识时，把两批 ROI 混在一起讨论是很容易犯的错（2026-10-29 真实事故）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.roi import evolution_roi


def test_real_curve_reports_batch_tag():
    result = evolution_roi()
    if not result.get("ok"):
        return
    assert "batchTag" in result, "ROI 输出必须带批次标识"
    assert result["batchTag"], "批次标识不能为空"
    assert "source" in result, "必须同时给出源文件路径，便于核对"


def test_legacy_curve_marked_explicitly(tmp_path):
    """旧 curve（无 batchTag 字段）必须显式标 legacy-untagged，不能伪装成已知批次。"""
    curve = {
        "arms": ["no_evolution", "ungated_evolution"],
        "points": [
            {"arm": "no_evolution", "round": 1, "pass_at_1": 0.0, "n": 6, "tokens_M": 1.0},
            {"arm": "ungated_evolution", "round": 1, "pass_at_1": 16.7, "n": 6, "tokens_M": 1.0},
        ],
    }
    path = tmp_path / "curve.json"
    path.write_text(json.dumps(curve), encoding="utf-8")
    result = evolution_roi(path)
    assert result["ok"] is True
    assert result["batchTag"] == "legacy-untagged"


def test_tagged_curve_preserves_tag(tmp_path):
    curve = {
        "batchTag": "ws-w14c",
        "arms": ["no_evolution", "ungated_evolution"],
        "points": [
            {"arm": "no_evolution", "round": 1, "pass_at_1": 0.0, "n": 6, "tokens_M": 1.0},
            {"arm": "ungated_evolution", "round": 1, "pass_at_1": 16.7, "n": 6, "tokens_M": 1.0},
        ],
    }
    path = tmp_path / "curve-ws-w14c.json"
    path.write_text(json.dumps(curve), encoding="utf-8")
    result = evolution_roi(path)
    assert result["batchTag"] == "ws-w14c"


def test_missing_curve_returns_reason():
    result = evolution_roi(Path("D:/nonexistent/curve.json"))
    assert result["ok"] is False
    assert result["reason"] == "curve-missing"
