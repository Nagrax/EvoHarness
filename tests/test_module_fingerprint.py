"""代码指纹回归：位置可跨行号漂移重新定位。

真实问题（2026-10-29）：backlog 20 个 high finding 只存行号，代码改动后失效 →
既定位不到、也不会被扫描清空 → 闭环在最重要处空转。
指纹锚在 except 行及其上下文，行号变了也能找到。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.module_scan import code_fingerprint, locate_by_fingerprint


def test_fingerprint_stable_under_line_shift(tmp_path):
    body = [
        "import os",
        "def f():",
        "    try:",
        "        return do()",
        "    except Exception:",
        "        pass",
    ]
    path = tmp_path / "m.py"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    fp = code_fingerprint(lines, 5)          # except 行

    # 在文件头插入 3 行 → 行号漂移到 8，但指纹不变
    shifted = ["# pad1", "# pad2", "# pad3"] + body
    path.write_text("\n".join(shifted) + "\n", encoding="utf-8")
    lines2 = path.read_text(encoding="utf-8").splitlines()
    assert code_fingerprint(lines2, 8) == fp, "行号漂移后指纹必须不变"
    assert locate_by_fingerprint(path, fp) == 8, "必须能按指纹找回新行号"


def test_fingerprint_changes_when_code_changes(tmp_path):
    path = tmp_path / "m.py"
    path.write_text("def f():\n    try:\n        x()\n    except E:\n        pass\n", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    fp = code_fingerprint(lines, 4)
    path.write_text("def f():\n    try:\n        x()\n    except E:\n        raise\n", encoding="utf-8")
    lines2 = path.read_text(encoding="utf-8").splitlines()
    assert code_fingerprint(lines2, 4) != fp, "代码变了指纹必须变（否则不是指纹）"


def test_locate_returns_zero_when_gone(tmp_path):
    path = tmp_path / "m.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert locate_by_fingerprint(path, "deadbeefcafe") == 0


def test_locate_returns_zero_for_empty_fingerprint(tmp_path):
    path = tmp_path / "m.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert locate_by_fingerprint(path, "") == 0, "空指纹不得匹配首行（否则会误定位）"


def test_locate_handles_missing_file(tmp_path):
    assert locate_by_fingerprint(tmp_path / "nope.py", "abc123") == 0


def test_real_scan_findings_carry_fingerprint():
    path = REPO / "eval" / "runs" / "module-scan.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    silent = [f for f in data["findings"] if f["rule"].startswith("silent-except")]
    if not silent:
        return
    with_fp = [f for f in silent if f.get("fingerprint")]
    assert with_fp, "silent-except 类 finding 必须带指纹"
    # 抽样验证指纹能精确定位回原 except 行
    for finding in with_fp[:8]:
        got = locate_by_fingerprint(REPO / finding["file"], finding["fingerprint"])
        assert got == finding["exceptLine"], (
            f"{finding['file']} 指纹重定位失败：期望 {finding['exceptLine']} 实际 {got}")
