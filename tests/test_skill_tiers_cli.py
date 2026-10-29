"""部署分区 CLI 回归（2026-10-29，修复"只能 curl"的可用性缺口）。

背景：全栈评测发现 `/api/skills/tiers` 与 `/api/skills/tiers/action` 既无前端入口、
`skill_tiers.py` 也无 CLI → 运维想晋级/回退只能手写 curl。
本 CLI 复用库函数（不复制业务逻辑），所以重点验证：
① 命令真的改变分区 ② 变更写进审计日志 ③ 隔离 HOME 下不碰真实技能库
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agents import skill_tiers_cli as cli


@pytest.fixture()
def isolated_home(monkeypatch):
    home = Path(tempfile.mkdtemp(prefix="evo-tier-cli-"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(home)
    from agents import skill_tiers

    skill_tiers.reset_tier_cache()
    yield home
    skill_tiers.reset_tier_cache()
    shutil.rmtree(home, ignore_errors=True)


def test_doctor_reports_paths(isolated_home, capsys):
    assert cli.main(["doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "tiersPath" in payload and "auditPath" in payload
    assert payload["degraded"] in (True, False, None)


def test_promote_changes_tier_and_audits(isolated_home, capsys):
    from agents import skill_tiers

    assert cli.main(["promote", "cli-demo-skill", "--to", "staging"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("ok", True) is True
    skill_tiers.reset_tier_cache()
    assert skill_tiers.get_tier("cli-demo-skill") == skill_tiers.TIER_STAGING

    audit = Path(skill_tiers.tier_audit_path())
    assert audit.is_file(), "变更必须写审计日志"
    rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(r.get("skill") == "cli-demo-skill" for r in rows), "审计里应有该技能"


def test_rollback_returns_to_experimental(isolated_home, capsys):
    from agents import skill_tiers

    cli.main(["promote", "cli-demo-2", "--to", "staging"])
    capsys.readouterr()
    assert cli.main(["rollback", "cli-demo-2", "--reason", "drill"]) == 0
    capsys.readouterr()
    skill_tiers.reset_tier_cache()
    assert skill_tiers.get_tier("cli-demo-2") == skill_tiers.TIER_EXPERIMENTAL


def test_list_is_json_serializable(isolated_home, capsys):
    cli.main(["promote", "cli-demo-3", "--to", "core"])
    capsys.readouterr()
    assert cli.main(["list", "--json"]) == 0
    mapping = json.loads(capsys.readouterr().out)
    assert isinstance(mapping, dict)
    assert mapping.get("cli-demo-3") == "core"


def test_show_reports_single_skill(isolated_home, capsys):
    cli.main(["promote", "cli-demo-4", "--to", "staging"])
    capsys.readouterr()
    assert cli.main(["show", "cli-demo-4"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skill"] == "cli-demo-4"
    assert payload["tier"] == "staging"


def test_cli_reuses_library_not_duplicates_logic():
    """CLI 不得自己实现分区读写——必须走库函数（防两条路径漂移）。"""
    src = (REPO / "agents" / "skill_tiers_cli.py").read_text(encoding="utf-8")
    for fn in ("manual_promote", "rollback", "get_tier", "get_tier_map"):
        assert fn in src, f"CLI 应复用 {fn}"
    assert "deployment_tiers.json" not in src, "CLI 不应硬编码存储路径"