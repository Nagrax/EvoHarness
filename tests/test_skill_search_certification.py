"""DE-35 同制品封存认证、显式采用与原 tier 接线回归（SKILL-EVOLUTION-SEARCH §13/§4、
NATIVE-ARCHITECTURE-INTEGRATION §7/§8）。

覆盖 tasks.json DE-35 的 5 条 acceptance_scenarios + 五状态区分 + H 一次性 + core 人工门：

1. S1：候选 A 的封存/冠军成绩不能晋级未替换的 active B（hash 门 + 搜索报告一律
   不晋级）；追加审计文本后 hash 变化必须重新验证（active_drift / blocked_source_changed）。
2. S2：出生题/搜索开发题不作线上 usage score（dev_evidence 不入分数向量）；证书
   样本量只来自真实计数、不填造线上样本量；采用后缺证据保持 experimental。
3. S3：H 失败不能依 H 择第二名或继续调整后仍称同一 H 独立（seal 一次性语义）；
   旧曲线 holdout 按已暴露登记（复用原污染登记）。
4. S4：搜索期间 active 被原维护更新或投影重建时不得覆盖（CAS source_changed /
   managed_projection_source_owned）；tier 回退后若内容未恢复必须如实报告
   （rollback 与 restore_content 分离的回执组合）。
5. S5：core 仍明确人工晋级；已有原生 experimental 技能不因新搜索认证要求被全局禁用。

全部离线（合成 SKILL.md / 固定评分 / 固定协议指纹 / 合成封存回执），不依赖真实模型。
真实模型能力实验条件单列 blockedOn（见 docs/digital-employee/acceptance/DE-35/）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import skill_tiers as st
from agents.frontmatter import parse_frontmatter
from agents.online_skill_eval import _deployment_states_field, evaluate_online_skill_evolution_async
from agents.skill_evolution import get_evolution_dir, skill_content_hash
from agents.skill_search import certification as cert
from agents.skill_search import contracts as sc
from agents.skill_search import evaluator as ev
from agents.skill_search import modules as sm
from agents.skill_search import mutator as mu
from agents.skill_search import pool as pl
from agents.skill_search import protocol as sp
from agents.skill_search import selection as sel
from agents.skills import create_skill, evolve_skill, reset_skill_cache

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


# ── 合成材料 ─────────────────────────────────────────────────────────────────

SOP_TEXT = """---
name: cert-sop
description: 认证验收业务技能
version: 0.4.0
producer-kind: executable_sop
---

# 总览

本技能处理认证验收业务流程。

## 工作流程

按顺序执行以下步骤，先核对输入再执行：

1. 领取工单并核对客户信息、预约时间与机型配置。
2. 出发前检查物料包：备件、工具、防护与回单模板。
3. 到场后先断电验机，拍照留存现场与铭牌信息。
4. 按机型作业指导执行维修或安装，逐项回填工单。
5. 完成后向客户演示功能，并确认验收签字。

## 输出格式

结果一律使用 Markdown 表格输出。

## 安全边界

