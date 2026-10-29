"""eval/triage_file.py 回归（这是 module-without-tests 唯一真实缺口）。

判据（与 eval/loop/README.md 一致）优先级：
  清理 > 降级 > 状态 > 无信号
即：
  - 只有清理词 → reasonable-defense（清理收尾不该算缺陷）
  - 只有降级词 → degradation-only（失败可从源头恢复）
  - 只有状态词 → real-defect（吞掉用户可见状态）
  - 状态+清理 → real-defect（状态优先，因为丢的是数据）
  - 都没有 → needs-review
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.triage_file import block_source, classify


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def test_block_source_returns_window(tmp_path):
    path = _write(tmp_path, """
        line1
        line2
        line3
        line4
    """)
    text = block_source(path, 2, span=2)
    assert "line2" in text and "line3" in text
    assert "line1" not in text


def test_block_source_missing_file_returns_empty(tmp_path):
    assert block_source(tmp_path / "nope.py", 1) == ""


def test_block_source_beyond_eof_returns_empty(tmp_path):
    path = _write(tmp_path, "only_one_line\n")
    assert block_source(path, 99, span=5) == ""


def test_classify_cleanup_only(tmp_path):
    path = _write(tmp_path, """
        try:
            conn.close()
        except Exception:
            pass
    """)
    verdict, reason = classify(path, 1)
    assert verdict == "reasonable-defense"
    assert reason.startswith("cleanup:")


def test_classify_degradation_only(tmp_path):
    path = _write(tmp_path, """
        try:
            read_file_state[abs_path] = os.path.getmtime(abs_path)
        except OSError:
            pass
    """)
    verdict, reason = classify(path, 1)
    assert verdict == "degradation-only"
    assert reason.startswith("degrade:")


def test_classify_state_only_is_defect(tmp_path):
    path = _write(tmp_path, """
        try:
            record.save()
        except Exception:
            pass
    """)
    verdict, reason = classify(path, 1)
    assert verdict == "real-defect"
    assert reason.startswith("state:")


def test_classify_state_beats_cleanup(tmp_path):
    """状态 + 清理同时出现时按缺陷判（丢的是数据，不能被清理语义掩盖）。"""
    path = _write(tmp_path, """
        try:
            record.save()
            conn.close()
        except Exception:
            pass
    """)
    verdict, reason = classify(path, 1)
    assert verdict == "real-defect"
    assert "state-priority:" in reason


def test_classify_no_signal_is_needs_review(tmp_path):
    path = _write(tmp_path, """
        try:
            do_something_unknown()
        except Exception:
            pass
    """)
    verdict, reason = classify(path, 1)
    assert verdict == "needs-review"
    assert reason == "no-signal"


def test_classify_is_case_insensitive(tmp_path):
    path = _write(tmp_path, """
        try:
            Connection.CLOSE()
        except Exception:
            pass
    """)
    verdict, _ = classify(path, 1)
    assert verdict == "reasonable-defense"


def test_real_repo_silent_except_sites_are_classifiable():
    """对真实仓库抽样：每个位置都能得到四种判读之一（不会抛异常）。"""
    import json

    scan = REPO / "eval" / "runs" / "module-scan.json"
    if not scan.is_file():
        return
    data = json.loads(scan.read_text(encoding="utf-8"))
    sites = [f for f in data["findings"] if f["rule"].startswith("silent-except")][:8]
    allowed = {"real-defect", "reasonable-defense", "degradation-only", "needs-review"}
    for site in sites:
        verdict, _reason = classify(REPO / site["file"], site["exceptLine"])
        assert verdict in allowed, f"{site['file']}:{site['exceptLine']} → {verdict}"
