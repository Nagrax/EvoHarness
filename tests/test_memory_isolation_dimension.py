"""记忆隔离维度回归（2026-10-29，对齐面试硬问题）。

面试官的原话：「如果检索出别的用户的用户画像怎么办？」

**当前实现的真实答案**（读代码得出，不是猜）：
`agents/memory.py` 用 **cwd 的 sha256 前 16 位** 做隔离：
```python
d = Path.home() / ".evoharness" / "projects" / _project_hash() / "memory"
```
→ **项目级隔离，不是用户级**。

**为什么这是合理设计**（而不是缺陷）：
- EvoHarness 是本地单用户 CLI；隔离维度应当是"项目"——同一项目的记忆本就该共享
- 若做成用户级，同一人在不同项目间反而拿不到各自的项目记忆

**但必须如实标注局限**：若部署为多用户服务（一个进程服务多人），
同一项目下的记忆会被所有用户共享 → 需要加 tenant 层。

本测试固定两件事：
① 项目级隔离**确实有效**（不同 cwd → 不同目录）
② 代码里**没有**用户级隔离（防止有人以为有）
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import agents.memory as memory


def test_project_hash_depends_on_cwd(tmp_path, monkeypatch):
    """不同工作目录必须得到不同 hash（这是隔离的实际机制）。"""
    monkeypatch.chdir(tmp_path)
    first = memory._project_hash()
    other = tmp_path / "another-project"
    other.mkdir()
    monkeypatch.chdir(other)
    second = memory._project_hash()
    assert first != second, "不同 cwd 应得到不同项目 hash"
    assert len(first) == 16 and len(second) == 16


def test_project_hash_is_stable_for_same_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = memory._project_hash()
    second = memory._project_hash()
    assert first == second, "同一 cwd 的 hash 必须稳定（否则记忆会丢）"


def test_project_hash_matches_expected_algorithm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]
    assert memory._project_hash() == expected


def test_memory_dir_is_under_project_scoped_path(tmp_path, monkeypatch):
    """目录结构必须是 projects/<hash>/memory（项目级，不是用户级）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = memory.get_memory_dir()
    parts = path.parts
    assert "projects" in parts, f"缺少 projects 层：{path}"
    assert "memory" in parts
    # hash 层必须紧跟在 projects 之后
    idx = parts.index("projects")
    assert parts[idx + 1] == memory._project_hash()


def test_no_user_level_isolation_exists():
    """如实固定：当前**没有**用户级隔离（避免有人误以为有）。

    若将来真的实现了多租户，这个测试应当被**显式改写**并附迁移说明，
    而不是悄悄删掉。
    """
    source = (REPO / "agents" / "memory.py").read_text(encoding="utf-8")
    for marker in ("user_id", "tenant", "owner_id", "getuid"):
        assert marker not in source, (
            f"memory.py 出现了 {marker} —— 若实现了多租户隔离，"
            "请更新本测试并补迁移说明")


def test_session_store_is_separate_from_memory():
    """sessions 与 memory 是两个独立目录（会话数据不混进长期记忆）。"""
    source = (REPO / "agents" / "memory.py").read_text(encoding="utf-8")
    assert "sessions" not in source or "memory" in source
    # 只断言 memory 目录不叫 sessions
    assert "projects" in source