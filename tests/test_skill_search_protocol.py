"""DE-30 协议回归：Case 协议 S/V/H 角色、来源纪律、封存集引用与冻结。

覆盖（tasks.json DE-30 acceptance_scenarios 之 6 / NATIVE-ARCHITECTURE-INTEGRATION
§6 / SKILL-EVOLUTION-SEARCH §4）：

- Case 同时保留 origin_group=evidence|score|curated_run、原 split、family 与新
  S/V/H 角色；evidence 不得升 V/H（出生题重答成功只作开发证据）；
- 已观测 holdout（原 split=final_holdout、score 来源、命中污染登记）不能直接当
  新 H——必须另设独立封存集引用（ref+sha256+case_ids）；
- 复用 eval/evolution_experiment 的污染登记与近重复泄漏判定（不重造登记）；
- 冻结：Case 增/删/改/协议变化 → verify_case_freeze 拒绝；协议条目经
  protocol_freeze_entry 进 SearchManifest → 角色/Case 变化 → 新协议指纹 →
  旧评测不可复用（接 DE-29 复用门）。

全部离线合成，不调真实模型。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.skill_search import protocol as sp
from agents.skill_search import contracts as sc
from eval import evolution_experiment as ee


_UNSET = object()


def _default_question(case_id: str) -> str:
    """确定性派生彼此不近重复的题面（避免默认题面在泄漏门被正确拒绝）。"""
    words = [f"{''.join(chr(97 + (ord(ch) * 7 + i) % 26))}{i}" for i, ch in enumerate(case_id * 6)]
    return " ".join(["topic", *words])


def _case(case_id: str, *, origin: str = "evidence", split: str = "evidence",
          family: str = "fam-a", question=_UNSET, role: str = "", **extra) -> dict:
    case = {
        "case_id": case_id,
        "origin_group": origin,
        "origin_split": split,
        "family": family,
        "question": _default_question(case_id) if question is _UNSET else question,
    }
    if role:
        case["protocol_role"] = role
    case.update(extra)
    return case


def _sealed(case_ids: list[str]) -> dict:
    return {
        "ref": "sealed-holdout-2026w46",
        "sha256": "a" * 64,
        "case_ids": case_ids,
    }


# ── Case 集合构建：缺 ID / 重复 ID / 非法来源 fail-closed ────────────────────


def test_case_set_rejects_missing_id_duplicate_id_and_bad_origin():
    bad = sp.build_case_set([
        {"origin_group": "evidence", "family": "f"},
        _case("c1"),
        _case("c1"),
        _case("c2", origin="not-a-group"),
    ])
    assert bad["ok"] is False
    codes = {item["code"] for item in bad["errors"]}
    assert "case_id_missing" in codes or any("case_id_missing" in e["reason"] for e in bad["errors"])
    assert "duplicate_case_id" in codes
    assert any(code.startswith("invalid_origin_group") or code == "invalid_origin_group" for code in codes)

    ok = sp.build_case_set([_case("c1", origin="score", split="mutate_dev"), _case("c2", origin="curated_run")])
    assert ok["ok"] is True
    assert [c["case_id"] for c in ok["cases"]] == ["c1", "c2"]


def test_case_preserves_origin_split_family_and_consent():
    built = sp.build_case_set([
        _case("c-score", origin="score", split="promotion_test", family="fam-b",
              source_ref="usage.jsonl#42", consent_scope="tenant-a", revision="3"),
    ])
    assert built["ok"] is True
    case = built["cases"][0]
    assert case["origin_group"] == "score"
    assert case["origin_split"] == "promotion_test"
    assert case["family"] == "fam-b"
    assert case["source_ref"] == "usage.jsonl#42"
    assert case["consent_scope"] == "tenant-a"
    assert case["revision"] == "3"


# ── 角色授予：来源纪律（evidence 不能升 V/H；score 不能当封存 H） ────────────


def test_evidence_cases_cannot_be_promoted_to_v_or_h():
    cases = [
        _case("birth-1", origin="evidence", split="evidence", role="S"),
        _case("birth-2", origin="evidence", split="evidence", role="V"),
        _case("birth-3", origin="evidence", split="evidence", role="H"),
    ]
    out = sp.assign_protocol_roles(cases, sealed_holdout=_sealed(["birth-3"]))
    assert out["ok"] is False
    codes = {v["case_id"]: v["code"] for v in out["violations"]}
    assert codes["birth-2"] == "evidence_cannot_be_final_holdout"
    assert codes["birth-3"] == "evidence_cannot_be_final_holdout"


def test_score_origin_cannot_be_sealed_holdout_without_separate_reference():
    cases = [
        _case("dev-1", origin="score", split="mutate_dev", role="S"),
        _case("promo-1", origin="score", split="promotion_test", role="V"),
        _case("used-1", origin="score", split="mutate_dev", role="H"),
    ]
    out = sp.assign_protocol_roles(cases, sealed_holdout=_sealed(["used-1"]))
    assert out["ok"] is False
    assert {v["case_id"]: v["code"] for v in out["violations"]}["used-1"] == "observed_holdout_not_sealed"


def test_curated_run_h_requires_independent_sealed_reference():
    cases = [_case("fresh-1", origin="curated_run", split="", role="H")]
    # 无封存引用 → 拒绝（不接受无引用的裸题当 H）
    out = sp.assign_protocol_roles(cases)
    assert out["ok"] is False
    assert out["violations"][0]["code"] == "sealed_ref_required"
    # 引用里没有该题 → 拒绝
    out = sp.assign_protocol_roles(cases, sealed_holdout=_sealed(["other-1"]))
    assert out["ok"] is False
    assert out["violations"][0]["code"] == "sealed_ref_required"
    # 独立封存集引用齐全 → 授 H
    out = sp.assign_protocol_roles(cases, sealed_holdout=_sealed(["fresh-1"]))
    assert out["ok"] is True
    assert out["cases"][0]["protocol_role"] == "H"
    assert out["promotion_grade"] == sp.PROMOTION_GRADE_SEALED


def test_observed_final_holdout_split_cannot_be_renamed_as_new_h():
    # 原 experiment 的 final_holdout 已被逐轮看过成绩：即使标 curated_run 也拒绝当新 H。
    cases = [_case("old-hold-1", origin="curated_run", split="final_holdout", role="H")]
    out = sp.assign_protocol_roles(cases, sealed_holdout=_sealed(["old-hold-1"]))
    assert out["ok"] is False
    assert out["violations"][0]["code"] == "observed_holdout_not_sealed"


def test_contamination_registry_reuses_evolution_experiment_and_blocks_h(tmp_path):
    items = [
        {"task_id": "t-old-1", "Question": "old holdout question alpha", "answer": "a",
         "problem_type": "pt-old", "Level": 1},
        {"task_id": "t-seal-1", "Question": "brand new sealed question beta", "answer": "b",
         "problem_type": "pt-new", "Level": 1},
    ]
    registry = ee.register_contamination(
        ["t-old-1"], reason="historical pilot observed this holdout",
        evidence=["test"], path=tmp_path / "contamination.json", items=items,
    )
    cases = [
        _case("t-old-1", origin="curated_run", split="", family="pt-old:L1", role="H",
              question="old holdout question alpha"),
        _case("t-seal-1", origin="curated_run", split="", family="pt-new:L1", role="H",
              question="brand new sealed question beta"),
    ]
    out = sp.assign_protocol_roles(cases, sealed_holdout=_sealed(["t-old-1", "t-seal-1"]),
                                   contamination=registry)
    assert out["ok"] is False
    assert {v["case_id"]: v["code"] for v in out["violations"]}["t-old-1"] == "contaminated_case_not_sealed"
    assert out["violations"][0]["reason"].startswith("命中历史 holdout 污染登记")

    out = sp.assign_protocol_roles([cases[1]], sealed_holdout=_sealed(["t-seal-1"]), contamination=registry)
    assert out["ok"] is True
    assert out["promotion_grade"] == sp.PROMOTION_GRADE_SEALED
    # 复用原设施：登记文件本身由 evolution_experiment 写出、含 families 污染
    on_disk = json.loads((tmp_path / "contamination.json").read_text(encoding="utf-8"))
    assert "t-old-1" in on_disk["taskIds"]


def test_holdout_leak_near_duplicate_rejected_and_selection_only_grade():
    cases = [
        _case("s-1", origin="score", split="mutate_dev", role="S",
              question="deploy rollout checklist with canary 5 percent gate"),
        _case("h-1", origin="curated_run", split="", role="H",
              question="deploy rollout checklist with canary 5 percent gate"),
    ]
    out = sp.assign_protocol_roles(cases, sealed_holdout=_sealed(["h-1"]))
    assert out["ok"] is False
    assert out["violations"][0]["code"] == "holdout_leak"

    # 干净的 V-only 协议：selection 数据，不具备无偏最终检验
    out = sp.assign_protocol_roles([
        _case("v-1", origin="score", split="promotion_test", role="V"),
        _case("s-2", origin="score", split="mutate_dev", role="S"),
    ])
    assert out["ok"] is True
    assert out["promotion_grade"] == sp.PROMOTION_GRADE_SELECTION
    assert out["roles"]["S"] == ["s-2"] and out["roles"]["V"] == ["v-1"] and out["roles"]["H"] == []


def test_h_question_missing_fails_closed():
    cases = [_case("h-noq", origin="curated_run", split="", role="H", question="")]
    out = sp.assign_protocol_roles(cases, sealed_holdout=_sealed(["h-noq"]))
    assert out["ok"] is False
    assert out["violations"][0]["code"] == "holdout_question_missing"


# ── 冻结与只读快照语义：增/删/改/协议变化一律拒绝 ───────────────────────────


def _assigned_cases() -> list[dict]:
    out = sp.assign_protocol_roles([
        _case("c-e1", origin="evidence", split="evidence", role="S"),
        _case("c-s1", origin="score", split="mutate_dev", role="S"),
        _case("c-v1", origin="score", split="promotion_test", role="V"),
        _case("c-h1", origin="curated_run", split="", role="H", family="fam-seal"),
    ], sealed_holdout=_sealed(["c-h1"]))
    assert out["ok"] is True
    return out["cases"]


def test_freeze_requires_roles_and_is_deterministic():
    unassigned = [_case("c1"), _case("c2")]
    assert sp.freeze_case_protocol(unassigned)["ok"] is False

    cases = _assigned_cases()
    first = sp.freeze_case_protocol(cases)
    assert first["ok"] is True
    second = sp.freeze_case_protocol(list(reversed(cases)))
    assert second["ok"] is True
    # 输入顺序无关：固定 Case ID 顺序 + 同一指纹
    assert first["freeze"].ordered_case_ids == second["freeze"].ordered_case_ids
    assert first["freeze"].fingerprint == second["freeze"].fingerprint
    assert first["freeze"].roles == {"S": ["c-e1", "c-s1"], "V": ["c-v1"], "H": ["c-h1"]}
    assert first["freeze"].promotion_grade == sp.PROMOTION_GRADE_SEALED


def test_verify_freeze_detects_add_remove_modify_and_protocol_change():
    cases = _assigned_cases()
    frozen_protocol = {"rubric": {"sha256": "r1"}}
    freeze = sp.freeze_case_protocol(cases, protocol=frozen_protocol)["freeze"]

    assert sp.verify_case_freeze(freeze, cases, protocol=frozen_protocol)["ok"] is True

    added = cases + [_case("c-new", origin="curated_run", split="", role="S")]
    out = sp.verify_case_freeze(freeze, added, protocol=frozen_protocol)
    assert out["ok"] is False and out["error"] == "case_protocol_drift"
    assert {"case_id": "c-new", "kind": "added"} in out["drift"]

    removed = [c for c in cases if c["case_id"] != "c-s1"]
    out = sp.verify_case_freeze(freeze, removed, protocol=frozen_protocol)
    assert out["ok"] is False
    assert {"case_id": "c-s1", "kind": "missing"} in out["drift"]

    modified = [dict(c) for c in cases]
    for c in modified:
        if c["case_id"] == "c-h1":
            c["question"] = "rotated sealed question gamma"
    out = sp.verify_case_freeze(freeze, modified, protocol=frozen_protocol)
    assert out["ok"] is False
    assert {"case_id": "c-h1", "kind": "modified"} in out["drift"]

    role_changed = [dict(c) for c in cases]
    for c in role_changed:
        if c["case_id"] == "c-v1":
            c["protocol_role"] = "S"
    out = sp.verify_case_freeze(freeze, role_changed, protocol=frozen_protocol)
    assert out["ok"] is False
    assert {"case_id": "c-v1", "kind": "modified"} in out["drift"]

    out = sp.verify_case_freeze(freeze, cases, protocol={"rubric": {"sha256": "r2"}})
    assert out["ok"] is False and out["error"] == "protocol_changed"


def test_case_readonly_digest_changes_on_any_content_drift():
    cases = _assigned_cases()
    digest = sp.case_readonly_digest(cases)
    assert digest == sp.case_readonly_digest(list(reversed(cases)))
    drifted = [dict(c) for c in cases]
    drifted[0]["source_revision"] = "r2"
    assert sp.case_readonly_digest(drifted) != digest


# ── 与 SearchManifest 的组合：协议条目进 manifest → 旧评测不可复用 ───────────


def test_manifest_protocol_fingerprint_binds_case_roles_and_reuse_gate(tmp_path):
    skill_dir = tmp_path / ".evoharness" / "skills" / "sop"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: sop-skill\ndescription: 独立执行 SOP\nversion: 1.0.0\nproducer-kind: executable_sop\n---\n\n# 步骤\n",
        encoding="utf-8",
    )
    cases = _assigned_cases()
    freeze = sp.freeze_case_protocol(cases)["freeze"]
    manifest_a = sc.build_search_manifest(
        skill_dir=skill_dir, tenant="t1", space="s1",
        protocol=sc.freeze_protocol(splits=sp.protocol_freeze_entry(freeze)),
    )["manifest"]
    manifest_b = sc.build_search_manifest(
        skill_dir=skill_dir, tenant="t1", space="s1",
        protocol=sc.freeze_protocol(splits=sp.protocol_freeze_entry(freeze)),
    )["manifest"]
    assert manifest_a.eval_protocol_fingerprint == manifest_b.eval_protocol_fingerprint

    # 角色范围变化（c-v1 升 H 并入封存集）→ 新协议指纹 → 旧评测不可复用
    cases_v2 = sp.assign_protocol_roles([
        _case("c-e1", origin="evidence", split="evidence", role="S"),
        _case("c-s1", origin="score", split="mutate_dev", role="S"),
        _case("c-v1", origin="curated_run", split="", role="H", family="fam-seal"),
        _case("c-h1", origin="curated_run", split="", role="H", family="fam-seal"),
    ], sealed_holdout=_sealed(["c-v1", "c-h1"]))["cases"]
    freeze_v2 = sp.freeze_case_protocol(cases_v2)["freeze"]
    manifest_v2 = sc.build_search_manifest(
        skill_dir=skill_dir, tenant="t1", space="s1",
        protocol=sc.freeze_protocol(splits=sp.protocol_freeze_entry(freeze_v2)),
    )["manifest"]
    assert manifest_v2.eval_protocol_fingerprint != manifest_a.eval_protocol_fingerprint

    evaluation = sc.attach_bundle_identity(manifest_a, {"score_vector": {"c-s1": 0.9}})
    assert manifest_v2.check_evaluation_reuse(evaluation)["ok"] is False
    assert manifest_v2.check_evaluation_reuse(evaluation)["error"] == "protocol_changed"
