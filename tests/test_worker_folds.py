"""worker 输出 folds 字段回归（2026-10-29 自纠）。

背景：W15 需要 `folds` 判断折叠干预是否真的发生，而 worker 原本不输出该字段，
导致 `record.get("folds")` 恒为 0 → 误判"干预未触发"。
修复时还踩了作用域坑：`agent` 是 `_work()` 的局部变量，直接在 record 里
`getattr(agent, ...)` 会 NameError → worker 静默崩溃、无输出文件。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SRC = REPO / "eval" / "evolution_experiment.py"


def test_worker_records_folds_via_holder():
    """folds 必须经 holder 从 _work() 传出，不能在 record 里直接引用局部 agent。"""
    text = SRC.read_text(encoding="utf-8")
    assert 'holder["folds"] = int(getattr(agent, "_fold_count", 0))' in text, (
        "必须在 _work() 内部把 folds 写进 holder")
    assert '"folds": int(holder.get("folds") or 0)' in text, (
        "record 必须从 holder 读，而不是引用局部变量 agent")
    # 不得再有 record 作用域里的 agent 引用
    assert '        "folds": getattr(agent' not in text, (
        "record 里直接 getattr(agent) 会 NameError（worker 静默崩溃）")


def test_folds_in_record_dict_shape():
    """folds 必须与 tokens/correct 等同级出现（同一 dict）。"""
    text = SRC.read_text(encoding="utf-8")
    idx = text.find('"folds": int(holder.get("folds")')
    assert idx > 0
    window = text[max(0, idx - 400): idx + 400]
    for key in ('"correct":', '"has_final_marker":', '"tokens"'):
        assert key in window, f"folds 应与其他结果字段同级（缺 {key}）"


def test_real_worker_output_has_folds_if_present():
    """若存在真实 worker 产物，必须带 folds 字段。"""
    for candidate in sorted((REPO / "eval" / "runs").glob("tmp-out*.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if "task_id" not in data:
            continue
        assert "folds" in data, f"{candidate.name} 缺 folds 字段"
        assert isinstance(data["folds"], int), "folds 必须是整数"
        return


def test_context_runner_reads_folds():
    """runner 侧必须读取并汇总 folds，否则干预验证失效。"""
    runner = (REPO / "eval" / "context_strategies_run.py").read_text(encoding="utf-8")
    assert '"folds": int(record.get("folds") or 0)' in runner
    assert '"foldTotal"' in runner
