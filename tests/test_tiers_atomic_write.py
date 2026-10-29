"""部署分区表原子写回归：唯一 tmp 名 + PermissionError 退避重试。

真实事故（2026-10-26 门禁 loop-gate17 实测）：
`test_cli_tier_commands` 在门禁里偶发 `PermissionError`，单跑与重跑全量都绿——竞态特征。
根因（agents/skill_tiers.py::_write_tiers_atomic 旧实现）：
  1. 固定 tmp 名（`path.name + ".tmp"`）：两个并发写者互撞；
  2. `os.replace` 无重试：Windows 上新写文件常被杀毒/索引器短暂占用，replace 直接抛错，
     分区表写入丢失 = "晋级凭空没生效"，用户侧无任何提示。

修复：唯一 tmp 名（对齐 agents/session.py 的 atomic_write_text）+ 5 次短退避重试；
重试用尽才抛错（调用方 set_tier 记审计失败），不静默丢写。
"""

from __future__ import annotations

import inspect
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import skill_tiers


def test_uses_unique_tmp_name_not_fixed():
    """必须用唯一 tmp 名：固定 `.tmp` 名在并发下互撞。"""
    source = inspect.getsource(skill_tiers._write_tiers_atomic)
    assert "time.time_ns()" in source or "uuid" in source, (
        "tmp 文件名必须含 pid/纳秒等唯一成分，固定名会在并发写时互撞")
    assert '.with_name(path.name + ".tmp")' not in source, "不允许再用固定 tmp 名"


def test_permission_error_is_retried_not_fatal(tmp_path, monkeypatch):
    """首次 os.replace 抛 PermissionError 时必须退避重试而不是直接失败。"""
    path = tmp_path / "deployment_tiers.json"
    attempts = {"n": 0}
    real_replace = skill_tiers.os.replace

    def flaky_replace(src, dst):
        attempts["n"] += 1
        if attempts["n"] <= 2:  # 前两次模拟被占用
            raise PermissionError("WinError 32: file in use (simulated)")
        return real_replace(src, dst)

    monkeypatch.setattr(skill_tiers.os, "replace", flaky_replace)
    monkeypatch.setattr(skill_tiers.time, "sleep", lambda _s: None)  # 测试不真等

    skill_tiers._write_tiers_atomic(path, {"version": 1, "tiers": {"s1": "staging"}})

    assert attempts["n"] == 3, f"应重试到第 3 次成功，实际尝试 {attempts['n']} 次"
    assert json.loads(path.read_text(encoding="utf-8"))["tiers"]["s1"] == "staging"


def test_exhausted_retries_raise_instead_of_silent_loss(tmp_path, monkeypatch):
    """重试用尽必须抛错（调用方记审计失败），绝不静默丢写。"""
    path = tmp_path / "deployment_tiers.json"

    def always_busy(_src, _dst):
        raise PermissionError("WinError 32: persistent lock (simulated)")

    monkeypatch.setattr(skill_tiers.os, "replace", always_busy)
    monkeypatch.setattr(skill_tiers.time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        skill_tiers._write_tiers_atomic(path, {"version": 1, "tiers": {}})
    assert not list(tmp_path.glob("*.tmp")), "重试用尽后不得残留 tmp 文件"


def test_concurrent_writers_all_land(tmp_path):
    """多线程并发写：唯一 tmp 名下每个写者都成功，最终文件是合法 JSON。"""
    path = tmp_path / "deployment_tiers.json"
    errors: list[Exception] = []

    def writer(index: int) -> None:
        try:
            skill_tiers._write_tiers_atomic(
                path, {"version": 1, "tiers": {f"s{index}": "experimental"}})
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"并发写入不应失败：{errors[:2]}"
    # 最终文件必然是 8 个写者之一的完整内容（原子替换保证不撕裂）
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["tiers"]) == 1
    assert not list(tmp_path.glob("*.tmp")), "并发写后不得残留 tmp"


def test_set_tier_still_works_end_to_end(tmp_path, monkeypatch):
    """唯一写入口在修复后行为不变（tier 落盘 + 审计行）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    skill_tiers.reset_tier_cache()
    result = skill_tiers.set_tier("s-x", skill_tiers.TIER_STAGING, trigger="test", actor="user")
    assert result["ok"] is True
    assert skill_tiers.get_tier("s-x") == skill_tiers.TIER_STAGING
    audit = skill_tiers.tier_audit_path()
    assert audit.is_file() and "tier_change" in audit.read_text(encoding="utf-8")
