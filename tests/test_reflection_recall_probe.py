"""教训可召回性回归：反思链路第一环必须通（召回不到则收益无从谈起）。

真实结果：3/3 相关查询命中教训，无关查询 0 误召（2870 tokens，raccoon 模型）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.reflection_recall_probe as probe
from agents.memory import parse_frontmatter


def test_fixture_lesson_is_valid_memory(tmp_path):
    """探针造的教训必须过 frontmatter 契约（否则探针本身测不到真实链路）。"""
    fixtures = probe.build_fixtures(tmp_path / "memories")
    lesson_path = tmp_path / "memories" / fixtures["lessonFile"]
    assert lesson_path.is_file()
    meta = parse_frontmatter(lesson_path.read_text(encoding="utf-8")).meta
    assert meta.get("type") == "feedback"
    assert meta.get("subject") == fixtures["lessonSubject"]


def test_fixtures_include_decoys_and_unrelated_query(tmp_path):
    """必须有干扰项与无关查询——否则测不出过度召回。"""
    fixtures = probe.build_fixtures(tmp_path / "memories")
    files = list((tmp_path / "memories").glob("*.md"))
    assert len(files) >= 8, "至少 1 条教训 + 7 条干扰"
    kinds = {parse_frontmatter(p.read_text(encoding="utf-8")).meta.get("type") for p in files}
    assert "feedback" in kinds and "project" in kinds
    unrelated = [q for q in fixtures["queries"] if not q["expect"]]
    assert unrelated and unrelated[0]["forbid"], "无关查询必须声明禁止召回的教训"


def test_verdict_requires_zero_false_recall():
    """判定标准：召回率 > 0 且零误召，否则判链路断裂。"""
    source = (REPO / "eval" / "reflection_recall_probe.py").read_text(encoding="utf-8")
    assert "recall_rate > 0 and false_recall == 0" in source
    assert "not-recallable:reflection-chain-broken" in source


def test_probe_does_not_touch_real_memory_dir():
    """探针必须在临时目录跑，不得污染真实 ~/.evoharness/memories。"""
    source = (REPO / "eval" / "reflection_recall_probe.py").read_text(encoding="utf-8")
    assert "tempfile.TemporaryDirectory" in source
    assert "memory.get_memory_dir = original" in source, "必须恢复原目录（失败也不泄漏）"
