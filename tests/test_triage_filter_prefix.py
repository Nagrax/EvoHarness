"""mark_triaged 过滤前缀回归（2026-10-29 抓到的真缺陷）。

bug：`startswith("module:silent-except:")` 匹配不到
`module:silent-except-return-none:<file>`（第 9 字符是 "-" 不是 ":"）→
这类 finding 从未被 triage 处理，永远挂在 pending。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SRC = REPO / "eval" / "mark_triaged.py"


def test_filter_covers_all_module_rules():
    text = SRC.read_text(encoding="utf-8")
    assert 'if not key.startswith("module:")' in text, (
        "过滤条件必须覆盖所有 module: 规则（含 return-none 变体）")
    assert 'startswith("module:silent-except:")' not in text, (
        "不得再用精确前缀（会漏掉 silent-except-return-none）")


def test_prefix_bug_is_reproducible_as_documented():
    """把 bug 的成因固定下来：两种 key 的前缀关系。"""
    key_plain = "module:silent-except:agents/agent.py"
    key_variant = "module:silent-except-return-none:agents/session.py"
    assert key_plain.startswith("module:silent-except:")
    assert not key_variant.startswith("module:silent-except:"), (
        "这正是旧过滤条件漏处理 return-none 的原因")
    assert key_variant.startswith("module:")


def test_no_shadowed_builtin_file():
    """不得用 file 作变量名（遮蔽内置名，易错）。"""
    text = SRC.read_text(encoding="utf-8")
    assert "rule, _, file = body.partition" not in text, "file 遮蔽内置名"
    if "module_file" in text:
        assert 'VERDICTS.get(f"{module_file}#{rule}")' in text


def test_module_backlog_fully_triaged():
    """真实 backlog：module 类项不应再有未判读位置挂在 pending。"""
    import json

    path = REPO / "eval" / "loop" / "BACKLOG.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    pending = []
    for item in data["items"]:
        if item["status"] == "done":
            continue
        if not (item.get("key") or "").startswith("module:"):
            continue
        pending.append(item["id"])
    assert not pending, f"仍有未闭合的 module 项：{pending}"
