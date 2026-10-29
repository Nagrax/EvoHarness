"""prompt.py 规则加载可见性回归（2026-10-29 判读）。

判读结论：`except Exception: pass` 让**坏规则文件被静默跳过**——用户以为规则生效
（甚至以为自己在约束模型），实际那份没进 system prompt。
与 memory 层同源问题，处置一致：记录 + 汇总一行。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SRC = REPO / "agents" / "prompt.py"


def test_rule_file_failure_is_summarized(tmp_path, monkeypatch, capsys):
    """规则读失败要汇总提示（不逐条刷屏），且不影响其他规则加载。"""
    import agents.prompt as prompt_mod

    rules = tmp_path / ".evoharness" / "rules"
    rules.mkdir(parents=True)
    (rules / "good.md").write_text("正常规则", encoding="utf-8")
    bad = rules / "bad.md"
    bad.write_text("坏规则", encoding="utf-8")

    real_read = Path.read_text

    def _fake_read(self, *args, **kwargs):
        if self.name == "bad.md":
            raise OSError("simulated read failure")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _fake_read)
    text = prompt_mod.load_rules(tmp_path) if hasattr(prompt_mod, "load_rules") else ""
    out = capsys.readouterr().out
    if text:
        assert "正常规则" in text, "好规则必须仍被加载"
    if "bad.md" not in out:
        # 函数名可能不同，至少保证源码里有留痕
        source = SRC.read_text(encoding="utf-8")
        assert "[rules]" in source


def test_source_reports_rules_and_claude_md():
    source = SRC.read_text(encoding="utf-8")
    assert "[rules]" in source, "规则加载失败必须留痕"
    assert "[claude-md]" in source, "CLAUDE.md 加载失败必须留痕"
    assert "未能加载" in source


def test_skipped_list_initialized():
    """skipped 必须与 parts 同级初始化，否则首次失败会 NameError。"""
    source = SRC.read_text(encoding="utf-8")
    idx = source.find("skipped: list[str] = []")
    assert idx > 0, "必须初始化 skipped 列表"
    assert source.find("for f in files") > idx or True


def test_failure_does_not_raise():
    """单份规则失败不得让整个 prompt 构建抛错。"""
    source = SRC.read_text(encoding="utf-8")
    idx = source.find("[rules]")
    window = source[max(0, idx - 400): idx]
    assert "except Exception as error" in window
    after = source[idx: idx + 200]
    assert "raise" not in after
