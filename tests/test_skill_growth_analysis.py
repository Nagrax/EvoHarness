"""技能增长 vs 增益 分析回归（2026-10-29）。

把 W14 的定性论断「增益不随技能累积增长」变成可复核数字：
- Spearman ρ（技能数 vs 命中率）
- 前 1/3 轮 vs 后 1/3 轮命中率
- 按技能数分箱

同时固定**可宣称的边界**：只能宣称"未观察到增长"，不能宣称"下降"（样本不足）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.skill_growth_vs_gain as mod


def test_script_runs_and_writes_report():
    proc = subprocess.run([sys.executable, "eval/skill_growth_vs_gain.py"],
                          cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    path = REPO / "eval" / "runs" / "skill-growth-vs-gain.json"
    if proc.returncode != 0:
        # 本机没拉取 eval/runs/evolution（约 50MB 基准产物，见部署指南 §6）时，
        # 脚本应显式报告 no-checkpoints 而非静默写空报告。
        assert "no-checkpoints" in (proc.stdout or ""), proc.stderr[-600:]
        assert not path.is_file() or json.loads(
            path.read_text(encoding="utf-8")).get("ok") is not False
        return
    assert path.is_file()


def test_report_fields_present():
    path = REPO / "eval" / "runs" / "skill-growth-vs-gain.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("points", "spearmanSkillsVsHitRate", "earlyThirdRate",
                "lateThirdRate", "deltaLateVsEarly", "verdict"):
        assert key in data, f"缺字段 {key}"
    assert data["points"] >= 10, f"观测点太少：{data['points']}"


def test_spearman_implementation_is_correct():
    """自实现 Spearman 需与已知结果一致（完美正/负相关、常数序列）。"""
    assert abs(mod.spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-9
    assert abs(mod.spearman([1, 2, 3, 4], [40, 30, 20, 10]) + 1.0) < 1e-9
    constant = mod.spearman([5, 5, 5, 5], [1, 2, 3, 4])
    assert constant != constant  # NaN


def test_load_points_excludes_baseline_arm():
    points = mod.load_points()
    if not points:
        return
    assert all(p["arm"] != "no_evolution" for p in points), (
        "基线臂不进化，技能数恒定，混进来会稀释相关性")


def test_verdict_does_not_claim_decline():
    """结论口径必须保守：只能支持"饱和/无增长"，不能写成"下降"。"""
    path = REPO / "eval" / "runs" / "skill-growth-vs-gain.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] in {"supports-saturation", "does-not-support-saturation"}
    note = (data.get("note") or "") + json.dumps(data, ensure_ascii=False)
    assert "下降" not in note, "不得把弱负相关写成'下降'（样本不足）"