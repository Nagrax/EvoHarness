"""sidecar 元数据回归：批次信息必须完整可追溯。

真实缺陷（2026-10-29 实测）：sidecar 里 `startedAt` 恒为 null，
因为引用了该模块不存在的 `_utc_now()`——批次元数据缺启动时间，
追溯时无法确认批界（而它正是防止混批的关键字段之一）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_no_dead_utc_now_reference():
    source = (REPO / "eval" / "evolution_experiment.py").read_text(encoding="utf-8")
    assert "_utc_now() if" not in source, "不得引用本模块不存在的 _utc_now"


def test_started_at_uses_stdlib():
    source = (REPO / "eval" / "evolution_experiment.py").read_text(encoding="utf-8")
    assert '"startedAt": __import__("time").strftime' in source, (
        "startedAt 必须用标准库生成，不能是 None 占位")


def test_real_sidecar_has_core_fields():
    path = REPO / "eval" / "runs" / "evolution" / "batch-ws-w14d.json"
    if not path.is_file():
        return
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("batchTag", "workspace", "rounds", "holdout", "arms", "model"):
        assert data.get(key) is not None, f"sidecar 缺 {key}"
    assert data["batchTag"] == "ws-w14d"
    assert len(data["arms"]) == 3, "三臂必须齐全"


def test_tagged_checkpoint_naming_pattern():
    """新批次必须产出带 tag 的 checkpoint（文件名含 tag 才能隔离）。"""
    source = (REPO / "eval" / "evolution_experiment.py").read_text(encoding="utf-8")
    assert 'checkpoint-{batch_tag}-{arm}-r{rnd}.json' in source
