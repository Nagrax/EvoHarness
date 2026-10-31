"""eval/silent_triage.py 回归测试（2026-10-30）。

为什么现在才补：原计划让 EvoHarness 自己写（dogfooding #4），但模型两次限流失败。
**我自己写**——不把"未完成"包装成"等条件"。

测试聚焦两件事：
① 分类逻辑（真缺陷 / 合理防御 / 需人工）三种判据
② **一个真实崩过的 bug**：`detail.rule` 为 list 时 `list in set` 抛
   `TypeError: unhashable type` —— 整个工具不可用（由 BACKLOG 的 L0022 触发）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.silent_triage as triage


def _write_backlog(root: Path, items: list[dict]) -> None:
    target = root / "eval" / "loop"
    target.mkdir(parents=True, exist_ok=True)
    (target / "BACKLOG.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")


def _write_source(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_classify_state_word_is_real_defect(tmp_path):
    _write_source(tmp_path, "m.py",
                  "def f():\n    try:\n        save_state()\n    except Exception:\n        pass\n")
    category, reason = triage.classify(tmp_path / "m.py", 3)
    assert category == "real-defect"
    assert "state:" in reason


def test_classify_cleanup_word_is_reasonable_defense(tmp_path):
    _write_source(tmp_path, "m.py",
                  "def f():\n    try:\n        pipe.close()\n    except Exception:\n        pass\n")
    category, reason = triage.classify(tmp_path / "m.py", 3)
    assert category == "reasonable-defense"
    assert "cleanup:" in reason


def test_classify_no_signal_needs_review(tmp_path):
    _write_source(tmp_path, "m.py",
                  "def f():\n    try:\n        x = int(y)\n    except Exception:\n        pass\n")
    category, _ = triage.classify(tmp_path / "m.py", 3)
    assert category == "needs-review"


def test_classify_unreadable_file_needs_review(tmp_path):
    category, reason = triage.classify(tmp_path / "missing.py", 1)
    assert category == "needs-review"
    assert reason == "unreadable"


def test_main_survives_list_valued_rule(tmp_path, monkeypatch):
    """真实崩过的场景：`detail.rule` 是 list → `list in set` 抛 TypeError。

    来源：BACKLOG L0022（variance 的三条判据写成了数组）。
    修复前整个 triage 工具不可用。
    """
    _write_source(tmp_path, "m.py", "try:\n    save_state()\nexcept Exception:\n    pass\n")
    _write_backlog(tmp_path, [
        {"id": "L1", "status": "open",
         "detail": {"rule": "silent-except", "file": "m.py", "line": 2}},
        {"id": "L2", "status": "open",
         "detail": {"rule": ["a", "b"], "file": "m.py", "line": 2}},
    ])
    monkeypatch.setattr(triage, "REPO", tmp_path)

    class _Sink:
        def write(self, _t):  # noqa: D102
            return 0

        def flush(self):  # noqa: D102
            return None

    monkeypatch.setattr(sys, "stdout", _Sink())
    assert triage.main() == 0, "rule 为 list 时不得崩"

    report = json.loads((tmp_path / "eval" / "runs" / "silent-triage.json")
                        .read_text(encoding="utf-8"))
    ids = {e["id"] for bucket in ("real-defect", "reasonable-defense", "needs-review")
           for e in report[bucket]}
    assert "L1" in ids, "正常的 silent-except 条目应被分类"
    assert "L2" not in ids, "rule 为 list 的条目应被跳过（不是 silent-except）"


def test_main_survives_stdout_without_reconfigure(tmp_path, monkeypatch):
    """stdout 无 reconfigure 时不得崩（与 loop_context 同源缺陷）。"""
    _write_backlog(tmp_path, [])
    monkeypatch.setattr(triage, "REPO", tmp_path)

    class _Plain:
        def write(self, _t):  # noqa: D102
            return 0

        def flush(self):  # noqa: D102
            return None

    monkeypatch.setattr(sys, "stdout", _Plain())
    assert triage.main() == 0


def test_missing_backlog_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(triage, "REPO", tmp_path)

    class _Sink:
        def write(self, _t):  # noqa: D102
            return 0

        def flush(self):  # noqa: D102
            return None

    monkeypatch.setattr(sys, "stdout", _Sink())
    assert triage.main() == 1, "缺 BACKLOG 应返回 1 而不是崩"