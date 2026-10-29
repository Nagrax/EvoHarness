"""injection-without-truncate 规则精度回归（2026-10-29 判读后收紧）。

原实现只看"源码文本里有 content 字样"，实测 5 处命中里 4 处是误报
（固定错误提示、含 `.get('content')` 的短消息）。收紧后真实仓库 0 命中。

本测试用合成代码固定双向行为：
- 短固定提示 / 单变量插值 → **不报**（这是之前的误报来源）
- 长字面量 + 多变量 + 疑似正文 → **必须报**（防收紧过度变成漏报）
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.module_scan import scan_file


def _findings(tmp_path: Path, body: str) -> list[dict]:
    path = tmp_path / "sample.py"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return [f for f in scan_file(path) if f["rule"] == "injection-without-truncate"]


def test_short_error_message_not_reported(tmp_path):
    """固定错误提示（短、单变量）是之前的误报来源。"""
    hits = _findings(tmp_path, '''
        def f(verb):
            return f"Error: You must read this file before {verb}. Use read_file first."
        ''')
    assert not hits, f"短固定提示不该被报：{hits}"


def test_single_variable_interpolation_not_reported(tmp_path):
    hits = _findings(tmp_path, '''
        def f(memory_dir):
            return f"You have a persistent memory system at `{memory_dir}`."
        ''')
    assert not hits


def test_long_body_injection_is_reported(tmp_path):
    """长字面量 + 多变量 + 正文类名字 → 必须报（防收紧过度）。"""
    filler = "x" * 420
    hits = _findings(tmp_path, f'''
        def f(content, history):
            return f"""
            {filler}
            conversation:
            {{content}}
            previous:
            {{history}}
            """
        ''')
    assert hits, "长文本注入必须被报（否则规则失效）"
    assert "字符" in hits[0]["detail"]


def test_truncate_mention_suppresses_report(tmp_path):
    """字符串里出现 truncate 说明作者已处理，不报。"""
    filler = "y" * 420
    hits = _findings(tmp_path, f'''
        def f(content, history):
            return f"""
            {filler}
            {{content}}
            {{history}}
            (truncated to limit)
            """
        ''')
    assert not hits


def test_real_repo_has_no_injection_findings():
    """真实仓库：人工判读后 5 处均为误报/已有上游限制，期望 0 命中。"""
    import json

    path = REPO / "eval" / "runs" / "module-scan.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    hits = [f for f in data["findings"] if f["rule"] == "injection-without-truncate"]
    assert not hits, f"真实仓库又出现注入告警（需重新判读）：{[h['file'] for h in hits]}"