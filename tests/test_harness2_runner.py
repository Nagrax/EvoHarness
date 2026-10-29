"""Harness² runner 的路径与题池绑定回归。

真踩过的两个坑（都导致"跑了但结果作废"）：

1. **manifest 未绑定题池子集**：用 `--task-ids-file` 换题集后，若复用同一 manifest，
   会触发 `SplitManifestDrift: existing split task missing from dataset`。
   修复：workspace/manifest 路径同时绑定 scale 与 pool。

2. **失败 run 复用旧 curve**：exitCode!=0 时若仍读 curve.json，会把上一轮成功的结果
   记成这次变体的结果。修复：只在 exitCode==0 时才读。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import eval.harness2_run as h2


def test_manifest_path_binds_pool_subset():
    source = inspect.getsource(h2.main)
    assert "pool_tag" in source, "manifest/workspace 必须绑定题池子集，否则换题集会 SplitManifestDrift"
    assert "Path(args.task_ids_file).stem" in source
    assert 'f"{scale_tag}-{tag}{pool_tag}"' in source


def test_failed_run_does_not_reuse_curve():
    source = inspect.getsource(h2.main)
    assert 'if record["exitCode"] == 0' in source, "失败 run 不得读 curve"
    assert 'record["runFailed"] = True' in source


def test_task_ids_file_is_passed_through():
    source = inspect.getsource(h2.run_variant)
    assert "--task-ids-file" in source, "必须把题池筛选透传给 evolution_experiment"
    assert "task_ids_file" in source


def test_evolution_experiment_filters_items():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import eval.evolution_experiment as ee

    assert hasattr(ee, "_filter_items_by_ids"), "必须有题池筛选函数"
    items = [
        {"task_id": "a", "Question": "q1"},
        {"task_id": "b", "Question": "q2"},
        {"task_id": "c", "Question": "q3"},
    ]
    # 无清单：原样返回
    assert ee._filter_items_by_ids(items, None) == items
    # 有清单：只保留命中项
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ids_file = Path(tmp) / "ids.json"
        ids_file.write_text(json.dumps(["a", "c"]), encoding="utf-8")
        kept = ee._filter_items_by_ids(items, str(ids_file))
        assert [it["task_id"] for it in kept] == ["a", "c"]


def test_filter_rejects_empty_intersection():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import eval.evolution_experiment as ee
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ids_file = Path(tmp) / "ids.json"
        ids_file.write_text(json.dumps(["zzz-not-in-dataset"]), encoding="utf-8")
        try:
            ee._filter_items_by_ids([{"task_id": "a"}], str(ids_file))
        except SystemExit as error:
            assert "无交集" in str(error)
        else:
            raise AssertionError("题池与数据集无交集时必须显式退出，不能静默跑空")
