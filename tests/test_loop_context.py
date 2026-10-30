"""eval/loop_context.py 回归测试。

loop_context.main() 从固定 REPO 读取 eval/loop/BACKLOG.json，
按 sys.argv 的 limit / rule 过滤后打印源码上下文片段。
测试用 tmp_path + monkeypatch 重定向 REPO 与 stdout，保证自包含。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.loop_context as loop_context


def _write_backlog(root: Path, items: list[dict]) -> None:
    target = root / "eval" / "loop"
    target.mkdir(parents=True, exist_ok=True)
    (target / "BACKLOG.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8"
    )


def _write_source(root: Path, rel: str, lines: int = 12, width: int = 20) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"line {i:02d} " + "x" * width + "\n" for i in range(1, lines + 1))
    path.write_text(content, encoding="utf-8")
    return path


def _item(item_id: str, rule: str, file: str, line: int) -> dict:
    return {"id": item_id, "detail": {"rule": rule, "file": file, "line": line}}


def _run(monkeypatch, root: Path, argv: list[str] | None = None) -> tuple[int, str]:
    monkeypatch.setattr(loop_context, "REPO", root)
    monkeypatch.setattr(sys, "argv", ["loop_context.py", *(argv or [])])
    buf = io.BytesIO()
    wrapper = io.TextIOWrapper(buf, encoding="utf-8", errors="replace", newline="")
    monkeypatch.setattr(sys, "stdout", wrapper)
    try:
        rc = loop_context.main()
        wrapper.flush()
        text = buf.getvalue().decode("utf-8")
    finally:
        wrapper.detach()
    return rc, text


def test_prints_item_header_and_context(tmp_path, monkeypatch):
    _write_source(tmp_path, "src/a.py")
    _write_backlog(tmp_path, [_item("B-1", "ruleX", "src/a.py", 6)])

    rc, out = _run(monkeypatch, tmp_path)

    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == "B-1 ruleX src/a.py:6"
    # window = lines[max(0, 6-4) : 6+2] -> 0 基下标 2..7，即第 3..8 行
    context = lines[1].strip()
    assert context.startswith("line 03")
    assert "line 08" in context
    assert "line 09" not in context
    assert " | " in context


def test_limit_caps_number_of_items(tmp_path, monkeypatch):
    for i in range(1, 6):
        _write_source(tmp_path, f"src/m{i}.py")
    _write_backlog(
        tmp_path,
        [_item(f"B-{i}", "r", f"src/m{i}.py", 2) for i in range(1, 6)],
    )

    rc, out = _run(monkeypatch, tmp_path, ["2"])

    assert rc == 0
    printed_ids = [line.split()[0] for line in out.splitlines() if line.startswith("B-")]
    assert printed_ids == ["B-1", "B-2"]


def test_default_limit_is_twenty(tmp_path, monkeypatch):
    items = [_item(f"B-{i}", "r", "src/d.py", 3) for i in range(1, 26)]
    _write_source(tmp_path, "src/d.py")
    _write_backlog(tmp_path, items)

    rc, out = _run(monkeypatch, tmp_path)

    assert rc == 0
    headers = [line for line in out.splitlines() if line.startswith("B-")]
    assert len(headers) == 20
    assert headers[-1].startswith("B-20 ")


def test_rule_filter_selected_and_unknown_rule_is_empty(tmp_path, monkeypatch):
    _write_source(tmp_path, "src/f.py")
    _write_backlog(
        tmp_path,
        [
            _item("B-1", "keep", "src/f.py", 5),
            _item("B-2", "drop", "src/f.py", 5),
        ],
    )

    rc, out = _run(monkeypatch, tmp_path, ["10", "keep"])
    assert rc == 0
    assert "B-1 keep" in out
    assert "B-2" not in out

    rc2, out2 = _run(monkeypatch, tmp_path, ["10", "absent"])
    assert rc2 == 0
    assert out2.strip() == ""


def test_missing_file_is_skipped(tmp_path, monkeypatch):
    _write_source(tmp_path, "src/ok.py")
    _write_backlog(
        tmp_path,
        [
            _item("B-1", "r", "src/does_not_exist.py", 4),
            _item("B-2", "r", "src/ok.py", 4),
        ],
    )

    rc, out = _run(monkeypatch, tmp_path)

    assert rc == 0
    assert "B-1" not in out
    assert "B-2" in out


def test_context_line_is_truncated_to_220_chars(tmp_path, monkeypatch):
    _write_source(tmp_path, "src/long.py", width=400)
    _write_backlog(tmp_path, [_item("B-1", "r", "src/long.py", 3)])

    rc, out = _run(monkeypatch, tmp_path)

    assert rc == 0
    context = out.splitlines()[1].strip()
    assert len(context) == 220


def test_line_number_before_file_start_is_clamped(tmp_path, monkeypatch):
    _write_source(tmp_path, "src/short.py", lines=3)
    _write_backlog(tmp_path, [_item("B-1", "r", "src/short.py", 1)])

    rc, out = _run(monkeypatch, tmp_path)

    assert rc == 0
    context = out.splitlines()[1].strip()
    assert context.startswith("line 01")
    assert "line 03" in context

def test_main_survives_stdout_without_reconfigure(tmp_path, monkeypatch):
    """stdout 没有 `reconfigure` 时不得崩（dogfooding #3 报告的真实缺陷）。

    真实场景：被 IDE / CI / 其他程序替换 stdout 时，只是一个普通写对象。
    修复前会在 `main()` 第一行抛 AttributeError，什么都打印不出来。
    """
    _write_backlog(tmp_path, [])
    monkeypatch.setattr(loop_context, "REPO", tmp_path)

    class _PlainOut:
        """只有 write/flush 的 stdout（模拟无 reconfigure 的包装器）。"""

        def __init__(self) -> None:
            self.buffer: list[str] = []

        def write(self, text: str) -> int:
            self.buffer.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    fake = _PlainOut()
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.setattr(sys, "argv", ["loop_context.py"])

    # 不得抛异常
    assert loop_context.main() == 0
