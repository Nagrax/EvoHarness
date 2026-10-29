"""limitation（能力边界）处置回归：闭环第四类处置必须存在且不可滥用。

背景：triage 原有三类（real-defect / reasonable-defense / informational）都不覆盖
"这不是缺陷，是我们做不到"。真实案例：GAIA level 2/3 的稳定失败全是多跳网络检索，
改代码修不了——留在 pending 会永久污染排期。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

LOOP = REPO / "eval" / "loop.py"


def _run(*args: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(LOOP), *args], capture_output=True,
                          cwd=str(REPO), env={"PYTHONUTF8": "1", "PATH": ""}, check=False)
    out = proc.stdout.decode("utf-8", "replace").strip()
    try:
        return proc.returncode, json.loads(out) if out.startswith("{") else {}
    except ValueError:
        return proc.returncode, {}


def test_limitation_requires_reason():
    """不给理由就当边界 = 变相关闭工单，必须拒绝。"""
    code, _ = _run("limitation", "L0001")
    assert code != 0, "缺少 --reason 必须失败"


def test_limitation_subcommand_registered_correctly():
    """parser 顺序回归：limitation 的 set_defaults 不能被 block 覆盖。"""
    text = LOOP.read_text(encoding="utf-8").splitlines()
    seq = [l for l in text if 'sub.add_parser("block")' in l
           or 'sub.add_parser("limitation")' in l
           or 'cmd_close(a.id, "blocked"' in l or 'cmd_limitation(a.id' in l]
    assert len(seq) == 4, f"期望 4 行注册，实际 {len(seq)}"
    # block parser 后必须紧跟自己的 set_defaults（否则 limitation 会调 cmd_close）
    assert 'cmd_close(a.id, "blocked"' in seq[1]
    assert 'add_parser("limitation")' in seq[2]
    assert 'cmd_limitation(a.id' in seq[3]


def test_real_limitation_item_closed_correctly():
    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    items = [i for i in data["items"] if i.get("closedReason") == "known-limitation"]
    if not items:
        return
    for item in items:
        assert item["status"] == "done", "能力边界必须移出排期（done）"
        assert item.get("limitationReason"), "必须记录边界理由"
        assert not (item.get("openOccurrences") or []), "边界项不应再有未处理位置"


def test_limitation_is_distinct_from_blocked():
    """边界（我们做不到）与阻塞（外部条件不具备）必须区分——语义不同，排期处理不同。"""
    source = LOOP.read_text(encoding="utf-8")
    assert 'closedReason"] = "known-limitation"' in source
    assert 'cmd_close(a.id, "blocked"' in source
