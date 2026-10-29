"""运行间方差回归：同配置重复的极差必须被显式计量。

真事故（本轮）：`only-prefetch-off` 同一配置、同题池、同规模，两次运行
一次 pass=50.0、一次 pass=0.0。若不估方差，就会把"这次跑出的差异"
当成"配置差异"——对照实验最经典的陷阱。

`heavy-wrapped` 基线重复两次的极差实测为 **50pp**，直接推翻了此前所有
"Δpass=-50"的结论：它们全都落在噪声范围内。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import eval.variance_check as vc


def _write_history(root: Path, runs: list[dict]) -> None:
    hist = root / "history"
    hist.mkdir(parents=True, exist_ok=True)
    for index, spec in enumerate(runs):
        payload = {
            "scale": {"rounds": 2, "tasks": 1, "holdout": 2, "taskIdsFile": "ids.json"},
            "results": [{
                "design": spec["design"],
                "env": spec.get("env") or {},
                "totalTokensM": spec.get("tokensM", 0.4),
                "lastPoint": {"pass_at_1": spec["pass"], "tokens_M": spec.get("tokensM", 0.2)},
            }],
        }
        (hist / f"run-{index:04d}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_same_config_variance_is_detected(tmp_path, monkeypatch):
    """同配置两次结果不同 → 必须被识别为不稳定。"""
    _write_history(tmp_path, [
        {"design": "heavy-wrapped", "pass": 0.0},
        {"design": "heavy-wrapped", "pass": 50.0},
    ])
    monkeypatch.setattr(vc, "HIST", tmp_path / "history")
    rows = vc.collect_history()
    result = vc.analyze(rows, None)
    assert result["ok"] is True
    assert result["maxObservedRangePts"] == 50.0
    unstable = result["unstableSameScale"]
    assert any(u["design"] == "heavy-wrapped" and u["range"] == 50.0 for u in unstable)
    assert "50.0pp" in result["implication"]


def test_stable_config_reports_zero_range(tmp_path, monkeypatch):
    _write_history(tmp_path, [
        {"design": "plugin", "pass": 50.0},
        {"design": "plugin", "pass": 50.0},
    ])
    monkeypatch.setattr(vc, "HIST", tmp_path / "history")
    result = vc.analyze(vc.collect_history(), None)
    plugin = next(d for d in result["designs"] if d["design"] == "plugin")
    assert plugin["passAt1Range"] == 0.0
    assert plugin["comparableWithinSameScale"] is True


def test_no_history_is_reported_not_faked(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "HIST", tmp_path / "absent")
    result = vc.analyze(vc.collect_history(), None)
    assert result["ok"] is False
    assert result["reason"] == "no-history"


def test_rule_requires_claim_above_noise():
    """结论必须强于噪声：规则文本要明确写出这条。"""
    rules = vc.analyze([{"design": "x", "passAt1": 10.0, "tokensM": 1.0,
                         "rounds": 2, "tasks": 1, "holdout": 2,
                         "taskIdsFile": "ids.json", "env": {}, "file": "f"}], None)
    text = json.dumps(rules, ensure_ascii=False)
    assert "必须显著大于同配置重复运行的极差" in text
    assert "n<30 时一律 not-claimable" in text


def test_arch_run_archives_every_run():
    """arch_run 必须归档每次运行，否则方差分析无从谈起。"""
    import inspect

    import eval.arch_run as arch_run

    source = inspect.getsource(arch_run.main)
    assert "history" in source, "运行历史必须落盘到 history/"
    assert "stamp" in source
