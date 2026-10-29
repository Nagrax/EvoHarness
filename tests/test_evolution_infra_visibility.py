"""闭环修复回归：进化实验的路径与基础设施失败必须可区分。

两个真实事故（都在本轮真机 pilot 里踩到）：

1. `--workspace` 传相对路径时，worker 子进程以 `cwd=<arm cwd>` 启动，
   `--item-json eval/runs/...` 在子进程 cwd 下解析 → FileNotFoundError
   → 记成 worker_no_json → 该题静默 0 分。分数"看起来有值"，其实是路径 bug。
   修复：ws_root 必须 `.resolve()` 成绝对路径。

2. worker 没产出 JSON 几乎总是环境问题，但旧实现只记 `error`，
   与"模型答错"无法区分。修复：加 `infra_error: True` 并在 failures 里单列 `infra`。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval.evolution_experiment as ee


def test_workspace_is_resolved_to_absolute():
    source = inspect.getsource(ee._cmd_run)
    assert ".resolve()" in source, "workspace 必须 resolve 成绝对路径，否则子进程 cwd 变化会找不到 item 文件"
    assert "ws_root = Path(args.workspace or SESSION_MEMORY_DEFAULT).resolve()" in source


def test_worker_no_json_marks_infra_error():
    source = inspect.getsource(ee._run_single)
    assert '"infra_error": True' in source, "worker 未产出 JSON 必须标 infra_error"
    assert "worker_no_json" in source


def test_failure_report_separates_infra():
    source = inspect.getsource(ee._cmd_run)
    assert '"infra": sum(' in source, "failures 必须单列 infra 计数"
    # stream 与 holdout 两处都要有
    assert source.count('"infra": sum(') >= 2


def test_arm_manifest_still_marks_switches():
    manifest = ee._arm_manifest(
        ["no_evolution", "ungated_evolution", "gated_evolution"], gated_promotion=True)
    assert manifest["no_evolution"]["streamEvolve"] is False
    assert manifest["ungated_evolution"]["irMode"] == "off"
    assert manifest["gated_evolution"]["promotion"] == "apply_eval_promotions"