未经确认不得执行不可逆操作。
"""

DEFS = [
    {"module_id": "workflow", "type": "capability.rollout", "writable": True, "heading": "## 工作流程"},
    {"module_id": "output-format", "type": "shared.response", "writable": True, "heading": "## 输出格式"},
    {"module_id": "guardrails", "type": "shared.guardrails", "writable": False, "heading": "## 安全边界"},
]

S_CASE_IDS = ["case-s1", "case-s2"]
POLICY_ON = mu.MutationPolicy(enable_bounded_module_edit=True)


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_skill(root: Path, slug: str, text: str) -> Path:
    skill_dir = root / ".evoharness" / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


def _content_hash_of(text: str) -> str:
    parsed = parse_frontmatter(text)
    return skill_content_hash(
        {
            "name": str(parsed.meta.get("name") or ""),
            "description": str(parsed.meta.get("description") or ""),
            "when_to_use": str(parsed.meta.get("when-to-use") or parsed.meta.get("when_to_use") or ""),
            "instructions": parsed.body,
        }
    )


def _manifest_for(skill_dir: Path) -> sc.SearchManifest:
    return sc.build_search_manifest(
        skill_dir=skill_dir,
        tenant="acme",
        space="sre",
        protocol=sc.freeze_protocol(
            cases_ref="fixtures/cert-cases@v1",
            cases_sha256="a" * 64,
            rubric_ref="rubrics/cert@v1",
            rubric_sha256="b" * 64,
            verifier_sha256="c" * 64,
            models={"evaluator": "offline-fixed", "params": {"temperature": 0}},
            algorithm_version="cert-offline-v1",
            splits={
                "S": {"ref": "S@v1", "access": "reflect"},
                "V": {"ref": "V@v1", "access": "release-check"},
                "H": {"ref": "H@v1", "access": "sealed"},
            },
        ),
    )["manifest"]


def _candidate_text() -> str:
    spec = sm.build_module_spec(SOP_TEXT, DEFS)["spec"]
    section = sm.module_section_text(spec, SOP_TEXT, "workflow")
    expected = "5. 完成后向客户演示功能，并确认验收签字。"
    start = section.index(expected)
    patch = {
        "target_module": "workflow",
        "base_text_sha256": _sha_text(SOP_TEXT),
        "rationale": "DE-35 认证候选",
        "ops": [
            {
                "op": "replace",
                "start": start,
                "end": start + len(expected),
                "expected": expected,
                "replacement": "5. 完成后向客户演示功能，确认验收签字并回填工单结果。",
                "module": "workflow",
            }
        ],
    }
    applied = mu.apply_candidate_patch(spec, SOP_TEXT, patch, policy=POLICY_ON)
    assert applied.get("ok") is True, applied
    return applied["candidate_text"]


def _setup_search(sandbox: Path, *, slug: str = "cert-sop", text: str | None = None):
    """合成完整搜索链：skill → manifest → 池（种子+候选 A）→ 冠军锁定 → 最终包装。"""
    skill_text = text or SOP_TEXT
    skill_dir = _write_skill(sandbox, slug, skill_text)
    manifest = _manifest_for(skill_dir)
    candidate_text = _candidate_text() if text is None else _candidate_text_for(skill_text)
    parsed = parse_frontmatter(candidate_text)
    cand_content_hash = skill_content_hash(
        {
            "name": str(parsed.meta.get("name") or ""),
            "description": str(parsed.meta.get("description") or ""),
            "when_to_use": str(parsed.meta.get("when-to-use") or ""),
            "instructions": parsed.body,
        }
    )
    pool = pl.CandidatePool(search_id=manifest.search_id, case_ids=S_CASE_IDS)
    seed_result = pool.register_seed(
        candidate_id="seed",
        score_vector={"case-s1": 0.5, "case-s2": 0.4},
        content_hash=_content_hash_of(skill_text),
        protocol_hash=manifest.eval_protocol_fingerprint,
    )
    assert seed_result.get("decision") == pl.DECISION_ADMITTED, seed_result
    admit = pool.admit(
        candidate_id="cand-a",
        parent_id="seed",
        score_vector={"case-s1": 0.9, "case-s2": 0.8},
        content_hash=cand_content_hash,
        protocol_hash=manifest.eval_protocol_fingerprint,
        cumulative_seed_diff={"added_bytes": 12, "ops": 1, "generations": 1},
    )
    assert admit.get("decision") == pl.DECISION_ADMITTED, admit
    champion = sel.select_search_champion(pool.candidates, case_ids=S_CASE_IDS)
    assert champion.get("ok") is True and champion.get("search_champion") == "cand-a", champion
    lock = cert.lock_champion_candidate(manifest=manifest, champion_selection=champion, pool=pool)
    assert lock.get("ok") is True, lock
    packaged = mu.package_candidate(
        candidate_text, lesson="采用搜索冠军候选 cand-a", rationale="DE-35 封存认证验证"
    )
    assert packaged.get("ok") is True, packaged
    return {
        "skill_dir": skill_dir,
        "skill_name": "cert-sop",
        "manifest": manifest,
        "pool": pool,
        "champion": champion,
        "lock": lock["lock"],
        "candidate_text": candidate_text,
        "candidate_content_hash": cand_content_hash,
        "packaged": packaged,
        "seed_sha256": _sha_file(skill_dir / "SKILL.md"),
    }


def _candidate_text_for(text: str) -> str:
    """同款有界 patch 作用于任意同构正文（供漂移测试构造不同文本）。"""
    spec = sm.build_module_spec(text, DEFS)["spec"]
    section = sm.module_section_text(spec, text, "workflow")
    expected = "5. 完成后向客户演示功能，并确认验收签字。"
    start = section.index(expected)
    patch = {
        "target_module": "workflow",
        "base_text_sha256": _sha_text(text),
        "rationale": "DE-35 认证候选",
        "ops": [
            {
                "op": "replace",
                "start": start,
                "end": start + len(expected),
                "expected": expected,
                "replacement": "5. 完成后向客户演示功能，确认验收签字并回填工单结果。",
                "module": "workflow",
            }
        ],
    }
    applied = mu.apply_candidate_patch(spec, text, patch, policy=POLICY_ON)
    assert applied.get("ok") is True, applied
    return applied["candidate_text"]


def _sealed_holdout() -> dict:
    return {"ref": "H@v1-sealed", "sha256": "d" * 64, "case_ids": ["case-h1"]}


def _h_cases() -> list[dict]:
    return [
        {
            "case_id": "case-h1",
            "origin_group": "curated_run",
            "family": "cert-h-family",
            "question": "独立封存题：认证验收场景 H 的完整执行预期。",
            "source_ref": "seal/H@v1-sealed",
            "consent_scope": "sre",
        }
    ]


def _run_holdout(ctx: dict, *, passed: bool) -> dict:
    seal = cert.create_holdout_seal(
        manifest=ctx["manifest"], locked=ctx["lock"], sealed_holdout=_sealed_holdout(), h_cases=_h_cases()
    )
    assert seal.get("ok") is True, seal
    attempt = cert.record_holdout_attempt(
        manifest=ctx["manifest"],
        locked=ctx["lock"],
        seal=seal["seal"],
        verdicts={"case-h1": {"passed": passed, "score": 1.0 if passed else 0.2}},
    )
    assert attempt.get("ok") is True, attempt
    return attempt


def _certify(ctx: dict, *, passed: bool = True, with_holdout: bool = True, with_comparison: bool = True):
    attempt = _run_holdout(ctx, passed=passed) if with_holdout else None
    active_comparison = (
        {
            "eval_protocol_fingerprint": ctx["manifest"].eval_protocol_fingerprint,
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
            "candidate_average_score": 0.85,
            "active_average_score": 0.45,
            "source": "offline_fixed_session",
        }
        if with_comparison
        else None
    )
    return cert.certify_candidate(
        manifest=ctx["manifest"],
        locked=ctx["lock"],
        packaged=ctx["packaged"],
        holdout_seal=attempt["seal"] if attempt else None,
        active_comparison=active_comparison,
        s_case_ids=S_CASE_IDS,
        v_case_ids=[],
        reviewer="offline",
    )


def _promotion_report(name: str, status: str, *, promoted=True, artifacts_extra=None) -> dict:
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
        "thresholds": {"min_score_samples": 3, "min_promotion_tests": 1},
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


def _tier_audit_rows(root: Path) -> list[dict]:
    path = root / ".evoharness" / "skill-evolution" / "tier_audit.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _seed_experimental_tier(name: str) -> None:
    assert st.set_tier(name, st.TIER_EXPERIMENTAL, trigger="test_seed", actor="test")["ok"]


# ── 场景 1（S1）：候选 A 的封存/冠军成绩不能晋级未替换的 active B；审计文本追加 → 重新验证 ──


def test_s1_champion_scores_cannot_promote_untouched_active_and_search_reports_never_promote(sandbox):
    ctx = _setup_search(sandbox)
    name = ctx["skill_name"]
    _seed_experimental_tier(name)  # active 仍是种子制品 B（未替换）
    assert st.get_tier(name) == "experimental"

    # 认证候选 A（封存 H 通过 + 同协议对照）——认证本身不改 tier
    cert_out = _certify(ctx)
    assert cert_out["ok"] is True, cert_out
    assert cert_out["state"] == cert.STATE_CERTIFIED_CANDIDATE
    assert cert_out["certificate"]["certification_only"] is True
    assert cert_out["certificate"]["messaging_authorization"] == "not_granted"
    assert st.get_tier(name) == "experimental"  # 认证不晋级

    # A 的报告绑定 A 的制品 hash → active 仍是 B → active_drift 拒绝（两侧 hash 可见）
    report_a = _promotion_report(
        name,
        "healthy",
        promoted=True,
        artifacts_extra={
            "active_snapshot_hash": ctx["candidate_content_hash"],
            "candidate_snapshot_hash": ctx["candidate_content_hash"],
            "eval_protocol_fingerprint": ctx["manifest"].eval_protocol_fingerprint,
        },
    )
    out = st.apply_eval_promotions(report_a)
    assert out["promoted"] == []
    drift = [s for s in out["skipped"] if s.get("why", "").startswith("active_drift")]
    assert drift, out["skipped"]
    assert drift[0]["active_snapshot_hash"] == ctx["candidate_content_hash"]
    assert drift[0]["current_active_hash"] == _content_hash_of(SOP_TEXT)
    assert st.get_tier(name) == "experimental"

    # 搜索报告形态（eval_source=skill_search）：即使 hash 与 active 一致也一律拒绝
    search_report = _promotion_report(
        name,
        "healthy",
        promoted=True,
        artifacts_extra={
            "eval_source": cert.EVAL_SOURCE_SEARCH,
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
            "eval_protocol_fingerprint": ctx["manifest"].eval_protocol_fingerprint,
        },
    )
    out2 = st.apply_eval_promotions(search_report)
    assert out2["promoted"] == []
    gated = [s for s in out2["skipped"] if s.get("gate_code") == "search_report_not_promotion_evidence"]
    assert gated, out2["skipped"]
    assert "搜索报告不直接晋级" in gated[0]["why"]
    assert st.get_tier(name) == "experimental"
    assert not [r for r in _tier_audit_rows(sandbox) if r.get("trigger") == "auto_evidence"]

    # 评测后追加审计文本 → active hash 变化 → 绑定旧 hash 的成绩必须重新验证（拒绝晋级）
    skill_file = ctx["skill_dir"] / "SKILL.md"
    drifted = skill_file.read_text(encoding="utf-8").rstrip() + "\n\n## Evolution Notes\n\n- 2026-11-14: 评测后追加的包装\n"
    skill_file.write_text(drifted, encoding="utf-8")
    reset_skill_cache()
    report_seed = _promotion_report(
        name,
        "healthy",
        promoted=True,
        artifacts_extra={
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
            "eval_protocol_fingerprint": ctx["manifest"].eval_protocol_fingerprint,
        },
    )
    out3 = st.apply_eval_promotions(report_seed)
    assert out3["promoted"] == []
    assert any(s.get("why", "").startswith("active_drift") for s in out3["skipped"])
    # 源漂移同样使认证拒绝（S4 前半：重开搜索而非 rebase）
    drift_cert = cert.certify_candidate(
        manifest=ctx["manifest"],
        locked=ctx["lock"],
        packaged=ctx["packaged"],
        active_comparison={
            "eval_protocol_fingerprint": ctx["manifest"].eval_protocol_fingerprint,
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
        },
    )
    assert drift_cert["ok"] is False and drift_cert["error"] == "blocked_source_changed", drift_cert


# ── 场景 2（S2）：出生题不作 usage score；证书样本量如实；采用后缺证据保持 experimental ──


def test_s2_birth_evidence_never_usage_score_and_certificate_counts_are_truthful(sandbox):
    ctx = _setup_search(sandbox)
    name = ctx["skill_name"]
    _seed_experimental_tier(name)

    # 出生题（origin_group=evidence）重答成功只作开发证据：不入分数向量、不构成线上成绩
    case_set = sp.build_case_set(
        [
            {"case_id": "case-s1", "origin_group": "evidence", "family": "f1", "question": "出生证据题"},
            {"case_id": "case-s2", "origin_group": "score", "family": "f1", "question": "线上成绩题"},
        ]
    )
    roles = sp.assign_protocol_roles(case_set["cases"])
    assert roles["ok"] is True and roles["promotion_grade"] == sp.PROMOTION_GRADE_DEV_ONLY
    freeze = sp.freeze_case_protocol(roles["cases"])["freeze"]
    outcomes = [
        ev.ExecutionOutcome(
            case_id="case-s1",
            candidate_id="cand-x",
            terminal_status=ev.TERMINAL_PASS,
            valid=True,
            score=1.0,
            dev_evidence_only=True,  # 出生题重答
            protocol_fingerprint=freeze.fingerprint,
        ),
        ev.ExecutionOutcome(
            case_id="case-s2",
            candidate_id="cand-x",
            terminal_status=ev.TERMINAL_PASS,
            valid=True,
            score=0.9,
            protocol_fingerprint=freeze.fingerprint,
        ),
    ]
    finalize = ev.finalize_score_vector(freeze=freeze, outcomes=outcomes, candidate={"candidate_id": "cand-x"})
    assert finalize["ok"] is True
    assert "case-s1" not in finalize["score_vector"]  # 不作 usage score
    assert finalize["dev_evidence"] and finalize["dev_evidence"][0]["case_id"] == "case-s1"

    # 无 H / 无同协议对照 → 证书如实给出 publish_blockers（不具备完整发布证据）
    cert_out = _certify(ctx, with_holdout=False, with_comparison=False)
    assert cert_out["ok"] is True
    assert set(cert_out["publish_blockers"]) == {"no_sealed_holdout_verification", "no_same_protocol_active_comparison"}
    counts = cert_out["certificate"]["sample_counts"]
    assert counts["S"] == 2 and counts["V"] == 0 and counts["H"] == 0
    assert counts["online_usage"] == 0 and counts["counts_verified"] is True
    assert "不填造" in counts["note"]

    # 显式采用（带未满足条件也如实记录）→ 回 experimental，缺真实使用证据不晋级
    adoption = cert.adopt_certified_candidate(
        certificate=cert_out["certificate"], packaged=ctx["packaged"], actor="offline-release"
    )
    assert adoption["ok"] is True, adoption
    assert adoption["tier"] == "experimental"
    assert adoption["adoption"]["adopted_with_blockers"] is True
    assert adoption["messaging_authorization"] == "not_granted"
    assert st.get_tier(name) == "experimental"

    # 存在采用记录后：成绩未绑定评测协议 → 拒绝晋级（保持 experimental）
    unbound = _promotion_report(name, "healthy", promoted=True, artifacts_extra={"active_snapshot_hash": adoption["adoption"]["certified_final_content_hash"]})
    out = st.apply_eval_promotions(unbound)
    assert out["promoted"] == []
    blocked = [s for s in out["skipped"] if s.get("gate_code") == "protocol_unbound"]
    assert blocked, out["skipped"]
    assert st.get_tier(name) == "experimental"

    # 非 healthy 的真实成绩也不晋级（缺证据保持 experimental 的另一形态）
    weak = _promotion_report(
        name,
        "incubating",
        promoted=True,
        artifacts_extra={"eval_protocol_fingerprint": "real-usage-proto-1", "active_snapshot_hash": adoption["adoption"]["certified_final_content_hash"]},
    )
    out2 = st.apply_eval_promotions(weak)
    assert out2["promoted"] == []
    assert st.get_tier(name) == "experimental"


# ── 场景 3（S3）：H 一次性语义 + 旧曲线按已暴露登记 ───────────────────────────


def test_s3_holdout_one_shot_no_second_place_and_exposure_registration(sandbox):
    ctx = _setup_search(sandbox)
    manifest = ctx["manifest"]
    seal_out = cert.create_holdout_seal(
        manifest=manifest, locked=ctx["lock"], sealed_holdout=_sealed_holdout(), h_cases=_h_cases()
    )
    assert seal_out["ok"] is True
    seal = seal_out["seal"]

    # 逐题回执缺题 → 拒绝（不填零、不编造）
    incomplete = cert.record_holdout_attempt(
        manifest=manifest, locked=ctx["lock"], seal=seal, verdicts={}
    )
    assert incomplete["ok"] is False and incomplete["error"] == "holdout_verdict_incomplete"

    # 依 H 择第二名：第二个候选不得使用同一个 H（锁也不得改锁）
    other_lock = cert.lock_champion_candidate(
        manifest=manifest,
        champion_selection={"ok": True, "search_champion": "cand-b", "algorithm_version": "x"},
    )
    assert other_lock["ok"] is False and other_lock["error"] == "champion_already_locked"
    mismatch = cert.record_holdout_attempt(
        manifest=manifest,
        locked={"candidate_id": "cand-b", "protocol_fingerprint": manifest.eval_protocol_fingerprint},
        seal=seal,
        verdicts={"case-h1": {"passed": True, "score": 1.0}},
    )
    assert mismatch["ok"] is False and mismatch["error"] == "holdout_candidate_mismatch"

    # 锁定候选 A 的 H 失败 → 本轮不发布；seal 已消耗
    failed = cert.record_holdout_attempt(
        manifest=manifest,
        locked=ctx["lock"],
        seal=seal,
        verdicts={"case-h1": {"passed": False, "score": 0.2}},
    )
    assert failed["ok"] is True and failed["passed"] is False
    assert failed["seal"]["spent"] is True
    assert failed["seal"]["spent_reason"] == "holdout_failed_no_repick"

    # 继续调整后（新候选 cand-c）仍想用同一 H → 拒绝（one-shot）
    retry = cert.record_holdout_attempt(
        manifest=manifest,
        locked={"candidate_id": "cand-c", "protocol_fingerprint": manifest.eval_protocol_fingerprint},
        seal=failed["seal"],
        verdicts={"case-h1": {"passed": True, "score": 1.0}},
    )
    assert retry["ok"] is False and retry["error"] == "holdout_seal_spent"

    # 认证拒绝：H 失败 → 不发布、不择第二名
    cert_out = cert.certify_candidate(
        manifest=manifest,
        locked=ctx["lock"],
        packaged=ctx["packaged"],
        holdout_seal=failed["seal"],
        active_comparison={
            "eval_protocol_fingerprint": manifest.eval_protocol_fingerprint,
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
        },
    )
    assert cert_out["ok"] is False and cert_out["error"] == "holdout_failed"
    assert "择第二名" in cert_out["reason"]

    # 封存记录落盘即 spent；旧曲线按已暴露登记（复用原污染登记）
    reloaded = cert.load_holdout_seal(manifest)
    assert reloaded and reloaded["spent"] is True
    registry_out = cert.retire_holdout_as_exposed(
        seal=reloaded, path=sandbox / "holdout-registry.json"
    )
    assert registry_out["ok"] is True and registry_out["retired_as"] == "selection_or_dev"
    registry = registry_out["registry"]
    assert "case-h1" in registry["taskIds"]

    # 已暴露的题不得再充当新封存 H（新搜索的角色授予拒绝）
    reassigned = sp.assign_protocol_roles(
        [dict(c, protocol_role=sp.ROLE_H) for c in _h_cases()],
        sealed_holdout=_sealed_holdout(),
        contamination=registry,
    )
    assert reassigned["ok"] is False
    codes = {v["code"] for v in reassigned["violations"]}
    assert "contaminated_case_not_sealed" in codes


# ── 场景 4（S4）：搜索期间 active 被更新/投影重建不得覆盖；回退回执如实分离 ──


def test_s4_adopt_refuses_source_changed_and_projection_takeover(sandbox):
    ctx = _setup_search(sandbox)
    name = ctx["skill_name"]
    cert_out = _certify(ctx)
    assert cert_out["ok"] is True, cert_out
    certificate = cert_out["certificate"]

    # 搜索期间用户在线演化了 active（新用户决定）
    evolved = evolve_skill(name, lesson="用户在线修订", instructions="用户新决定的正文，优先级更高。")
    assert evolved.get("ok") is True
    file = ctx["skill_dir"] / "SKILL.md"
    user_text = file.read_text(encoding="utf-8-sig")
    assert "用户新决定" in user_text

    # CAS：expected_active_sha256 绑定搜索冻结的种子字节 → source_changed 拒绝，不覆盖
    adoption = cert.adopt_certified_candidate(certificate=certificate, packaged=ctx["packaged"])
    assert adoption["ok"] is False and adoption["error"] == "source_changed"
    assert "不得覆盖" in adoption["note"]
    assert "用户新决定" in file.read_text(encoding="utf-8-sig")  # 新决定原样保留
    assert not cert.load_record_for_skill(name, cert.ADOPTION_RECORD_NAME)

    # 投影重建接管：源服务把 active 标记为托管投影 → 发布入口拒绝认领
    drift_cert = cert.certify_candidate(
        manifest=ctx["manifest"],
        locked=ctx["lock"],
        packaged=ctx["packaged"],
        active_comparison={
            "eval_protocol_fingerprint": ctx["manifest"].eval_protocol_fingerprint,
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
        },
    )
    assert drift_cert["ok"] is False and drift_cert["error"] == "blocked_source_changed"
    takeover = file.read_text(encoding="utf-8-sig").replace(
        "producer-kind: executable_sop", "producer-kind: projection\nmanaged-by: experience.skill_sync"
    )
    file.write_text(takeover, encoding="utf-8")
    reset_skill_cache()
    adoption2 = cert.adopt_certified_candidate(certificate=certificate, packaged=ctx["packaged"])
    assert adoption2["ok"] is False and adoption2["error"] == "managed_projection_source_owned"
    assert "用户新决定" in file.read_text(encoding="utf-8-sig")  # 投影正文未被搜索覆盖


def test_s4_rollback_receipt_separates_tier_and_content(sandbox):
    ctx = _setup_search(sandbox)
    name = ctx["skill_name"]
    _seed_experimental_tier(name)
    pre_adoption_sha = _sha_file(ctx["skill_dir"] / "SKILL.md")
    cert_out = _certify(ctx)
    assert cert_out["ok"] is True
    adoption = cert.adopt_certified_candidate(
        certificate=cert_out["certificate"], packaged=ctx["packaged"], actor="offline-release"
    )
    assert adoption["ok"] is True
    certified_sha = adoption["adoption"]["released"]["sha256"]

    # 晋级 staging（真实成绩：绑定采用的制品 hash + 协议指纹）
    report = _promotion_report(
        name,
        "healthy",
        promoted=True,
        artifacts_extra={
            "active_snapshot_hash": adoption["adoption"]["certified_final_content_hash"],
            "eval_protocol_fingerprint": "real-usage-proto-1",
        },
    )
    out = st.apply_eval_promotions(report)
    assert out["promoted"] and out["promoted"][0]["skill"] == name, out
    assert st.get_tier(name) == "staging"

    # 回退回执：tier 已降级，但内容仍是认证候选制品 → content_restored=False 如实报告
    receipt = cert.rollback_receipt(name, reason="staging 回退演练")
    assert receipt["ok"] is True
    assert receipt["tier"]["from_tier"] == "staging" and receipt["tier"]["to_tier"] == "experimental"
    assert receipt["tier_rolled_back"] is True
    assert receipt["content"]["matches_certificate"] is True
    assert receipt["content"]["current_sha256"] == certified_sha
    assert receipt["content_restored"] is False
    assert receipt["next_action"] == "restore_content"
    assert "不等于内容回退" in receipt["note"]
    assert st.get_tier(name) == "experimental"

    # 内容恢复另走 restore_content（tier 不动），恢复后回执可确认一致
    restored = st.restore_content(name, actor="offline-release")
    assert restored["ok"] is True, restored
    assert _sha_file(ctx["skill_dir"] / "SKILL.md") == pre_adoption_sha
    assert st.get_tier(name) == "experimental"  # restore 不改 tier

    # 重新晋级后再回退：提供期望恢复 hash → 回执确认两条记录一致
    report_restored = _promotion_report(
        name,
        "healthy",
        promoted=True,
        artifacts_extra={
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
            "eval_protocol_fingerprint": "real-usage-proto-2",
        },
    )
    out2 = st.apply_eval_promotions(report_restored)
    assert out2["promoted"], out2
    receipt2 = cert.rollback_receipt(name, expected_restored_sha256=pre_adoption_sha)
    assert receipt2["ok"] is True
    assert receipt2["content_restored"] is True
    assert receipt2["content"]["restored"] is True
    assert st.get_tier(name) == "experimental"


# ── 场景 5（S5）：core 人工晋级保持；原生 experimental 技能不被全局禁用 ───────


def test_s5_core_manual_gate_and_native_experimental_paths_unaffected(sandbox):
    # 原生技能（无任何搜索认证记录）：legacy 晋级报告零变化
    created = create_skill(name="native-exp", description="d", instructions="原生沉淀正文。")
    assert created.get("ok") is True
    assert st.get_tier("native-exp") == "experimental"

    states = cert.certification_state("native-exp")
    assert list(states["states"].keys()) == list(cert.STATE_LADDER)
    assert all(not s["active"] for s in states["states"].values())
    assert states["current_tier"] == "experimental"

    legacy = _promotion_report("native-exp", "healthy", promoted=True)  # 旧形态：无协议指纹
    out = st.apply_eval_promotions(legacy)
    assert out["promoted"] and out["promoted"][0]["skill"] == "native-exp"
    assert st.get_tier("native-exp") == "staging"  # 不因认证要求被禁用

    # 晋级门永不自动到 core：再次消费报告不改变 tier；core 只走人工入口
    out2 = st.apply_eval_promotions(_promotion_report("native-exp", "healthy", promoted=True))
    assert out2["promoted"] == []
    assert st.get_tier("native-exp") == "staging"
    core_attempt = cert.certification_state("native-exp")
    assert core_attempt["states"]["staging"]["active"] is True
    assert core_attempt["states"]["core"]["active"] is False

    # manual_promote_core 只接受 staging → core（人工明确决定）
    assert st.manual_promote_core("native-exp")["ok"] is True
    assert st.get_tier("native-exp") == "core"
    assert cert.certification_state("native-exp")["states"]["core"]["active"] is True

    # core 不可自动降级/自动晋级：rollback 只作用于 staging；报告不再改 core
    assert st.rollback("native-exp")["ok"] is False
    out3 = st.apply_eval_promotions(_promotion_report("native-exp", "healthy", promoted=True))
    assert out3["promoted"] == []
    assert st.get_tier("native-exp") == "core"


# ── 五状态区分 + 报告字段接线 ─────────────────────────────────────────────────


def test_five_states_distinct_across_lifecycle_and_report_field_wired(sandbox):
    ctx = _setup_search(sandbox)
    name = ctx["skill_name"]
    _seed_experimental_tier(name)

    # 1) 只有 search_champion
    s1 = cert.certification_state(name)
    assert s1["states"]["search_champion"]["active"] is True
    assert s1["states"]["certified_candidate"]["active"] is False
    assert s1["states"]["adopted_experimental"]["active"] is False

    # 2) 认证后 certified_candidate 亮起（tier 仍 experimental）
    cert_out = _certify(ctx)
    assert cert_out["ok"] is True
    s2 = cert.certification_state(name)
    assert s2["states"]["search_champion"]["active"] is True
    assert s2["states"]["certified_candidate"]["active"] is True
    assert s2["states"]["adopted_experimental"]["active"] is False
    assert s2["states"]["staging"]["active"] is False and s2["states"]["core"]["active"] is False

    # 3) 显式采用后 adopted_experimental 亮起（tier 回 experimental）
    adoption = cert.adopt_certified_candidate(
        certificate=cert_out["certificate"], packaged=ctx["packaged"], actor="offline-release"
    )
    assert adoption["ok"] is True
    s3 = cert.certification_state(name)
    assert s3["states"]["adopted_experimental"]["active"] is True
    assert s3["current_tier"] == "experimental"
    assert s3["messaging_authorization"] == "not_granted"

    # 4) 真实成绩晋级 staging；5) core 人工
    report = _promotion_report(
        name,
        "healthy",
        promoted=True,
        artifacts_extra={
            "active_snapshot_hash": adoption["adoption"]["certified_final_content_hash"],
            "eval_protocol_fingerprint": "real-usage-proto-1",
        },
    )
    assert st.apply_eval_promotions(report)["promoted"]
    s4 = cert.certification_state(name)
    assert s4["states"]["staging"]["active"] is True and s4["current_tier"] == "staging"
    assert st.manual_promote_core(name)["ok"] is True
    s5 = cert.certification_state(name)
    assert s5["states"]["core"]["active"] is True and s5["states"]["staging"]["active"] is False

    # 五状态在任何时刻都显式分开（五个键、各自 active），认证不折叠为 tier
    for states in (s1, s2, s3, s4, s5):
        assert list(states["states"].keys()) == list(cert.STATE_LADDER)
        assert sum(1 for s in states["states"].values() if s["active"]) <= len(cert.STATE_LADDER)

    # 在线评测报告接线：deployment_states 字段逐 skill 输出五状态阶梯
    create_skill(name="wire-skill", description="d", instructions="i")
    reset_skill_cache()
    report = asyncio.run(evaluate_online_skill_evolution_async(write_report=False, write_artifacts=False))
    items = [s for s in report["skills"] if s["skill"] == "wire-skill"]
    assert items, [s["skill"] for s in report["skills"]]
    field = items[0]["deployment_states"]
    assert list(field["states"].keys()) == list(cert.STATE_LADDER)
    assert field["certification_only"] is True
    assert all(not s["active"] for s in field["states"].values())
    direct = _deployment_states_field(name)
    assert direct["states"]["core"]["active"] is True  # 手动 core 后字段如实反映


# ── 认证门细化：包装/同制品/协议对照的拒绝面 ─────────────────────────────────


def test_certification_rejects_wrapper_hash_gap_cross_artifact_and_cross_protocol(sandbox):
    ctx = _setup_search(sandbox)
    manifest = ctx["manifest"]

    # 对照 active 跨协议 → 拒绝（旧冠军不同日期/模型/样本的历史均分不可当对照）
    cross = cert.certify_candidate(
        manifest=manifest,
        locked=ctx["lock"],
        packaged=ctx["packaged"],
        active_comparison={
            "eval_protocol_fingerprint": "some-other-proto",
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
        },
    )
    assert cross["ok"] is False and cross["error"] == "active_comparison_protocol_mismatch"

    # 最终包装被替换为另一份制品（bundle hash 与复算不符）→ 拒绝
    tampered = dict(ctx["packaged"])
    tampered["final_body"] = ctx["packaged"]["final_body"] + "\n\n## Evolution Notes\n\n- 冒称包装"
    tampered_out = cert.certify_candidate(
        manifest=manifest,
        locked=ctx["lock"],
        packaged=tampered,
        active_comparison={
            "eval_protocol_fingerprint": manifest.eval_protocol_fingerprint,
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
        },
    )
    assert tampered_out["ok"] is False
    assert tampered_out["error"] in {"wrapper_not_reflected_in_hash", "final_artifact_not_locked_candidate"}

    # 解包后的裸候选不是锁定候选（测 A 认 B）→ 拒绝
    other_packaged = dict(ctx["packaged"])
    other_packaged["release_bundle_sha256"] = "f" * 64
    other_out = cert.certify_candidate(
        manifest=manifest,
        locked=ctx["lock"],
        packaged=other_packaged,
        active_comparison={
            "eval_protocol_fingerprint": manifest.eval_protocol_fingerprint,
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
        },
    )
    assert other_out["ok"] is False and other_out["error"] == "final_artifact_not_locked_candidate"

    # 未锁定候选 → 拒绝认证
    no_lock = cert.certify_candidate(
        manifest=manifest,
        locked={},
        packaged=ctx["packaged"],
        active_comparison={
            "eval_protocol_fingerprint": manifest.eval_protocol_fingerprint,
            "active_snapshot_hash": _content_hash_of(SOP_TEXT),
        },
    )
    assert no_lock["ok"] is False and no_lock["error"] == "lock_required"

    # 锁定候选协议与 manifest 不一致 → 拒绝
    bad_fp_lock = cert.certify_candidate(
        manifest=manifest,
        locked={"candidate_id": "cand-a", "content_hash": ctx["candidate_content_hash"], "protocol_fingerprint": "stale-fp"},
        packaged=ctx["packaged"],
    )
    assert bad_fp_lock["ok"] is False and bad_fp_lock["error"] == "lock_protocol_mismatch"


# ── R08 追加：冠军锁定 / H 一次性登记的「读后写」原子性（并发恰好一个成功） ────


def test_concurrent_champion_lock_single_writer_wins(sandbox):
    """两实例并发锁定不同候选：判定与写入同持命名空间锁 → 恰好一个锁定成功。"""
    ctx = _setup_search(sandbox)
    manifest = ctx["manifest"]
    lock_path = sc.search_namespace(manifest) / cert.CHAMPION_LOCK_NAME
    assert lock_path.is_file()
    lock_path.unlink()  # 从未锁定状态开始并发竞写

    outcomes = {}
    start = threading.Barrier(2)

    def lock_worker(name, champion_id):
        start.wait(timeout=30)
        outcomes[name] = cert.lock_champion_candidate(
            manifest=manifest,
            champion_selection={"ok": True, "search_champion": champion_id,
                                "algorithm_version": "x"},
        )

    threads = [threading.Thread(target=lock_worker, args=("thread-a", "cand-a")),
               threading.Thread(target=lock_worker, args=("thread-b", "cand-b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    won = [name for name, result in outcomes.items() if result.get("ok") is True]
    assert len(won) == 1, outcomes
    loser = outcomes[[name for name in outcomes if name not in won][0]]
    assert loser["error"] == "champion_already_locked"
    on_disk = json.loads(lock_path.read_text(encoding="utf-8"))
    assert on_disk["candidate_id"] == outcomes[won[0]]["lock"]["candidate_id"]


def test_concurrent_holdout_attempt_one_shot_single_winner(sandbox):
    """两线程并发消耗同一未 spent 封存集：恰好一个登记成功，另一个 holdout_seal_spent。"""
    ctx = _setup_search(sandbox)
    manifest = ctx["manifest"]
    seal_out = cert.create_holdout_seal(
        manifest=manifest, locked=ctx["lock"], sealed_holdout=_sealed_holdout(), h_cases=_h_cases()
    )
    assert seal_out["ok"] is True
    outcomes = {}
    start = threading.Barrier(2)

    def attempt_worker(name):
        start.wait(timeout=30)
        outcomes[name] = cert.record_holdout_attempt(
            manifest=manifest, locked=ctx["lock"], seal=seal_out["seal"],
            verdicts={"case-h1": {"passed": True, "score": 1.0}},
        )

    threads = [threading.Thread(target=attempt_worker, args=("thread-a",)),
               threading.Thread(target=attempt_worker, args=("thread-b",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    won = [name for name, result in outcomes.items() if result.get("ok") is True]
    assert len(won) == 1, outcomes
    loser = outcomes[[name for name in outcomes if name not in won][0]]
    assert loser["error"] == "holdout_seal_spent"
    on_disk = json.loads(
        (sc.search_namespace(manifest) / cert.HOLDOUT_SEAL_NAME).read_text(encoding="utf-8"))
    assert on_disk["spent"] is True
    assert len(on_disk["attempts"]) == 1  # H 只消耗一次：无并发双登记
    assert on_disk["attempts"][0]["attempt_id"] == outcomes[won[0]]["attempt"]["attempt_id"]
