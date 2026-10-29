"""部署分区回归测试：experimental/staging/core 三分区与五状态机正交叠加。

覆盖 agents/skill_tiers.py 及四个挂载点：
- 写入即实验区：create_skill/evolve_skill 产物 tier=experimental（project+user 两级），手写缺省 core
- 注入标注：experimental 带 "[experimental:" 来源标注，与 provisional 标注的组合矩阵
  （本仓第一份标注断言：grep -rn provisional tests/ 在本文件之前零匹配）
- fork 禁令扩圈（分发点动态读 tier）+ 保守注入模式（env 每次调用时读）
- 晋升钩子：healthy+champion 门双断言、champion 拦截、干跑 fail-closed
- 一键回退（回退与断言之间严禁 reset 缓存——防把"烤缓存"bug 掩盖成绿）
- 正交性：写 tier 不触碰 status_map.json；tier 不影响检索得分
- repair 不改 tier（审计记 kind）；原子写无残留；损坏按缺省读+审计重放可复原
- CLI 人工操作入口（list/promote/rollback/core）

沙箱纪律（对齐 test_online_skill_ir_gate.py）：monkeypatch.chdir + Path.home 补丁 +
前后 reset_skill_cache()/reset_tier_cache()。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import skill_tiers
from agents.online_skill_eval import evaluate_online_skill_evolution_async
from agents.skill_evolution import get_evolution_dir
from agents.skills import (
    create_skill,
    evolve_skill,
    format_retrieved_skill_context,
    reset_skill_cache,
    retrieve_relevant_skills,
)

ANCHOR_EXP = "zzexpanchor"
ANCHOR_CORE = "zzcoreanchor"


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    reset_skill_cache()
    skill_tiers.reset_tier_cache()
    yield tmp_path
    reset_skill_cache()
    skill_tiers.reset_tier_cache()


def _hand_write_skill(root: Path, name: str, *, anchor: str = ANCHOR_CORE) -> Path:
    skill_dir = root / ".evoharness" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: hand written {anchor}\n"
        f"when-to-use: use when {anchor}\n---\n\nBody mentions {anchor}.\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_status_map(statuses: dict[str, str]) -> None:
    path = get_evolution_dir() / "online-eval" / "status_map.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"generated_at": "t", "statuses": statuses, "certified_statuses": statuses},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # skill_status 按 mtime 缓存：显式前移时间戳，保证同进程内连续写入必刷新。
    bump = time.time() + 3600
    os.utime(path, (bump, bump))


def _read_audit() -> list[dict]:
    path = skill_tiers.tier_audit_path()
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _promotion_report(name: str, status: str, *, promoted: bool | None = None, include_promotion: bool = True) -> dict:
    artifacts: dict = {}
    if include_promotion:
        artifacts["promotion"] = {
            "promoted": promoted,
            "status": "active_champion" if promoted else "rejected",
            "min_score_delta": 0.01,
        }
    return {
        "generated_at": "2026-10-13T00:00:00Z",
        "thresholds": {
            "min_score_samples": 3,
            "min_promotion_tests": 1,
            "min_retrieved": 5,
            "min_used_rate": 0.2,
            "min_relevance_rate": 0.35,
            "min_rule_pass_rate": 0.8,
            "min_score_delta": 0.01,
        },
        "skills": [
            {
                "skill": name,
                "status": status,
                "reasons": [],
                "retrieved": 5,
                "relevant": 3,
                "used": 2,
                "used_rate": 0.4,
                "relevance_rate": 0.6,
                "replay": {"count": 3, "promotion_test": 1},
                "eval": {"pass_rate": 1.0, "hard_failures": 0},
                "artifacts": artifacts,
            }
        ],
    }


def test_evolution_writes_land_experimental(sandbox):
    res = create_skill(name="exp-project", description="d", instructions="i", when_to_use="w", target="project")
    assert res.get("ok") is True
    assert skill_tiers.get_tier("exp-project") == "experimental"

    res_user = create_skill(name="exp-user", description="d", instructions="i", target="user")
    assert res_user.get("ok") is True
    assert skill_tiers.get_tier("exp-user") == "experimental"

    evolved = evolve_skill("exp-project", lesson="durable lesson")
    assert evolved.get("ok") is True
    assert skill_tiers.get_tier("exp-project") == "experimental"

    _hand_write_skill(sandbox, "hand-core")
    reset_skill_cache()
    assert skill_tiers.get_tier("hand-core") == "core"

    rows = _read_audit()
    assert rows and rows[-1]["trigger"] == "evolution_write"


def test_experimental_injection_annotated(sandbox):
    # experimental + incubating（无 status_map，缺省 incubating）→ 双标注
    create_skill(name="exp-skill", description=f"d {ANCHOR_EXP}", instructions="i", when_to_use=f"use when {ANCHOR_EXP}")
    reset_skill_cache()
    text, top = format_retrieved_skill_context(f"do {ANCHOR_EXP} now")
    assert "[experimental:" in text and "[provisional:" in text
    assert top is not None and top.get("tier") == "experimental"

    # staging + incubating → 仅 provisional
    assert skill_tiers.manual_promote("exp-skill")["ok"] is True
    text, top = format_retrieved_skill_context(f"do {ANCHOR_EXP} now")
    assert "[provisional:" in text and "[experimental:" not in text
    assert top.get("tier") == "staging"

    # experimental + healthy → 仅 experimental 标注
    assert skill_tiers.rollback("exp-skill")["ok"] is True
    _write_status_map({"exp-skill": "healthy"})
    text, top = format_retrieved_skill_context(f"do {ANCHOR_EXP} now")
    assert "[experimental:" in text and "[provisional:" not in text
    assert top.get("status") == "healthy"

    # core 手写 → 无任何标注（core-skill 用独立锚点，避免与仍处实验区的 exp-skill 同召回；
    # 同时给 core-skill 写 healthy 状态，排除状态机轴的 provisional 干扰——正交性）
    _hand_write_skill(sandbox, "core-skill", anchor=ANCHOR_CORE)
    _write_status_map({"exp-skill": "healthy", "core-skill": "healthy"})
    reset_skill_cache()
    text, top = format_retrieved_skill_context(f"do {ANCHOR_CORE} now")
    assert "[experimental:" not in text and "[provisional:" not in text
    assert top.get("tier") == "core"


def test_experimental_fork_downgraded(sandbox):
    create_skill(name="fork-exp", description="d", instructions="i", context="fork")
    reset_skill_cache()
    reason = skill_tiers.fork_blocked_reason("fork-exp")
    assert "experimental" in reason

    # 动态读契约：晋升/回退后立即生效，期间不 reset 任何缓存。
    assert skill_tiers.manual_promote("fork-exp")["ok"] is True
    assert skill_tiers.fork_blocked_reason("fork-exp") == ""
    assert skill_tiers.rollback("fork-exp")["ok"] is True
    assert "experimental" in skill_tiers.fork_blocked_reason("fork-exp")

    # 手写 core 不拦；孵化期状态机门仍拦（provisional 组合）。
    _hand_write_skill(sandbox, "fork-core")
    _write_status_map({"fork-core": "incubating", "fork-exp": "incubating"})
    reset_skill_cache()
    assert skill_tiers.fork_blocked_reason("fork-core") == "trial period (status=incubating)"
    assert "trial period" in skill_tiers.fork_blocked_reason("fork-exp")


def test_conservative_mode_excludes_experimental(sandbox, monkeypatch):
    create_skill(name="cons-exp", description=f"d {ANCHOR_EXP}", instructions="i", when_to_use=f"w {ANCHOR_EXP}")
    _hand_write_skill(sandbox, "cons-core", anchor=ANCHOR_CORE)
    reset_skill_cache()

    monkeypatch.setenv("EVOHARNESS_TIER_EXPERIMENTAL_INJECT", "0")
    names = {h["name"] for h in retrieve_relevant_skills(f"q {ANCHOR_EXP} {ANCHOR_CORE}")}
    assert "cons-exp" not in names and "cons-core" in names

    # env 每次调用时读：同进程内翻转立即生效（非 import 期固化）。
    monkeypatch.setenv("EVOHARNESS_TIER_EXPERIMENTAL_INJECT", "1")
    names2 = {h["name"] for h in retrieve_relevant_skills(f"q {ANCHOR_EXP} {ANCHOR_CORE}")}
    assert {"cons-exp", "cons-core"} <= names2


def test_no_promotion_without_evidence(sandbox):
    create_skill(name="np-skill", description="d", instructions="i")
    for status in ("incubating", "watch", "unobserved", "pruned"):
        skill_tiers.apply_eval_promotions(_promotion_report("np-skill", status, promoted=True))
        assert skill_tiers.get_tier("np-skill") == "experimental"
    # 干跑形态：artifacts.promotion 缺失（write_artifacts=False）→ fail-closed 不晋升
    skill_tiers.apply_eval_promotions(_promotion_report("np-skill", "healthy", promoted=True, include_promotion=False))
    assert skill_tiers.get_tier("np-skill") == "experimental"
    assert not [r for r in _read_audit() if r.get("trigger") == "auto_evidence"]


def test_promotion_on_double_evidence(sandbox):
    create_skill(name="pr-skill", description="d", instructions="i")
    out = skill_tiers.apply_eval_promotions(_promotion_report("pr-skill", "healthy", promoted=True))
    assert out["promoted"] and out["promoted"][0]["skill"] == "pr-skill"
    assert skill_tiers.get_tier("pr-skill") == "staging"

    row = _read_audit()[-1]
    assert row["event"] == "tier_change"
    assert row["trigger"] == "auto_evidence"
    assert row["from_tier"] == "experimental" and row["to_tier"] == "staging"
    evidence = row["evidence"]
    assert evidence["thresholds"]["min_score_samples"] == 3
    assert evidence["thresholds"]["min_score_delta"] == 0.01
    assert evidence["champion_gate"]["promoted"] is True
    assert evidence["status_map_generated_at"] == "2026-10-13T00:00:00Z"
    assert evidence["score_samples"] == 3 and evidence["promotion_test_samples"] == 1

    # 已是 staging：重复评测不重复晋升
    out2 = skill_tiers.apply_eval_promotions(_promotion_report("pr-skill", "healthy", promoted=True))
    assert out2["promoted"] == []


def test_promotion_blocked_by_champion_gate(sandbox):
    create_skill(name="cg-skill", description="d", instructions="i")
    out = skill_tiers.apply_eval_promotions(_promotion_report("cg-skill", "healthy", promoted=False))
    assert skill_tiers.get_tier("cg-skill") == "experimental"
    assert any("champion gate" in s["why"] for s in out["skipped"])
    assert not [r for r in _read_audit() if r.get("trigger") == "auto_evidence"]


def test_one_click_rollback(sandbox):
    create_skill(name="rb-skill", description=f"d {ANCHOR_EXP}", instructions="i", when_to_use=f"w {ANCHOR_EXP}")
    reset_skill_cache()
    before = retrieve_relevant_skills(f"q {ANCHOR_EXP}")
    assert before and before[0]["name"] == "rb-skill"

    assert skill_tiers.manual_promote("rb-skill")["ok"] is True
    text, _ = format_retrieved_skill_context(f"q {ANCHOR_EXP}")
    assert "[experimental:" not in text

    # 一键回退：回退与断言之间严禁 reset_skill_cache / reset_tier_cache（防烤缓存 bug 被掩盖成绿）。
    assert skill_tiers.rollback("rb-skill", reason="regression")["ok"] is True
    after = retrieve_relevant_skills(f"q {ANCHOR_EXP}")
    text2, _ = format_retrieved_skill_context(f"q {ANCHOR_EXP}")
    assert "[experimental:" in text2
    assert after[0]["score"] == before[0]["score"]  # tier 不影响检索得分（正交性）

    # 回退只作用于 staging：experimental/core 上拒绝
    assert skill_tiers.rollback("rb-skill")["ok"] is False

    row = _read_audit()[-1]
    assert row["trigger"] == "manual_rollback" and row["actor"] == "user"
    assert row["evidence"]["reason"] == "regression"


def test_orthogonal_to_status_machine(sandbox):
    create_skill(name="orth-skill", description=f"d {ANCHOR_EXP}", instructions="i", when_to_use=f"w {ANCHOR_EXP}")
    reset_skill_cache()
    _write_status_map({"orth-skill": "incubating"})
    sm_path = get_evolution_dir() / "online-eval" / "status_map.json"
    before_bytes = sm_path.read_bytes()

    # 人工提前晋升：tier=staging 与 status=incubating 共存
    assert skill_tiers.manual_promote("orth-skill")["ok"] is True
    # 晋升钩子不碰 status_map；incubating 不满足 healthy 门 → tier 不变
    skill_tiers.apply_eval_promotions(_promotion_report("orth-skill", "incubating", promoted=True))
    assert skill_tiers.get_tier("orth-skill") == "staging"
    assert sm_path.read_bytes() == before_bytes

    text, top = format_retrieved_skill_context(f"q {ANCHOR_EXP}")
    assert "[provisional:" in text and "[experimental:" not in text
    assert top.get("tier") == "staging" and top.get("status") == "incubating"


def test_dry_run_never_promotes(sandbox):
    create_skill(name="dry-skill", description="d", instructions="i")
    reset_skill_cache()
    report = asyncio.run(evaluate_online_skill_evolution_async(write_report=False, write_artifacts=False))
    assert isinstance(report, dict)
    # 干跑也物化 status_map（与 tests/test_eval_rule_ir.py 的干跑事实一致），
    # 但钩子只认报告内 artifacts.promotion → fail-closed 不晋升。
    assert (get_evolution_dir() / "online-eval" / "status_map.json").is_file()
    assert skill_tiers.get_tier("dry-skill") == "experimental"
    assert not [r for r in _read_audit() if r.get("trigger") == "auto_evidence"]


def test_repair_preserves_tier(sandbox):
    skill_dir = sandbox / ".evoharness" / "skills" / "rep-core"
    skill_dir.mkdir(parents=True)
    # "中文内容" 的 GBK 字节：utf-8-sig 必然解码失败（encoding kind，确定性最高）
    (skill_dir / "SKILL.md").write_bytes("中文内容".encode("gbk"))
    reset_skill_cache()
    assert skill_tiers.get_tier("rep-core") == "core"

    from agents.skill_repair import apply_repair

    result = apply_repair(str(skill_dir / "SKILL.md"), "encoding")
    assert result.get("ok") is True
    assert skill_tiers.get_tier("rep-core") == "core"

    row = _read_audit()[-1]
    assert row["trigger"] == "repair" and row["kind"] == "encoding"
    assert row["from_tier"] == "core" and row["to_tier"] == "core"


def test_atomic_write_and_recovery(sandbox):
    create_skill(name="aw-skill", description="d", instructions="i")
    assert skill_tiers.get_tier("aw-skill") == "experimental"
    tiers_path = skill_tiers.deployment_tiers_path()
    assert tiers_path.is_file()
    assert not list(tiers_path.parent.glob("*.tmp"))  # 原子写无残留

    expected = dict(skill_tiers.get_tier_map())
    # E-03：损坏后不再静默回落为全员 core，而是按追加式审计自动恢复（最近有效快照）。
    tiers_path.write_text("{corrupted", encoding="utf-8")
    skill_tiers.reset_tier_cache()
    assert skill_tiers.get_tier_map() == expected
    assert skill_tiers.get_tier("aw-skill") == "experimental"  # 隔离被保住，未被降级为 core
    assert tiers_path.is_file() and not list(tiers_path.parent.glob("*.tmp"))  # 自愈写仍原子

    rows = [
        json.loads(l)
        for l in skill_tiers.tier_fault_path().read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    recovered = [r for r in rows if r["event"] == "tier_metadata_recovered"]
    assert recovered and "aw-skill" in recovered[-1]["affected"]
    assert recovered[-1]["recovery"]

    # 手动恢复路径仍一致。
    assert skill_tiers.restore_from_audit() == expected


def test_corrupt_or_invalid_metadata_never_downgrades_isolated_skill(sandbox):
    create_skill(name="iso-skill", description="d", instructions="i")
    assert skill_tiers.get_tier("iso-skill") == "experimental"
    tiers_path = skill_tiers.deployment_tiers_path()

    # 截断 JSON。
    tiers_path.write_text("{ truncated", encoding="utf-8")
    skill_tiers.reset_tier_cache()
    assert skill_tiers.get_tier("iso-skill") == "experimental"

    # 非法分区值：整表视为损坏（不得把 experimental 静默读成 core）。
    tiers_path.write_text(
        json.dumps({"version": 1, "tiers": {"iso-skill": "core", "x": "bogus"}}),
        encoding="utf-8",
    )
    skill_tiers.reset_tier_cache()
    assert skill_tiers.get_tier("iso-skill") == "experimental"


def test_missing_metadata_recovers_isolation(sandbox):
    create_skill(name="miss-skill", description="d", instructions="i")
    expected = skill_tiers.get_tier("miss-skill")
    assert expected == "experimental"

    skill_tiers.deployment_tiers_path().unlink()
    skill_tiers.reset_tier_cache()
    # 已初始化运行时文件丢失：从审计恢复，不得回落全员 core。
    assert skill_tiers.get_tier("miss-skill") == "experimental"


def test_process_restart_keeps_isolation(sandbox):
    create_skill(name="rst-skill", description="d", instructions="i")
    skill_tiers.deployment_tiers_path().write_text("{ truncated", encoding="utf-8")
    skill_tiers.reset_tier_cache()  # 等价于进程重启后重新读盘
    assert skill_tiers.get_tier("rst-skill") == "experimental"


def test_first_install_missing_file_keeps_core_default(sandbox):
    # 首次安装：无审计、无状态机产物 → 缺省全员 core（兼容行为保留，不得误判为降级）。
    assert skill_tiers.get_tier_map() == {}
    assert skill_tiers.get_tier("anything") == "core"
    assert skill_tiers.tier_state_degraded() is False


def test_degraded_isolation_when_metadata_lost_without_audit(sandbox):
    _hand_write_skill(sandbox, "hand-skill")
    _write_status_map({"hand-skill": "healthy"})  # 运行时已初始化（有状态机产物）
    tiers_path = skill_tiers.deployment_tiers_path()
    tiers_path.write_text("{corrupted", encoding="utf-8")
    skill_tiers.reset_tier_cache()

    assert skill_tiers.tier_state_degraded() is True
    # 无可重放审计 → 隔离态：未知 skill 不得被当作正式能力。
    assert skill_tiers.get_tier("hand-skill") == "experimental"
    rows = [
        json.loads(l)
        for l in skill_tiers.tier_fault_path().read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert any(r["event"] == "tier_metadata_lost_isolated" for r in rows)


def test_valid_file_missing_entry_does_not_unisolate(sandbox):
    """合法但缺条目的分区文件（也可由并发丢更新写出）不得把已隔离 skill 读成 core。"""
    create_skill(name="iso2", description="d", instructions="i")
    assert skill_tiers.get_tier("iso2") == "experimental"
    skill_tiers.deployment_tiers_path().write_text(
        json.dumps({"version": 1, "tiers": {}}), encoding="utf-8"
    )
    skill_tiers.reset_tier_cache()
    assert skill_tiers.get_tier("iso2") == "experimental"  # 审计作一致性底线
    rows = [
        json.loads(l)
        for l in skill_tiers.tier_fault_path().read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert any(r["event"] == "tier_file_audit_mismatch" for r in rows)


def test_degraded_blocks_auto_promotion(sandbox):
    create_skill(name="deg-skill", description="d", instructions="i")
    _write_status_map({"deg-skill": "healthy"})
    skill_tiers.tier_audit_path().unlink()  # 制造「损坏且无可重放审计」
    skill_tiers.deployment_tiers_path().write_text("{corrupted", encoding="utf-8")
    skill_tiers.reset_tier_cache()

    assert skill_tiers.tier_state_degraded() is True
    out = skill_tiers.apply_eval_promotions(_promotion_report("deg-skill", "healthy", promoted=True))
    assert out["promoted"] == []
    assert any("degraded" in s["why"] for s in out["skipped"])
    assert skill_tiers.get_tier("deg-skill") == "experimental"


def test_cli_tier_commands(sandbox, capsys):
    from agents.main import _run_skills_tier_command

    create_skill(name="cli-skill", description="d", instructions="i")
    _run_skills_tier_command(["list"])
    assert "cli-skill" in capsys.readouterr().out

    _run_skills_tier_command(["promote", "cli-skill"])
    assert skill_tiers.get_tier("cli-skill") == "staging"
    _run_skills_tier_command(["rollback", "cli-skill"])
    assert skill_tiers.get_tier("cli-skill") == "experimental"
    _run_skills_tier_command(["promote", "cli-skill"])
    _run_skills_tier_command(["core", "cli-skill"])
    assert skill_tiers.get_tier("cli-skill") == "core"

    # 非法迁移被拒：core 不可一键回退（回退只作用于 staging）
    with pytest.raises(SystemExit):
        _run_skills_tier_command(["rollback", "cli-skill"])
