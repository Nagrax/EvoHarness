"""批次隔离回归：checkpoint/curve 必须绑定 workspace tag。

真实事故（2026-10-29）：RESULTS_DIR 全局固定、文件名不含批次 →
W14 前半批（ws-w14b）与后半批（ws-w14c）**同名互相覆盖**，
读侧只能靠 mtime 猜，差点用混合数据得出"gated 2.8%"的错误结论。

修法固定在此：文件名带 tag + sidecar batch-<tag>.json + curve 双写（tagged 归档 +
curve.json 作最新指针，保持既有读侧兼容）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.evolution_experiment import _batch_tag


def test_batch_tag_from_workspace_name():
    assert _batch_tag(Path("D:/x/ws-w14c")) == "ws-w14c"
    assert _batch_tag(Path("D:/x/ws_w14b")) == "ws_w14b"


def test_batch_tag_sanitizes_unsafe_chars():
    tag = _batch_tag(Path("D:/x/ws 14:c*v2"))
    assert " " not in tag and ":" not in tag and "*" not in tag
    assert tag, "标识不能为空"


def test_batch_tag_falls_back_for_empty_name():
    assert _batch_tag(Path("D:/")) == "default"


def test_checkpoint_filename_includes_tag():
    source = (REPO / "eval" / "evolution_experiment.py").read_text(encoding="utf-8")
    assert 'checkpoint-{batch_tag}-{arm}-r{rnd}.json' in source, (
        "checkpoint 文件名必须带批次标识，否则不同 workspace 会互相覆盖")


def test_curve_dual_written_for_read_compat():
    source = (REPO / "eval" / "evolution_experiment.py").read_text(encoding="utf-8")
    assert 'curve-{batch_tag}.json' in source, "必须有带 tag 的归档"
    assert '(RESULTS_DIR / "curve.json").write_text' in source, (
        "curve.json 必须继续写，作为最新指针（roi.py / curve_summary.py 依赖它）")


def test_sidecar_records_batch_metadata():
    source = (REPO / "eval" / "evolution_experiment.py").read_text(encoding="utf-8")
    assert 'batch-{batch_tag}.json' in source
    assert '"batchTag": batch_tag' in source
    assert '"workspace": str(ws_root)' in source
