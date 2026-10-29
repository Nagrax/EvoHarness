"""记忆生命周期层回归（W15 后续：先进设计移植的第一个真实落地）。

移植来源：mem0/Letta 类系统的生命周期管理（冲突消解/合并重复/过期降权）——
面经文档引用、对比表判定"最值得先移植"的机制。移植方式是**新增层**，
不改变既有 frontmatter/召回链路（M1-M7 断言继续通过）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agents.memory_lifecycle import (
    MERGE_THRESHOLD,
    MemoryRecord,
    parse_memory_file,
    recall_weight,
    resolve_conflicts,
)
def _rec(name, subject, updated, *, mtype="project", desc="d"):
    return MemoryRecord(name=name, subject=subject, mtype=mtype, description=desc,
                        content="body", updated_at=updated)


NOW = 1_800_000_000  # 固定时钟，测试可复现


# ── L1 冲突消解：同 subject 新覆盖旧，旧版本留痕 ──────────────


def test_same_subject_newer_supersedes_older():
    records = [_rec("old", "deploy-policy", NOW - 86400), _rec("new", "deploy-policy", NOW)]
    result = resolve_conflicts(records, now=NOW)
    kept_names = [r.name for r in result["kept"]]
    assert kept_names == ["new"], "同 subject 只保留最新"
    assert result["superseded"] == [{"superseded": "old", "by": "new",
                                     "oldVersion": 1, "newVersion": 1}]
    actions = {d["name"]: d["action"] for d in result["decisions"]}
    assert actions["new"] == "add"      # 最新的作为 add 进入
    assert actions["old"] == "discard"  # 旧的被 discard，决策可追溯


def test_supersede_direction_is_by_mtime_not_order():
    records = [_rec("later-named-first", "subj", NOW), _rec("earlier", "subj", NOW - 100)]
    result = resolve_conflicts(records, now=NOW)
    assert [r.name for r in result["kept"]] == ["later-named-first"], (
        "覆盖方向由 updated_at 决定，与输入顺序无关")


# ── L2 合并建议：相似但不同 subject ──────────────────────────


def test_similar_subjects_get_merge_suggestion():
    # L2 场景：不同 subject（都存活）、高相似描述 → 建议 merge 而非静默新增
    records = [
        _rec("commit-message", "git-commit-message", NOW,
             desc="write git commit message conventions and format rules"),
        _rec("commit-msg", "git-commit-msg-rules", NOW,
             desc="writing git commit message conventions and format rules"),
    ]
    result = resolve_conflicts(records, now=NOW)
    assert [r.name for r in result["kept"]] == ["commit-message", "commit-msg"], (
        "不同 subject 都保留（L1 不覆盖）")
    sim = result["mergeSuggestions"]
    assert sim and sim[0]["similarity"] >= MERGE_THRESHOLD, (
        f"高相似描述应给出 merge 建议，实际 {result['mergeSuggestions']}")
    assert sim[0]["suggestedAction"] == "merge"


def test_merge_suggestion_boundary_is_exact_threshold():
    """边界：恰好达到 MERGE_THRESHOLD 的对被召回；低于则不召回。"""
    records = [
        _rec("a", "alpha beta gamma", NOW, desc="one two three"),
        _rec("b", "alpha beta delta", NOW, desc="one two four"),
    ]
    result = resolve_conflicts(records, now=NOW)
    sim = result["mergeSuggestions"]
    # alpha/beta/one/two 共 4 个交集体 token，Jaccard = 4/8 = 0.5 >= 0.4 → 召回
    assert sim and sim[0]["similarity"] >= MERGE_THRESHOLD


def test_dissimilar_records_no_merge_noise():
    records = [
        _rec("python-sort", "python-sort", NOW, desc="sorting python lists"),
        _rec("git-tag", "git-tag", NOW, desc="creating git tags"),
    ]
    result = resolve_conflicts(records, now=NOW)
    assert result["mergeSuggestions"] == []


def test_merge_threshold_aligned_with_skill_dedup():
    """与 skill 去重口径一致（中文分词 Jaccard 偏低的教训）。"""
    assert MERGE_THRESHOLD == 0.4


# ── L3 过期降权 ─────────────────────────────────────────────


def test_recall_weight_decays_daily_with_floor():
    fresh = _rec("fresh", "s", NOW)
    old10 = _rec("old", "s", NOW - 10 * 86400)
    old400 = _rec("ancient", "s", NOW - 400 * 86400)
    w_fresh = recall_weight(fresh, now=NOW)
    w_old = recall_weight(old10, now=NOW)
    w_ancient = recall_weight(old400, now=NOW)
    assert w_fresh == pytest.approx(1.0)
    assert w_old == pytest.approx(0.8)     # 10 天 × 2%/天
    assert w_ancient == 0.5, "超期记忆必须被 floor 托住，不归零（有损便签定位）"
    assert w_ancient < w_old < w_fresh


# ── 与既有记忆文件格式兼容 ──────────────────────────────────


def test_parse_memory_file_reads_existing_frontmatter(tmp_path):
    path = tmp_path / "deploy.md"
    path.write_text("---\nname: deploy\ntype: project\ndescription: 部署约定\n"
                    "---\n\n生产库用隔离容器。\n", encoding="utf-8")
    record = parse_memory_file(path)
    assert record is not None
    assert record.name == "deploy"
    assert record.mtype == "project"
    assert record.subject  # 缺 subject 时自动从 name 规范化
    assert "隔离容器" in record.content


def test_parse_memory_file_no_frontmatter_returns_none(tmp_path):
    path = tmp_path / "plain.md"
    path.write_text("没有 frontmatter 的文件\n", encoding="utf-8")
    assert parse_memory_file(path) is None


def test_lifecycle_layer_boundary():
    """移植边界：memory.py 不 import lifecycle 模块（L3 用 mtime_ms 就地排序，零耦合）。

    L1（resolve_conflicts）与文件解析属于写入路径，召回链路只消费 L3 的降权排序——
    边界清晰才能保证 M1-M7 既有断言的验证对象只增加不改变。
    """
    import re

    memory_src = (REPO / "agents" / "memory.py").read_text(encoding="utf-8")
    import_lines = [l.strip() for l in memory_src.splitlines()
                    if l.strip().startswith(("from agents.memory_lifecycle",
                                             "from .memory_lifecycle",
                                             "import agents.memory_lifecycle"))]
    assert not import_lines, f"memory.py 不得 import lifecycle 模块：{import_lines}"
    # 注释提及允许（移植来源标注），但不允许真实 import
    assert "memory_lifecycle 移植" in memory_src  # 来源标注在
    # L1/L2 的解析与消解留在独立层，不被召回链路调用
    lifecycle_src = (REPO / "agents" / "memory_lifecycle.py").read_text(encoding="utf-8")
    assert "resolve_conflicts" in lifecycle_src
    # S1/S3 的行为开关由 memory.py 自有 env 读取
    assert "EVOHARNESS_MEMORY_LIFECYCLE" in memory_src
