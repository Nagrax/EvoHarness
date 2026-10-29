"""_materialize_status_map 回归（2026-10-29 从 398 行函数里拆出）。

这段语义此前**只藏在 398 行函数中间**，无从验证。拆出来后可以固定两条：
① 有答卷（replay.count > 0）→ runtime 与 certified **都**用报告认证状态
② 无答卷（手动导入，无 provenance）→ runtime 按使用数据估算，
   certified **不写入**（诚实展示"未经认证"）

顺带固定一个踩过的坑：拆分时 `async ` 前缀被 anchor 匹配错位，
`_materialize_status_map` 被误标 async、core 反而丢了 async → SyntaxError。
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import agents.online_skill_eval as mod


@pytest.fixture()
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_online_eval_root", lambda: tmp_path)
    return tmp_path


def test_status_map_is_sync_core_is_async():
    assert not inspect.iscoroutinefunction(mod._materialize_status_map), (
        "_materialize_status_map 是同步函数（拆分时曾被误标 async）")
    assert inspect.iscoroutinefunction(mod._evaluate_online_skill_evolution_core), (
        "core 必须是 async（内部有 await）")


def test_answered_skills_enter_both_tables(isolated_root):
    skills = [{"skill": "s1", "status": "healthy", "replay": {"count": 5}}]
    runtime, certified = mod._materialize_status_map(skills)
    assert runtime["s1"] == "healthy"
    assert certified["s1"] == "healthy", "有答卷的必须进认证表（检索加权只认它）"


def test_unanswered_skills_only_enter_runtime(isolated_root):
    skills = [{
        "skill": "s2", "status": "incubating", "replay": {"count": 0},
        "retrieved": 10, "relevant": 6, "used": 4,
    }]
    runtime, certified = mod._materialize_status_map(skills)
    assert "s2" in runtime, "无答卷也要有运行态（否则会被钉死在试用期）"
    assert "s2" not in certified, "无答卷不得进认证表（诚实展示未认证）"


def test_nameless_entries_skipped(isolated_root):
    runtime, certified = mod._materialize_status_map([{"skill": ""}, {"skill": None}])
    assert runtime == {} and certified == {}


def test_status_map_written_atomically_to_expected_path(isolated_root):
    import json

    mod._materialize_status_map([{"skill": "s3", "status": "healthy",
                                  "replay": {"count": 1}}])
    path = isolated_root / "status_map.json"
    assert path.is_file(), "必须物化 status_map.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) >= {"generated_at", "statuses", "certified_statuses"}
    assert data["statuses"]["s3"] == "healthy"


def test_core_returns_two_tables_contract():
    """core 应把两张表传出去（供上层消费），而不是只用后即弃。"""
    src = (REPO / "agents" / "online_skill_eval.py").read_text(encoding="utf-8")
    assert "runtime_statuses, certified_statuses = _materialize_status_map(skills)" in src
    # 两张表都应在后续被使用（至少各出现一次之外的定义处）
    assert src.count("runtime_statuses") >= 2
    assert src.count("certified_statuses") >= 2
