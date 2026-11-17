"""DE-18 接缝回归：原生学习裁决优先级、制品晋级一致性、内容恢复与发布事务。

覆盖（对应 tasks.json DE-18 acceptance_scenarios 与裁决文档 §7/§8/§10）：
- exact identity 命中不再把 LLM 裁决的 revise 强制改成 merge（明确矛盾走替换，
  旧内容留 history 快照，无矛盾 union）；同名去重 merge / 相似补充 merge 保持现语义
- 评测报告绑定 active/candidate snapshot hash 与协议指纹；候选 A 胜出而 active 已漂移
  成 B 时 apply_eval_promotions 拒绝（active_drift），hash 一致时正常晋级且证据可见
- rollback 只降 tier、restore_content 只恢复内容（两条记录分开）；投影文件拒绝恢复；
  恢复后检索缓存立即反映旧正文
- evolve_skill_file / publish_skill_bundle 的 expected_version CAS：过期版本被拒
  （source_changed），现文件不被破坏
- publish_skill_bundle 最小发布事务：回执 hash/版本正确、审计与 history 有行、
  发布后回 experimental、不授予对外发信授权

沙箱纪律（对齐 test_skill_deployment_tiers.py / test_online_skill_ir_gate.py）：
monkeypatch.chdir + Path.home 补丁 + 前后 reset_skill_cache()/reset_tier_cache()。
「新 experimental 无 score 不被认证加权惩罚、禁 fork」场景由
tests/test_skill_deployment_tiers.py::test_experimental_injection_annotated /
test_experimental_fork_downgraded 既有回归覆盖，此处不重复。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import online_skill_eval as oe
from agents import online_skill_evolution as ose
from agents import skill_evolution as se
from agents import skill_tiers as st
from agents.skills import (
    create_skill,
    discover_skills,
    evolve_skill,
    reset_skill_cache,
)
from evoharness.knowledge.release import _canonical_bundle_hash, publish_skill_bundle

EVOLUTION_DIR_ENVS = ("EVOHARNESS_EVOLUTION_DIR", "EVOHARNESS_SKILLS_ROOT")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for env in EVOLUTION_DIR_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "off")
    reset_skill_cache()
    st.reset_tier_cache()
    yield tmp_path
    reset_skill_cache()
    st.reset_tier_cache()


# ── 通用小工具 ──────────────────────────────────────────────────


def _skill_path(root: Path, name: str) -> Path:
    return root / ".evoharness" / "skills" / name / "SKILL.md"


def _read_skill_meta(root: Path, name: str) -> dict:
    from agents.frontmatter import parse_frontmatter

    raw = _skill_path(root, name).read_text(encoding="utf-8-sig")
    return parse_frontmatter(raw).meta


def _active_content_hash(name: str) -> str:
    """当前 active 制品的内容指纹（报告侧与晋级门同用 skill_content_hash 口径）。"""
    for skill in discover_skills():
        if skill.name == name:
            return se.skill_content_hash(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "when_to_use": skill.when_to_use or "",
                    "instructions": skill.prompt_template,
                }
            )
    return ""


def _history_rows(root: Path, name: str) -> list[dict]:
    path = root / ".evoharness" / "skill-evolution" / "history" / f"{se._safe_skill_slug(name)}.jsonl"
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


def _jsonl_rows(path: Path) -> list[dict]:
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


def _tier_audit_rows(root: Path) -> list[dict]:
    return _jsonl_rows(root / ".evoharness" / "skill-evolution" / "tier_audit.jsonl")


def _last_provenance_row(root: Path) -> dict:
    path = root / ".evoharness" / "skill-evolution" / "online_provenance.jsonl"
    rows = _jsonl_rows(path)
    assert rows, f"provenance 未落盘: {path}"
    return rows[-1]


def _promotion_report(
    name: str,
    status: str,
    *,
    promoted: bool = True,
    artifacts_extra: dict | None = None,
) -> dict:
    artifacts: dict = {
        "promotion": {
            "promoted": promoted,
            "status": "active_champion" if promoted else "rejected",
            "min_score_delta": 0.01,
        }
    }
    if artifacts_extra:
        artifacts.update(artifacts_extra)
    return {
        "generated_at": "2026-11-14T00:00:00Z",
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


def _extract_obj(**overrides) -> dict:
    obj = {
        "name": "pref-output",
        "description": "Output format preference",
        "when_to_use": "用户要求输出结果时",
        "instructions": "输出一律用 Markdown 表格，不要用 JSON。",
        "evidence": "用户说：以后不要 JSON，改用 Markdown 表格",
        "tags": ["output"],
    }
    obj.update(overrides)
    return obj


def _extract_json(obj: dict) -> str:
    return json.dumps({"skills": [obj]}, ensure_ascii=False)


def _decision_json(**overrides) -> str:
    decision = {"action": "add", "reason": "durable preference"}
    decision.update(overrides)
    return json.dumps(decision, ensure_ascii=False)


def _make_side_query(responses: list[str]):
    calls: list[tuple[str, str]] = []

    async def side_query(system: str, user: str) -> str:
        calls.append((system, user))
        index = len(calls) - 1
        return responses[index] if index < len(responses) else responses[-1]

    side_query.calls = calls
    return side_query


def _run_ingest(side_query) -> dict:
    return asyncio.run(
        ose.online_ingest(
            messages=[
                {"role": "user", "content": "以后输出结果一律用 Markdown 表格，不要用 JSON。"},
                {"role": "assistant", "content": "好的，后续输出我会用 Markdown 表格。"},
            ],
            side_query=side_query,
        )
    )


# ── 场景 1：exact identity 的裁决优先级（明确矛盾 revise > 同名 merge > add/discard 收敛） ──


def test_exact_identity_contradiction_revise_replaces(sandbox):
    """同名偏好明确反转：revise 裁决被尊重走替换，旧内容留 history 快照，无矛盾 union。"""
    root = sandbox
    create_skill(
        name="pref-output",
        description="Output format preference",
        instructions="输出一律用 JSON 格式。",
        when_to_use="用户要求输出结果时",
    )
    reset_skill_cache()

    obj = _extract_obj(
        name="pref-output",
        description="Output format preference",
        when_to_use="用户要求输出结果时",
        instructions="输出一律用 Markdown 表格，不要用 JSON。",
    )
    side_query = _make_side_query(
        [
            _extract_json(obj),
            _decision_json(
                action="revise",
                target_skill="pref-output",
                revised_instructions="输出一律用 Markdown 表格，绝不输出 JSON。",
            ),
        ]
    )
    result = _run_ingest(side_query)
    assert result.get("ok") is True, result
    assert result["action"] == "revise", result
    body = _skill_path(root, "pref-output").read_text(encoding="utf-8-sig")
    # 新规则落盘、旧规则整体替换：不出现「既要 JSON 又要 Markdown」的自相矛盾 union
    assert "Markdown 表格" in body
    assert "绝不输出 JSON" in body
    assert "JSON 格式" not in body
    assert _read_skill_meta(root, "pref-output")["version"] == "0.1.1"

    # 旧内容完整保留在 history 快照（旧快照不丢）
    snapshots = [r for r in _history_rows(root, "pref-output") if r.get("event") == "snapshot"]
    assert snapshots, "revise 前没有写 history 快照"
    last_snapshot = snapshots[-1]
    assert "输出一律用 JSON 格式" in last_snapshot["content"]
    assert last_snapshot["version"] == "0.1.0"

    # provenance 如实记 revise（不冒充 merge）
    row = _last_provenance_row(root)
    assert row["action"] == "revise"
    assert row["result"]["action"] == "revise"


def test_exact_identity_add_and_discard_still_dedup_merge(sandbox):
    """同名命中时 add/discard 仍按现语义收敛为去重 merge，不产生同名双 skill。"""
    root = sandbox
    create_skill(
        name="pref-output",
        description="Output format preference",
        instructions="输出一律用 JSON 格式。",
        when_to_use="用户要求输出结果时",
    )
    reset_skill_cache()
    obj = _extract_obj(name="pref-output", description="Output format preference", when_to_use="用户要求输出结果时")
    side_query = _make_side_query([_extract_json(obj), _decision_json(action="add")])
    result = _run_ingest(side_query)
    assert result.get("ok") is True, result
    assert result["action"] == "merge", result  # 同名去重 merge 优先于 add
    skill_dirs = [p.name for p in (root / ".evoharness" / "skills").iterdir() if p.is_dir()]
    assert skill_dirs == ["pref-output"], skill_dirs  # 未创建重复 skill
    assert _last_provenance_row(root)["action"] == "merge"

    # discard 同样收敛为去重 merge
    side_query2 = _make_side_query([_extract_json(obj), _decision_json(action="discard")])
    result2 = _run_ingest(side_query2)
    assert result2["action"] == "merge", result2
    skill_dirs2 = [p.name for p in (root / ".evoharness" / "skills").iterdir() if p.is_dir()]
    assert skill_dirs2 == ["pref-output"], skill_dirs2


def test_same_name_merge_decision_keeps_merged_body(sandbox):
    """同名 merge 裁决照常工作：LLM 的 merged_instructions 合并体落盘，去重不改名。"""
    root = sandbox
    create_skill(
        name="pref-output",
        description="Output format preference",
        instructions="输出一律用 JSON 格式。",
        when_to_use="用户要求输出结果时",
    )
    reset_skill_cache()
    obj = _extract_obj(
        name="pref-output",
        description="Output format preference",
        when_to_use="用户要求输出结果时",
        instructions="日期一律用 ISO 8601 格式。",
    )
    side_query = _make_side_query(
        [
            _extract_json(obj),
            _decision_json(
                action="merge",
                target_skill="pref-output",
                merged_instructions="JSON 输出规则保留；新增：日期一律用 ISO 8601 格式。",
            ),
        ]
    )
    result = _run_ingest(side_query)
    assert result.get("ok") is True and result["action"] == "merge", result
    body = _skill_path(root, "pref-output").read_text(encoding="utf-8-sig")
    assert "JSON 输出规则保留" in body and "ISO 8601" in body  # 合并体同时含旧规则与新规则
    assert _last_provenance_row(root)["action"] == "merge"


def test_similar_supplement_add_still_merges(sandbox):
    """相似补充：非同名候选 add 且相似度命中时仍收敛为 merge（现语义保持）。"""
    root = sandbox
    create_skill(
        name="iso-date-format",
        description="ISO date output convention",
        instructions="日期一律用 ISO 8601 格式输出，含时间时带时区。",
        when_to_use="用户要求日期输出时",
        tags=["date"],
    )
    reset_skill_cache()
    obj = _extract_obj(
        name="iso-date-format-note",
        description="ISO date output convention note",
        when_to_use="用户要求日期输出",
        instructions="日期一律用 ISO 8601 格式输出。",
        tags=["date"],
    )
    side_query = _make_side_query([_extract_json(obj), _decision_json(action="add")])
    result = _run_ingest(side_query)
    assert result.get("ok") is True, result
    assert result["action"] == "merge", result  # 相似补充 merge（相似度 >= 0.55 才触发）
    assert result.get("skill") == "iso-date-format", result


# ── 场景 2：候选 A 胜出而 active 漂移成 B → 拒绝晋级（active_drift），hash 证据可见 ──


def test_promotion_rejects_drifted_active_and_promotes_on_match(sandbox):
    create_skill(name="drift-skill", description="d", instructions="被评测版本 A 的规则正文。")
    reset_skill_cache()
    hash_a = _active_content_hash("drift-skill")
    assert hash_a

    # 评测之后、晋级之前，active 被在线演化改成 B（真实漂移序列）
    evolved = evolve_skill("drift-skill", lesson="new user decision", instructions="用户新决定版本 B 的规则正文。")
    assert evolved.get("ok") is True
    hash_b = _active_content_hash("drift-skill")
    assert hash_b != hash_a

    report = _promotion_report(
        "drift-skill",
        "healthy",
        promoted=True,
        artifacts_extra={
            "active_snapshot_hash": hash_a,  # 报告绑定的是被评测的 A
            "candidate_snapshot_hash": hash_a,
            "eval_protocol_fingerprint": "proto-fp-1",
        },
    )
    out = st.apply_eval_promotions(report)
    assert out["promoted"] == []
    drift = [s for s in out["skipped"] if "active_drift" in str(s.get("why"))]
    assert drift, out["skipped"]
    # 两侧 hash 都在跳过原因里可见
    assert drift[0]["active_snapshot_hash"] == hash_a
    assert drift[0]["current_active_hash"] == hash_b
    assert st.get_tier("drift-skill") == "experimental"  # 绝不拿 A 的成绩给 B 晋级

    # active hash 一致时正常晋级，且 hash 与协议指纹进入审计证据
    report_ok = _promotion_report(
        "drift-skill",
        "healthy",
        promoted=True,
        artifacts_extra={
            "active_snapshot_hash": hash_b,
            "candidate_snapshot_hash": "candidate-hash-xyz",
            "eval_protocol_fingerprint": "proto-fp-2",
        },
    )
    out2 = st.apply_eval_promotions(report_ok)
    assert out2["promoted"] and out2["promoted"][0]["skill"] == "drift-skill"
    assert st.get_tier("drift-skill") == "staging"
    tier_rows = _tier_audit_rows(sandbox)
    evidence_rows = [r for r in tier_rows if r.get("trigger") == "auto_evidence"]
    assert evidence_rows, tier_rows
    evidence = evidence_rows[-1]["evidence"]
    assert evidence["active_snapshot_hash"] == hash_b
    assert evidence["candidate_snapshot_hash"] == "candidate-hash-xyz"
    assert evidence["eval_protocol_fingerprint"] == "proto-fp-2"


def test_promotion_drift_gate_uses_same_hash_definition_as_report(sandbox):
    """晋级门与评测报告用同一 hash 算法：报告里写 skill_content_hash(snapshot) 必须能对上。"""
    create_skill(name="hash-sync-skill", description="d", instructions="同一份正文。")
    reset_skill_cache()
    from agents.skill_evolution import skill_content_hash

    snapshot = oe._active_skill_snapshots()["hash-sync-skill"]
    report = _promotion_report(
        "hash-sync-skill",
        "healthy",
        promoted=True,
        artifacts_extra={"active_snapshot_hash": skill_content_hash(snapshot)},
    )
    out = st.apply_eval_promotions(report)
    assert out["promoted"], out["skipped"]  # 同算法哈希一致 → 不误报漂移


def test_legacy_report_without_hashes_keeps_promotion_behavior(sandbox):
    """旧报告（无 active_snapshot_hash 字段）保持现行为——兼容既有报告形态。"""
    create_skill(name="legacy-skill", description="d", instructions="i")
    out = st.apply_eval_promotions(_promotion_report("legacy-skill", "healthy", promoted=True))
    assert out["promoted"] and out["promoted"][0]["skill"] == "legacy-skill"
    assert st.get_tier("legacy-skill") == "staging"


# ── 场景 2b：评测产物绑定 active/candidate hash 与协议指纹 ──


def test_eval_artifacts_bind_snapshot_and_protocol(sandbox):
    create_skill(name="bind-skill", description="d", instructions="绑定正文。")
    reset_skill_cache()
    snapshot = oe._active_skill_snapshots()["bind-skill"]
    rules = [{"rule_id": "r1", "label": "nonempty", "kind": "programmatic", "hard": True}]
    rule_summary = {
        "outcomes": [],
        "pass_rate": 1.0,
        "total_score": 1.0,
        "average_score": 1.0,
        "hard_failures": 0,
        "passed_rules": 1,
        "outcome_count": 1,
    }
    out = oe._persist_eval_artifacts(
        skill_name="bind-skill",
        snapshot=snapshot,
        replay_pool=[],
        rules=rules,
        rule_summary=rule_summary,
        status="incubating",
        reasons=["incubating"],
        candidate_bundle={},
    )
    expected_fp = oe._eval_protocol_fingerprint(rules, "history_latest_assistant")
    assert out["active_snapshot_hash"] == se.skill_content_hash(snapshot)
    assert out["candidate_snapshot_hash"] == out["active_snapshot_hash"]  # 无候选变体时候选=现任
    assert out["eval_protocol_fingerprint"] == expected_fp

    run_dir = Path(out["run_dir"])
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["active_snapshot_hash"] == out["active_snapshot_hash"]
    assert summary["candidate_snapshot_hash"] == out["candidate_snapshot_hash"]
    assert summary["eval_protocol_fingerprint"] == expected_fp
    assert summary["candidate"]["snapshot_hash"] == out["active_snapshot_hash"]
    assert summary["artifacts"]["active_snapshot_hash"] == out["active_snapshot_hash"]

    eval_spec = json.loads(
        (Path(out["eval_spec_file"])).read_text(encoding="utf-8")
    )
    assert eval_spec["eval_protocol_fingerprint"] == expected_fp
    assert eval_spec["active_snapshot_hash"] == out["active_snapshot_hash"]


def test_eval_artifacts_best_variant_carries_candidate_hash(sandbox):
    """best_variant 胜出时：晋级候选 hash 换成变体制品 hash（与现任 active 区分）。"""
    snapshot = {
        "name": "variant-skill",
        "description": "d",
        "when_to_use": "w",
        "instructions": "现任正文。",
    }
    best_variant = {
        "variant_id": "candidate-abc",
        "label": "llm_patch",
        "mutation_type": "instructions",
        "snapshot": {"name": "variant-skill", "description": "d", "when_to_use": "w", "instructions": "候选 A 的正文。"},
    }
    bundle = {
        "best_variant": best_variant,
        "best_dev_summary": {"average_score": 1.0, "hard_failures": 0},
        "best_test_summary": {
            "variant_id": "candidate-abc",
            "label": "llm_patch",
            "average_score": 1.0,
            "hard_failures": 0,
            "total_score": 1.0,
            "passed_rules": 1,
            "rule_pass_rate": 1.0,
        },
        "candidate_variants": [],
        "outputs": [],
        "judgments": [],
    }
    rules = [{"rule_id": "r1", "label": "nonempty", "kind": "programmatic", "hard": True}]
    rule_summary = {
        "outcomes": [],
        "pass_rate": 1.0,
        "total_score": 0.5,
        "average_score": 0.5,
        "hard_failures": 0,
        "passed_rules": 1,
        "outcome_count": 2,
    }
    out = oe._persist_eval_artifacts(
        skill_name="variant-skill",
        snapshot=snapshot,
        replay_pool=[],
        rules=rules,
        rule_summary=rule_summary,
        status="healthy",
        reasons=[],
        candidate_bundle=bundle,
    )
    assert out["active_snapshot_hash"] == se.skill_content_hash(snapshot)
    # 候选 hash 绑定 best_variant 的制品，而不是被评测的现任
    assert out["candidate_snapshot_hash"] == se.skill_content_hash(best_variant["snapshot"])
    assert out["candidate_snapshot_hash"] != out["active_snapshot_hash"]


# ── 场景 3：内容恢复与 tier 回退分开 ──


def test_rollback_only_changes_tier_restore_only_changes_content(sandbox):
    create_skill(name="cycle-skill", description="d", instructions="v0.1.0 的正文。")
    evolve_skill("cycle-skill", lesson="first change", instructions="v0.1.1 的正文。")
    evolve_skill("cycle-skill", lesson="second change", instructions="v0.1.2 的正文。")
    assert st.manual_promote("cycle-skill")["ok"] is True
    assert st.get_tier("cycle-skill") == "staging"

    file = _skill_path(sandbox, "cycle-skill")
    content_before_rollback = file.read_text(encoding="utf-8-sig")

    # rollback 只降 tier：内容一字不动
    rolled = st.rollback("cycle-skill", reason="regression")
    assert rolled.get("ok") is True
    assert st.get_tier("cycle-skill") == "experimental"
    assert file.read_text(encoding="utf-8-sig") == content_before_rollback
    assert _read_skill_meta(sandbox, "cycle-skill")["version"] == "0.1.2"

    # restore_content 只恢复内容：tier 保持 experimental 不动，正文/版本回到最近快照
    restored = st.restore_content("cycle-skill")
    assert restored.get("ok") is True, restored
    assert st.get_tier("cycle-skill") == "experimental"  # tier 未被恢复动作改写
    assert restored["restored_version"] == "0.1.1"  # 最近快照 = 最后一次 evolve 之前
    assert restored["previous_version"] == "0.1.2"
    # 实际加载内容与回执一致（hash 口径）
    restored_bytes = file.read_text(encoding="utf-8-sig")
    assert "v0.1.1 的正文" in restored_bytes and "v0.1.2 的正文" not in restored_bytes
    assert restored["restored_sha256"] == hashlib.sha256(restored_bytes.encode("utf-8")).hexdigest()

    # 缓存随恢复刷新：不做任何手动 reset，检索/发现立即反映旧正文
    names = {s.name: s.prompt_template for s in discover_skills()}
    assert "v0.1.1 的正文" in names["cycle-skill"]

    # 审计分开记录：restore 不产生 tier_change 行（tier 未变），content_restore 行明确 tier_changed=False
    audit_rows = _tier_audit_rows(sandbox)
    restore_rows = [r for r in audit_rows if r.get("event") == "content_restore"]
    assert restore_rows and restore_rows[-1]["skill"] == "cycle-skill"
    assert restore_rows[-1]["tier_changed"] is False
    assert restore_rows[-1]["tier_at_restore"] == "experimental"
    tier_changes = [r for r in audit_rows if r.get("event") == "tier_change"]
    assert not [r for r in tier_changes if r.get("trigger") == "content_restore"]
    usage_rows = _jsonl_rows(sandbox / ".evoharness" / "skill-evolution" / "usage.jsonl")
    assert any(r.get("event") == "content_restore" for r in usage_rows)

    # 无快照可恢复时报明确错误
    fresh = create_skill(name="no-history-skill", description="d", instructions="i")
    assert fresh.get("ok") is True
    empty = st.restore_content("no-history-skill")
    assert empty.get("ok") is False and empty["error"] == "no_history_snapshot", empty


def test_restore_content_rejects_projection(sandbox):
    try:
        projection = se.write_managed_projection(
            skills_root=sandbox / ".evoharness" / "skills",
            evolution_root=sandbox / ".evoharness" / "skill-evolution",
            name="proj-note",
            description="projection note",
            instructions="投影正文。",
            when_to_use="w",
            scope={"tenant": "t1", "owner": "o1"},
            source_revisions=[],
            actor="test",
        )
        assert projection.get("ok") is True, projection
        file = _skill_path(sandbox, "proj-note")
        before = file.read_text(encoding="utf-8-sig")
        refused = st.restore_content("proj-note")
        assert refused.get("ok") is False and refused["error"] == "managed_projection_source_owned", refused
        assert file.read_text(encoding="utf-8-sig") == before  # 投影正文不被恢复入口改写
    finally:
        reset_skill_cache()
        st.reset_tier_cache()


def test_restore_content_cas_expected_version(sandbox):
    create_skill(name="cas-restore", description="d", instructions="v0.1.0 的正文。")
    evolve_skill("cas-restore", lesson="change", instructions="v0.1.1 的正文。")
    # CAS：调用方以为 active 还是 0.1.0，实际已被在线演化改成 0.1.1 → 拒绝
    refused = st.restore_content("cas-restore", expected_version="0.1.0")
    assert refused.get("ok") is False and refused["error"] == "source_changed", refused
    assert _read_skill_meta(sandbox, "cas-restore")["version"] == "0.1.1"  # 现文件不被破坏
    # CAS 对上时正常恢复
    ok = st.restore_content("cas-restore", expected_version="0.1.1")
    assert ok.get("ok") is True, ok
    assert _read_skill_meta(sandbox, "cas-restore")["version"] == "0.1.0"


# ── 场景 4：evolve_skill_file 的 expected_version CAS ──


def test_evolve_skill_file_expected_version_cas(sandbox):
    created = create_skill(name="cas-evolve", description="d", instructions="初始正文。")
    assert created.get("ok") is True
    file = _skill_path(sandbox, "cas-evolve")
    before = file.read_text(encoding="utf-8-sig")

    # 版本一致 → 正常写入
    ok = se.evolve_skill_file(
        skill_name="cas-evolve", lesson="change", instructions="新正文。", expected_version="0.1.0"
    )
    assert ok.get("ok") is True, ok
    assert _read_skill_meta(sandbox, "cas-evolve")["version"] == "0.1.1"

    # 过期版本 → source_changed 拒绝，现文件不被破坏、不写快照（无副作用）
    stale_raw = file.read_text(encoding="utf-8-sig")
    snapshots_before = len(_history_rows(sandbox, "cas-evolve"))
    refused = se.evolve_skill_file(
        skill_name="cas-evolve", lesson="stale write", instructions="不应落盘。", expected_version="0.1.0"
    )
    assert refused.get("ok") is False, refused
    assert refused["error"] == "source_changed"
    assert refused["expected_version"] == "0.1.0"
    assert refused["current_version"] == "0.1.1"
    assert file.read_text(encoding="utf-8-sig") == stale_raw  # 新用户决定不被覆盖
    assert "不应落盘" not in file.read_text(encoding="utf-8-sig")
    assert len(_history_rows(sandbox, "cas-evolve")) == snapshots_before
    assert before != stale_raw  # 首次成功写入确实发生

    # skills.evolve_skill 透传 expected_version
    via_wrapper = evolve_skill("cas-evolve", lesson="stale via wrapper", expected_version="0.1.0")
    assert via_wrapper.get("ok") is False and via_wrapper["error"] == "source_changed"
    ok2 = evolve_skill("cas-evolve", lesson="fresh via wrapper", expected_version="0.1.1")
    assert ok2.get("ok") is True, ok2


# ── 场景 5：publish_skill_bundle 最小发布事务 ──


def test_publish_skill_bundle_end_to_end(sandbox):
    create_skill(name="pub-skill", description="旧描述", instructions="旧正文，未认证。")
    assert st.manual_promote("pub-skill")["ok"] is True  # 先晋级，验证发布后回 experimental
    assert st.get_tier("pub-skill") == "staging"
    file = _skill_path(sandbox, "pub-skill")

    bundle = {
        "name": "pub-skill",
        "description": "认证后的描述",
        "when_to_use": "认证后的触发条件",
        "instructions": "认证后的最终正文。",
        "producer_kind": "direct",
        "sha256": _canonical_bundle_hash(
            {
                "name": "pub-skill",
                "description": "认证后的描述",
                "when_to_use": "认证后的触发条件",
                "instructions": "认证后的最终正文。",
            }
        ),
    }
    out = publish_skill_bundle(skill_name="pub-skill", bundle=bundle, expected_version="0.1.0", actor="release-test")
    assert out.get("ok") is True, out
    # 实际加载回执：path/sha256/version 与磁盘一致
    assert Path(out["path"]) == file
    written = file.read_text(encoding="utf-8-sig")
    assert out["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    assert out["version"] == "0.1.1"
    assert _read_skill_meta(sandbox, "pub-skill")["version"] == "0.1.1"
    assert "认证后的最终正文" in written and "旧正文" not in written

    # 发布后回 experimental（register_evolution_write 语义），不自动晋级
    assert st.get_tier("pub-skill") == "experimental"
    assert out["tier"] == "experimental"

    # 旧内容留 history 快照
    snapshots = [r for r in _history_rows(sandbox, "pub-skill") if r.get("event") == "snapshot"]
    assert snapshots and "旧正文" in snapshots[-1]["content"]

    # 发布审计行（tier_audit 的 release 行 + usage 台账），回执 hash 记录在案
    audit_rows = _tier_audit_rows(sandbox)
    release_rows = [r for r in audit_rows if r.get("event") == "release"]
    assert release_rows and release_rows[-1]["sha256"] == out["sha256"]
    assert release_rows[-1]["messaging_authorization"] == "not_granted"
    usage_rows = _jsonl_rows(sandbox / ".evoharness" / "skill-evolution" / "usage.jsonl")
    assert any(r.get("event") == "release" for r in usage_rows)

    # 缓存随发布刷新：不做手动 reset，发现结果立即反映新正文
    names = {s.name: s.prompt_template for s in discover_skills()}
    assert "认证后的最终正文" in names["pub-skill"]

    # 许可边界：不授予对外自动发信授权
    assert out["messaging_authorization"] == "not_granted"


def test_publish_skill_bundle_cas_and_guards(sandbox):
    create_skill(name="guard-skill", description="d", instructions="旧正文。")
    file = _skill_path(sandbox, "guard-skill")
    bundle = {
        "name": "guard-skill",
        "instructions": "新正文。",
        "producer_kind": "direct",
        "sha256": _canonical_bundle_hash({"name": "guard-skill", "instructions": "新正文。"}),
    }

    # 期望版本不匹配被拒（source_changed），现文件不被破坏
    refused = publish_skill_bundle(skill_name="guard-skill", bundle=bundle, expected_version="9.9.9")
    assert refused.get("ok") is False and refused["error"] == "source_changed", refused
    assert "新正文" not in file.read_text(encoding="utf-8-sig")
    assert _read_skill_meta(sandbox, "guard-skill")["version"] == "0.1.0"

    # active 文件 hash CAS 不匹配被拒
    refused2 = publish_skill_bundle(
        skill_name="guard-skill", bundle=bundle, expected_active_sha256="0" * 64
    )
    assert refused2.get("ok") is False and refused2["error"] == "source_changed", refused2

    # bundle 内容 hash 与提供值不符被拒
    bad_hash_bundle = {**bundle, "sha256": "1" * 64}
    refused3 = publish_skill_bundle(skill_name="guard-skill", bundle=bad_hash_bundle, expected_version="0.1.0")
    assert refused3.get("ok") is False and refused3["error"] == "bundle_hash_mismatch", refused3

    # producer_kind=projection 拒绝（投影只由源重建）
    proj_bundle = {**bundle, "producer_kind": "projection"}
    refused4 = publish_skill_bundle(skill_name="guard-skill", bundle=proj_bundle, expected_version="0.1.0")
    assert refused4.get("ok") is False and refused4["error"] == "producer_kind_not_directly_publishable", refused4
    assert "新正文" not in file.read_text(encoding="utf-8-sig")

    # 目标是投影文件时拒绝发布
    projection = se.write_managed_projection(
        skills_root=sandbox / ".evoharness" / "skills",
        evolution_root=sandbox / ".evoharness" / "skill-evolution",
        name="proj-guard",
        description="projection",
        instructions="投影正文。",
        when_to_use="w",
        scope={"tenant": "t1", "owner": "o1"},
        source_revisions=[],
        actor="test",
    )
    assert projection.get("ok") is True, projection
    refused5 = publish_skill_bundle(
        skill_name="proj-guard",
        bundle={"name": "proj-guard", "instructions": "直接改投影。", "producer_kind": "direct"},
    )
    assert refused5.get("ok") is False and refused5["error"] == "managed_projection_source_owned", refused5
    assert "直接改投影" not in _skill_path(sandbox, "proj-guard").read_text(encoding="utf-8-sig")

    # 全部拒绝后目标文件保持原状，且成功路径仍可用（版本 CAS 对上）
    ok_bundle = {
        "name": "guard-skill",
        "instructions": "通过后的正文。",
        "producer_kind": "direct",
        "sha256": _canonical_bundle_hash({"name": "guard-skill", "instructions": "通过后的正文。"}),
    }
    ok = publish_skill_bundle(
        skill_name="guard-skill",
        bundle=ok_bundle,
        expected_version="0.1.0",
    )
    assert ok.get("ok") is True, ok
    assert _read_skill_meta(sandbox, "guard-skill")["version"] == "0.1.1"
