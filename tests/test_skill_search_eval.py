"""DE-36 搜索增强独立验收回归：运行面/发布门/诚实语义/公平对照/兼容矩阵/双线报告。

对应 tasks.json DE-36 四条 acceptance_scenarios + implementation_steps 4/5
（全部离线：确定性合成执行器/固定时钟/DE-15 真实预算端口 + sqlite/tmp_path）：

1. S1 Agent 可创建/查状态/暂停恢复取消并定位失败，不需读巨大 Trace；
2. S2 跨能力修好一题破坏另一题：探索允许留存（池），正式交付按发布门否决；
3. S3 无预算/无真实样本报告未执行而非已提升；回归前后统计可核对；
4. S4 同触发/正文/组合案例的公平成本/任务对照；投影文件没有被搜索覆盖；
5. 兼容矩阵：NATIVE-ARCHITECTURE-INTEGRATION §10 逐行有真实回归 + 关键开关
   关闭组合实测（在线抽取/tier/冠军门/优化器默认关闭）；
6. 双线报告结构：基础试点与搜索增强分开，不以方案交付结项。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agents.skill_search import certification as cert  # noqa: E402
from agents.skill_search import contracts as sc  # noqa: E402
from agents.skill_search import controller as ct  # noqa: E402
from agents.skill_search import evaluator as ev  # noqa: E402
from agents.skill_search import feedback as fb  # noqa: E402
from agents.skill_search import modules as sm  # noqa: E402
from agents.skill_search import protocol as sp  # noqa: E402
from agents.skill_search import selection as sel  # noqa: E402
from agents.skill_search.contracts import check_frozen_write, write_candidate_artifact  # noqa: E402
from agents.skill_search.controller import ControllerConfig, FaultInjected  # noqa: E402
from eval import skill_search_eval as se  # noqa: E402

SKILL_TEXT = """---
name: deploy-rollout
description: 部署发布独立 SOP
when_to_use: deploy release canary rollout
producer-kind: executable_sop
version: 1.0.0
---

## 工作流

1. 灰度 5% 观察 10 分钟。
2. 全量并回填结果。

## 安全边界

只读保护内容：不得越权改生产配置。
"""

MODULE_DEFS = [
    {"module_id": "cap.workflow", "type": "capability.workflow", "writable": True, "heading": "## 工作流"},
    {"module_id": "guard.readonly", "type": "shared.guardrails", "writable": False, "heading": "## 安全边界"},
]

READY = {
    "de30_evaluator": True,
    "de31_feedback": True,
    "de32_mutator": True,
    "local_baseline": True,
}

SEED_SCORES = {"case-1": 0.2, "case-2": 1.0}
CHILD_SCORES = {"case-1": 1.0, "case-2": 1.0, "case-v": 1.0}


# ── 合成夹具（与 DE-34 控制器回归同构；确定性、离线） ────────────────────────


def _case(case_id: str, *, role: str = "S") -> dict:
    return {
        "case_id": case_id,
        "origin_group": "score",
        "origin_split": "mutate_dev",
        "family": "deploy",
        "question": f"case {case_id} 执行部署发布任务 topic-{case_id}-alpha",
        "expected": f"case {case_id} 期望：灰度比例 10 percent 且生成发布摘要",
        "protocol_role": role,
    }


def _trace(case_id: str) -> list[dict]:
    return [
        {"event_id": "e-1", "kind": "user_message", "text": f"case {case_id} 用户消息"},
        {"event_id": "e-2", "kind": "tool_call", "text": f"tool deploy_rollout set canary ratio 5 percent ({case_id})"},
        {"event_id": "e-3", "kind": "verdict", "text": f"verifier failed canary ratio mismatch ({case_id})"},
    ]


class ScriptedExecutor:
    """合成执行器：按 (candidate_id, case_id) 脚本打分；记录调用与回执。"""

    adapter_id = "synthetic-de36-runtime"

    def __init__(self, scores: dict[str, dict[str, float]], *, usage: int = 5):
        self.scores = scores
        self.usage = usage
        self.calls: list[tuple[str, str]] = []
        self.receipts: dict[tuple[str, str], dict] = {}

    async def execute(self, *, case, candidate, frozen_protocol):
        candidate_id = str(candidate["candidate_id"])
        case_id = str(case["case_id"])
        self.calls.append((candidate_id, case_id))
        score = float(self.scores.get(candidate_id, {}).get(case_id, 0.0))
        receipt = ev.ExecutionOutcome(
            case_id=case_id,
            candidate_id=candidate_id,
            terminal_status=ev.TERMINAL_PASS if score >= 1.0 else ev.TERMINAL_SCORED_FAIL,
            execution_level=ev.LEVEL_TOOL_REPLAY,
            valid=True,
            score=score,
            dimension_scores={"accuracy": score},
            loaded_hash=str(candidate.get("content_hash") or ""),
            expected_loaded_hash=str(candidate.get("content_hash") or ""),
            response_ref=f"response://{candidate_id}/{case_id}",
            artifact_verified=True,
            task_success=score >= 1.0,
            cost={"used_tokens": self.usage},
            injection={"channel": "isolated_mount", "candidate_id": candidate_id},
            protocol_fingerprint=str((frozen_protocol or {}).get("fingerprint") or ""),
        ).to_dict()
        self.receipts[(candidate_id, case_id)] = receipt
        return ev.ExecutionOutcome(**{k: v for k, v in receipt.items()})

    def get_receipt(self, candidate_id: str, case_id: str):
        return self.receipts.get((candidate_id, case_id))


class Attributor:
    """确定性归因器：对父代全部 failed Case 产出可核验的 SKILL_ISSUE 提案。"""

    def __call__(self, ctx):
        proposals = []
        for evaluation in ctx.get("evaluations") or []:
            if str(evaluation.get("terminal_status") or "") != fb.TERMINAL_SCORED_FAIL:
                continue
            case_id = str(evaluation.get("case_id") or "")
            proposals.append({
                "case_id": case_id,
                "category": fb.ATTR_SKILL_ISSUE,
                "sub_kind": fb.SKILL_SUB_BODY_DEFECT,
                "module_id": "cap.workflow",
                "citations": [{"event_ref": f"trace://{case_id}#e-2"}],
                "observed": "canary ratio set to 5 percent",
                "expected": "canary ratio should be 10 percent",
                "failure_atoms": ["canary ratio wrong"],
                "confidence": 0.9,
            })
        return proposals


class PatchProvider:
    """确定性 patch：把灰度比例 5% 改为 10%（绑定父版本 hash 的 replace 操作）。"""

    def __call__(self, brief):
        section = str(brief["module_text"])
        base_sha = str((brief.get("mutation_brief") or {}).get("base_text_sha256") or "")
        old = "灰度 5% 观察 10 分钟。"
        start = section.index(old)
        return {
            "target_module": "cap.workflow",
            "base_text_sha256": base_sha,
            "ops": [
                {
                    "op": "replace",
                    "start": start,
                    "end": start + len(old),
                    "expected": old,
                    "replacement": "灰度 10% 观察 10 分钟。",
                    "module": "cap.workflow",
                }
            ],
            "rationale": "align canary ratio with expected 10 percent",
        }


class FakeIsolation:
    def mount(self, candidate_id: str, text: str) -> dict:
        return {"ok": True, "mounted": True, "candidate_id": candidate_id}

    def restore_seed(self) -> dict:
        return {"ok": True, "restored": True, "mode": "seed_mount_restored"}


def _make_budget(tmp_path: Path, *, space: str = "ops"):
    from evoharness.governance import budget as de15
    from evoharness.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db = tmp_path / f"budget-{space}.db"
    engine = create_engine("sqlite:///" + str(db.as_posix()), future=True, connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        de15.set_limit(session, scope_kind="org", scope_id="org-1", limit_tokens=100_000)
        de15.set_limit(session, scope_kind="space", scope_id=space, limit_tokens=100_000)
        session.commit()
    return ct.De15BudgetPort(factory, org_id="org-1", space_id=space)


def _components(tmp_path: Path, executor: ScriptedExecutor, *, config: ControllerConfig | None = None) -> se.SearchComponents:
    return se.SearchComponents(
        executor=executor,
        budget=_make_budget(tmp_path),
        attributor=Attributor(),
        patch_provider=PatchProvider(),
        isolation=FakeIsolation(),
        trace_store=fb.TraceStore({f"trace://{cid}": _trace(cid) for cid in ("case-1", "case-2", "case-v")}),
        config=config
        or ControllerConfig(
            prerequisites=dict(READY),
            trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
            max_iterations=1,
            max_case_workers=1,
        ),
        clock=lambda: 1000.0,
    )


def _skill_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "deploy-rollout"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
    return directory


@pytest.fixture()
def evo_root(tmp_path, monkeypatch):
    """隔离进化台账根（搜索命名空间/tier 审计/认证索引全部落 tmp）。"""
    root = tmp_path / "evo-root"
    monkeypatch.setenv("EVOHARNESS_EVOLUTION_DIR", str(root))
    monkeypatch.setattr("agents.session.SESSION_DIR", tmp_path / "sessions")
    return root


# ═══ S1：Agent 可创建/查状态/暂停/恢复/取消并定位失败 ═══════════════════════


def test_s1_agent_can_create_status_pause_resume_cancel_and_locate_failure(tmp_path, evo_root):
    executor = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
    runtime = se.SkillSearchRuntime()
    world = _skill_dir(tmp_path)
    components = _components(tmp_path, executor)

    created = runtime.create_search(
        skill_dir=world,
        cases=[_case("case-1"), _case("case-2"), _case("case-v", role="V")],
        tenant="acme",
        space="ops",
        module_defs=MODULE_DEFS,
        components=components,
        search_id="s-de36-s1",
    )
    assert created["ok"] is True, created
    sid = created["search_id"]
    assert sid == "s-de36-s1"
    # 无故障完整跑完：终态 completed 且 can_publish（隔离恢复回执有效）
    assert created["result"]["status"] == "completed"
    assert created["result"]["can_publish"] is True

    # ── 查状态（只读、跨实例） ──
    status = se.SkillSearchRuntime().status(sid)
    assert status["status"] == "completed"
    assert status["next_action"]["code"] == "request_certification"

    # ── 定位失败：基线 case-1 低分失败，证据是引用而非轨迹正文 ──
    report = runtime.locate_failure(sid)
    failed_ids = {f["case_id"] for f in report["failures"]}
    assert "case-1" in failed_ids
    entry = next(f for f in report["failures"] if f["case_id"] == "case-1")
    assert {"kind": "trace_ref", "ref": "trace://case-1"} in entry["evidence"]
    assert all(ev_item["kind"] in {"trace_ref", "response_ref"} for ev_item in entry["evidence"])
    dumped = json.dumps(report, ensure_ascii=False)
    assert "tool deploy_rollout" not in dumped  # 不复制轨迹正文（不读巨大 Trace）
    assert report["next_action"]["code"] in {"request_certification", "review_finalization"}

    # ── 详情：状态/预算/逐Case得失/差异/下一动作/认证五状态/证据 ──
    detail = se.SkillSearchRuntime().detail(sid)
    assert detail["search_id"] == sid
    assert detail["state"]["phase"] == "completed"
    assert detail["budget"]["used_tokens_total"] > 0
    assert detail["budget"]["open_reservations"] == 0  # 无遗留开放预留
    assert detail["case_gains"]["case-1"]["baseline"] == pytest.approx(0.2)
    assert detail["case_gains"]["case-1"]["best"] == pytest.approx(1.0)
    assert detail["case_gains"]["case-1"]["delta"] == pytest.approx(0.8)
    assert detail["diffs"]["champion"]["candidate_id"]
    assert detail["next_action"]["code"] == "request_certification"
    states = detail["certification"]["states"]
    assert set(states) == set(cert.STATE_LADDER)
    assert states["search_champion"]["active"] is False  # 未走认证锁定时五状态如实为未到达
    assert detail["evidence"]["state_sha256"]

    # ── 非暂停状态 resume：不误清标记（not_paused）；终态 run 幂等 ──
    resumed = runtime.resume(sid)
    assert resumed["ok"] is False and resumed["error"] == "not_paused"
    rerun = runtime.run(sid)
    assert rerun["result"]["status"] == "completed"  # 终态搜索 run 幂等，不重启


def test_s1_pause_then_resume_continues_from_breakpoint(tmp_path, evo_root):
    """故障崩溃 → 暂停 → run 停在 paused → resume 清标记并从断点续跑至完成。"""
    crashed: list[str] = []

    def fault_hook(stage: str) -> None:
        if stage == "baseline_evaluating" and stage not in crashed:
            crashed.append(stage)
            raise FaultInjected("fault@baseline_evaluating")

    executor = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
    components = _components(tmp_path, executor)
    world = _skill_dir(tmp_path)

    # 故障注入走控制器级构造（runtime.create_search 覆盖常规创建；本例验证
    # 崩溃后跨实例 pause/run/resume 的断点语义）。
    raw_text = (world / sc.SKILL_TEXT_NAME).read_text(encoding="utf-8-sig")
    spec_out = sm.build_module_spec(raw_text, MODULE_DEFS, skill_name="")
    assert spec_out["ok"] is True
    cases = [_case("case-1"), _case("case-2"), _case("case-v", role="V")]
    assigned = sp.assign_protocol_roles(cases)
    freeze_out = sp.freeze_case_protocol(assigned["cases"])
    freeze = freeze_out["freeze"]
    manifest_out = sc.build_search_manifest(
        skill_dir=world,
        tenant="acme",
        space="ops",
        protocol=sc.freeze_protocol(
            splits=sp.protocol_freeze_entry(freeze),
            algorithm_version=se.EVAL_VERSION,
            models={"evaluator": "synthetic"},
        ),
        module_spec=spec_out["spec"],
        search_id="s-de36-s1-fault",
    )
    assert manifest_out["ok"] is True
    manifest = manifest_out["manifest"]
    sc.save_manifest(manifest, base_dir=evo_root / "search")
    config = components.config
    controller = ct.SearchController(
        manifest=manifest,
        cases=assigned["cases"],
        freeze=freeze,
        module_spec=spec_out["spec"],
        executor=executor,
        budget=components.budget,
        config=config,
        base_dir=evo_root / "search",
        attributor=components.attributor,
        patch_provider=components.patch_provider,
        isolation=components.isolation,
        trace_store=components.trace_store,
        fault_hook=fault_hook,
        clock=lambda: 1000.0,
    )
    with pytest.raises(FaultInjected):
        controller.start(worker="agent")

    sid = str(manifest.search_id)
    # 暂停（跨实例：新 runtime 实例仅经落盘状态操作）
    other = se.SkillSearchRuntime(base_dir=evo_root / "search")
    assert other.pause(sid, "maintenance window")["ok"] is True
    # 持有组件的运行器 run：停在暂停标记，不推进阶段
    holder = se.SkillSearchRuntime(base_dir=evo_root / "search")
    holder._attached[sid] = controller
    stopped = holder.run(sid)
    assert stopped["result"]["status"] == "paused"
    status = other.status(sid)
    assert status["status"] == "paused"
    assert status["next_action"]["code"] == "resume"
    # 恢复：清除暂停标记并从断点续跑（故障只注入一次）→ 完成
    resumed = holder.resume(sid)
    assert resumed["ok"] is True
    assert resumed["result"]["status"] == "completed"
    # 每个 (候选, Case) 不重跑：基线 case 调用数不变
    baseline_seed_calls = sum(1 for c in executor.calls if c[0] == "seed")
    assert baseline_seed_calls == 2  # case-1/case-2 各一次
    assert other.status(sid)["status"] == "completed"


def test_s1_cancel_stops_with_no_new_side_effects(tmp_path, evo_root):
    """取消：标记后 run 直接落 cancelled 终态，不再发起新调用。"""
    executor = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
    components = _components(tmp_path, executor)
    runtime = se.SkillSearchRuntime()
    created = runtime.create_search(
        skill_dir=_skill_dir(tmp_path),
        cases=[_case("case-1"), _case("case-2"), _case("case-v", role="V")],
        tenant="acme",
        space="ops2",
        module_defs=MODULE_DEFS,
        components=components,
        search_id="s-de36-s1-cancel",
    )
    assert created["ok"] is True
    sid = created["search_id"]
    calls_before = len(executor.calls)
    cancelled = runtime.cancel(sid, "operator request")
    assert cancelled["ok"] is True
    rerun = runtime.run(sid)
    assert rerun["result"]["status"] == "cancelled"
    assert len(executor.calls) == calls_before  # 取消后零新调用
    status = runtime.status(sid)
    assert status["status"] == "cancelled"
    assert status["next_action"]["code"] == "reopen_new_search"


def test_s1_cli_and_server_share_same_search_id_and_evidence(tmp_path, evo_root, capsys):
    """CLI（--detail）与 server API（/api/skills/search/detail）引用同一 search ID 与证据。"""
    executor = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
    runtime = se.SkillSearchRuntime()
    created = runtime.create_search(
        skill_dir=_skill_dir(tmp_path),
        cases=[_case("case-1"), _case("case-2"), _case("case-v", role="V")],
        tenant="acme",
        space="ops3",
        module_defs=MODULE_DEFS,
        components=_components(tmp_path, executor),
        search_id="s-de36-share",
    )
    assert created["ok"] is True
    sid = created["search_id"]

    # CLI 侧
    code = se.main(["--detail", "--search-id", sid, "--json"])
    assert code == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["search_id"] == sid
    # server 侧（直接调用端点协件：同一函数 build_detail_report）
    from server.app import skills_search_detail

    server_payload = asyncio.run(skills_search_detail(search_id=sid))
    assert server_payload["search_id"] == cli_payload["search_id"]
    assert server_payload["evidence"]["state_sha256"] == cli_payload["evidence"]["state_sha256"]
    assert server_payload["next_action"] == cli_payload["next_action"]
    # Skill 摘要（/api/skills 接线共用）：按 lineage 定位同一 search ID
    summary = se.skill_search_summary("deploy-rollout")
    assert summary["found"] is True and summary["search_id"] == sid
    # 未知名 → found False（不猜）
    assert se.skill_search_summary("no-such-skill")["found"] is False


# ═══ S2：跨能力回退——探索留存，交付否决 ═════════════════════════════════════


def test_s2_cross_capability_regression_retained_in_pool_but_vetoed_at_release_gate(evo_root):
    scenario = se.build_regression_scenario()
    pool = scenario["pool"]
    seed_scores = scenario["seed_scores"]
    regressor_id = scenario["regressor"].edit_id

    # 1) 探索允许留存：回退候选已入池（不可比 → admit），记录本体保留
    record = pool.get_candidate(regressor_id)
    assert record is not None, "回退候选应被池留存（探索语义）"
    assert float(record["score_vector"]["case-1"]) > float(seed_scores["case-1"])  # 修好一题
    assert float(record["score_vector"]["case-2"]) < float(seed_scores["case-2"])  # 破坏另一题

    # 2) 冠军 tie 链不选回退候选（均分/最差分落后于无回退候选）
    champion = sel.select_search_champion(pool.candidates, case_ids=pool.case_ids)
    assert champion["ok"] is True
    assert champion["search_champion"] != regressor_id

    # 3) 正式交付按发布门否决（组合断言：跨能力非回退 + V + promotion_gate）
    veto = se.release_gate(
        candidate_id=regressor_id,
        per_case_scores={k: float(v) for k, v in record["score_vector"].items()},
        seed_scores=seed_scores,
    )
    assert veto["ok"] is False
    assert veto["rejected_reason"] == "cross_capability_regression"
    assert veto["checks"]["cross_capability_non_regression"]["regressions"]["case-2"]["seed"] == pytest.approx(1.0)
    # 搜索报告不能触发 tier 晋级（certification.promotion_gate 组合断言）
    assert veto["checks"]["tier_promotion_gate"]["ok"] is False
    assert veto["checks"]["tier_promotion_gate"]["code"] == "search_report_not_promotion_evidence"

    # 4) 无回退的候选通过发布门（成功保护不误伤）
    good_record = pool.get_candidate(scenario["good_fix"].edit_id)
    assert good_record is not None
    passed = se.release_gate(
        candidate_id=scenario["good_fix"].edit_id,
        per_case_scores={k: float(v) for k, v in good_record["score_vector"].items()},
        seed_scores=seed_scores,
        v_results={"case-v": 1.0},
    )
    assert passed["ok"] is True
    # 无 V 结果 → fail-closed 不冒称已提升
    unverified = se.release_gate(
        candidate_id=scenario["good_fix"].edit_id,
        per_case_scores={k: float(v) for k, v in good_record["score_vector"].items()},
        seed_scores=seed_scores,
        v_results={},
    )
    assert unverified["ok"] is False and unverified["rejected_reason"] == "v_verification_failed"
    # 5) 原生技能不受影响：无搜索报告时 promotion_gate 放行（零变化）
    assert cert.promotion_gate({"artifacts": {"eval_source": "online_eval"}}, "any-native-skill")["ok"] is True


# ═══ S3：诚实语义与统计对照 ══════════════════════════════════════════════════


def test_s3_no_budget_or_real_samples_reports_not_executed_not_promoted(evo_root):
    # 无预算 → 未执行（不是 partial、更不是 implemented/promoted）
    report = se.build_dual_line_report(
        basic_pilot={"status": "partial", "basis": ["DE-24/25 证据"]},
        search_evidence={"implementation_landed": True, "budget_present": False, "real_samples": 0},
        comparison={"benefit_proven": True},
        matrix={"ok": True},
    )
    search = report["search_enhancement"]
    assert search["status"] == "not_executed"
    assert search["decision"] == "keep_disabled"
    assert report["claims"]["no_budget_reports_not_executed"] is True
    assert report["claims"]["no_real_samples_reports_not_executed"] is True
    assert report["de_status_impact"]["auto_done"] is False
    assert set(report["de_status_impact"]["tasks_affected"]) == set(se.SEARCH_LINE_TASKS)

    # 有预算有样本但收益未证 → 保持关闭
    no_benefit = se.build_dual_line_report(
        search_evidence={
            "implementation_landed": True,
            "budget_present": True,
            "real_samples": 30,
            "review_ground_truth": True,
            "isolated_execution_environment": True,
        },
        comparison={"benefit_proven": False},
    )
    assert no_benefit["search_enhancement"]["status"] == "partial"
    assert no_benefit["search_enhancement"]["decision"] == "keep_disabled"
    assert no_benefit["claims"]["benefit_unproven_keeps_disabled"] is True

    # 有预算有样本有收益但缺隔离环境 → blocked（外部条件单列，不冒称完成）
    blocked = se.build_dual_line_report(
        search_evidence={
            "implementation_landed": True,
            "budget_present": True,
            "real_samples": 30,
            "review_ground_truth": True,
            "isolated_execution_environment": False,
        },
        comparison={"benefit_proven": True},
    )
    assert blocked["search_enhancement"]["status"] == "blocked"
    assert "isolated_execution_environment" in blocked["search_enhancement"]["blockers"]

    # 全门满足 → implemented / eligible_for_pilot（仍须 DE-35 认证线单独验收）
    eligible = se.build_dual_line_report(
        search_evidence={
            "implementation_landed": True,
            "budget_present": True,
            "real_samples": 30,
            "review_ground_truth": True,
            "isolated_execution_environment": True,
        },
        comparison={"benefit_proven": True},
    )
    assert eligible["search_enhancement"]["status"] == "implemented"
    assert eligible["search_enhancement"]["decision"] == "eligible_for_pilot"
    assert eligible["claims"]["search_pass_does_not_imply_basic"] is True
    # 两线分开：basic 状态独立于 search（基础通过不使搜索线 done）
    assert eligible["basic_pilot"]["status"] == "not_executed"  # 未传入基础证据时不重判


def test_s3_suite_statistics_are_reconcilable_before_and_after():
    counts = se.parse_pytest_counts("= 16 failed, 1704 passed, 17 skipped in 12.34s =")
    assert counts == {"passed": 1704, "failed": 16, "errors": 0, "skipped": 17}
    counts_errors = se.parse_pytest_counts("= 3 errors, 2 failed, 100 passed in 1.0s =")
    assert counts_errors["errors"] == 3 and counts_errors["failed"] == 2

    ok = se.compare_suite_statistics(
        {"passed": 1688, "failed": 16, "errors": 0, "skipped": 17},
        {"passed": 1725, "failed": 16, "errors": 0, "skipped": 17},
    )
    assert ok["ok"] is True
    assert ok["deltas"]["passed"] == 37 and ok["deltas"]["failed"] == 0
    assert ok["gates"] == {"failed_not_increased": True, "errors_not_increased": True, "passed_not_decreased": True}

    bad = se.compare_suite_statistics(
        {"passed": 1688, "failed": 16, "errors": 0, "skipped": 17},
        {"passed": 1700, "failed": 17, "errors": 1, "skipped": 17},
    )
    assert bad["ok"] is False
    assert bad["gates"]["failed_not_increased"] is False
    assert bad["gates"]["errors_not_increased"] is False


# ═══ S4：公平对照与投影保护 ══════════════════════════════════════════════════


def test_s4_fair_comparison_same_cases_model_and_cost(tmp_path, evo_root):
    world = se.build_comparison_world(
        cases=se.DEMO_CASES,
        verify_case_ids=["case-v"],
        seed_text=se.DEMO_SEED_TEXT,
        edits=se.DEMO_EDITS,
        budget_tokens=600,
        usage_per_call=5,
    )
    report = se.run_fair_comparison(world)
    conditions = report["conditions"]
    assert conditions["same_case_ids"] == ["case-1", "case-2", "case-v"]
    assert conditions["same_model"] == "synthetic-de36"
    assert conditions["same_usage_per_call"] == 5
    assert conditions["budget_cap_tokens"] == 600
    assert conditions["safety_gates"] == "always_on_not_a_variable"
    assert conditions["safety_gate_group_opened"] is False  # 安全门不做放开组

    per_arm = report["outcome"]["per_arm"]
    assert set(per_arm) == {
        se.STRATEGY_NATIVE_SINGLE_CHAMPION,
        se.STRATEGY_NATIVE_LOCAL_VARIANTS,
        se.STRATEGY_SEARCH_POOL_BOUNDED,
    }
    for arm in report["arms"]:
        assert arm["cost"]["actual_tokens"] <= 600  # 不超支
        assert arm["safety_gates"] == "always_on"
    # 同一案例对照：搜索互补池臂修复两题，原生臂至多修复一题
    assert per_arm[se.STRATEGY_SEARCH_POOL_BOUNDED]["task_success_count"] == 2
    assert per_arm[se.STRATEGY_NATIVE_LOCAL_VARIANTS]["task_success_count"] == 1
    assert per_arm[se.STRATEGY_NATIVE_SINGLE_CHAMPION]["task_success_count"] == 0
    assert per_arm[se.STRATEGY_SEARCH_POOL_BOUNDED]["release_gate_ok"] is True
    assert report["benefit_proven"] is True
    # 每臂逐 Case 向量可对照（同 Case 键）
    for arm in report["arms"]:
        assert set(arm["per_case_scores"]) == {"case-1", "case-2"}
    # 实际总成本并列披露（同预算上限下逐臂可核对）
    assert len(report["parity_view"]["best_average_at_min_tokens"]) == 3


def test_s4_safety_gate_never_opened_and_unsafe_edit_rejected_everywhere(tmp_path, evo_root):
    """安全门不做放开组：恒开启常量 + unsafe 编辑在原生臂与新策略臂都被拒绝。"""
    assert se.SAFETY_GATES_ALWAYS_ON is True
    unsafe = next(edit for edit in se.DEMO_EDITS if edit.unsafe)
    assert se.check_edit_safety(unsafe)["ok"] is False
    assert se.check_edit_safety(unsafe)["reason"] == "safety_violation"
    safe = next(edit for edit in se.DEMO_EDITS if not edit.unsafe)
    assert se.check_edit_safety(safe)["ok"] is True

    world = se.build_comparison_world(
        cases=se.DEMO_CASES,
        verify_case_ids=["case-v"],
        seed_text=se.DEMO_SEED_TEXT,
        edits=se.DEMO_EDITS,
        budget_tokens=600,
    )
    report = se.run_fair_comparison(world)
    for arm in report["arms"]:
        vetoed_ids = {v["candidate_id"] for v in arm.get("vetoed") or []}
        if arm["strategy"] == se.STRATEGY_NATIVE_SINGLE_CHAMPION:
            continue  # 该臂不做任何变异，自然没有 unsafe 尝试
        assert any("weaken-guardrail" in vid for vid in vetoed_ids), arm["strategy"]
        assert arm["delivered_text"] != unsafe.replace  # 未通过 unsafe 编辑产生交付物


def test_s4_projection_files_are_never_covered_by_search(tmp_path, evo_root):
    world_dir = _skill_dir(tmp_path)
    manifest_out = sc.build_search_manifest(
        skill_dir=world_dir,
        tenant="acme",
        space="ops-proj",
        protocol=se.freeze_protocol(algorithm_version=se.EVAL_VERSION),
    )
    assert manifest_out["ok"] is True
    manifest = manifest_out["manifest"]
    before = (world_dir / "SKILL.md").read_bytes()

    # 冻结源目录只读：搜索期间任何写回一律拒绝（候选只进搜索命名空间）
    guard = check_frozen_write(manifest, world_dir / "SKILL.md")
    assert guard["ok"] is False and guard["error"] == "frozen_source_readonly"
    rejected = write_candidate_artifact(manifest, world_dir, "candidate.md", "候选正文")
    assert rejected["ok"] is False
    assert (world_dir / "SKILL.md").read_bytes() == before

    # 投影文件不能被选作搜索目标（DE-06/29 拒绝面）
    projection_dir = tmp_path / "proj-skill"
    projection_dir.mkdir(parents=True, exist_ok=True)
    (projection_dir / "SKILL.md").write_text(
        "---\nname: proj-skill\nproducer-kind: projection\nmanaged-by: experience.skill_sync\n---\n\n正文\n",
        encoding="utf-8",
    )
    projection_manifest = sc.build_search_manifest(
        skill_dir=projection_dir,
        tenant="acme",
        space="ops-proj",
        protocol=se.freeze_protocol(algorithm_version=se.EVAL_VERSION),
    )
    assert projection_manifest["ok"] is False
    assert projection_manifest["error"] == "projection_not_searchable"
    # 公平对照框架不写任何技能目录：演示对照后种子文件字节不变
    world = se.build_comparison_world(
        cases=se.DEMO_CASES,
        verify_case_ids=["case-v"],
        seed_text=se.DEMO_SEED_TEXT,
        edits=se.DEMO_EDITS,
        budget_tokens=600,
    )
    se.run_fair_comparison(world)
    assert (world_dir / "SKILL.md").read_bytes() == before


# ═══ 兼容矩阵（step4）与原生路径关闭组合实测 ══════════════════════════════════


def test_compat_matrix_rows_map_to_real_regressions():
    report = se.verify_compat_matrix(REPO)
    scenarios = [row["scenario"] for row in report["rows"]]
    assert len(scenarios) == 14
    assert "未启动新优化器" in scenarios
    for row in report["rows"]:
        assert row["ok"] is True, f"矩阵行缺回归锚点: {row['scenario']} {row['anchors']}"
    assert report["ok"] is True
    assert report["checked_files"] >= 12


def test_native_paths_usable_with_search_components_closed(tmp_path, evo_root):
    """新组件关闭组合实测：干净台账根上原抽取/tier/冠军门/优化器默认关闭全部可用。"""
    check = se.native_paths_with_search_disabled(evo_root)
    assert check["extraction_save"] is True
    assert check["extraction_claim"] is True
    assert check["extraction_complete"] is True
    assert check["tier_set"] is True
    assert check["tier_get"] == "experimental"
    assert check["promotion_gate_native_passthrough"] is True
    assert check["new_optimizer_default_off"] is True
    assert check["search_namespaces_present"] == 0
    assert check["ok"] is True


# ═══ 双线报告结构与 CLI ══════════════════════════════════════════════════════


def test_dual_line_report_structure_and_cli(tmp_path, capsys):
    report = se.build_dual_line_report(
        basic_pilot={"status": "partial", "basis": ["DE-24/25"]},
        search_evidence={"implementation_landed": True, "budget_present": False, "real_samples": 0},
        comparison={"benefit_proven": True},
        matrix={"ok": True},
    )
    assert report["kind"] == "skill-search-dual-line-report"
    assert set(report["basic_pilot"]) >= {"line", "status", "detail", "note"}
    assert set(report["search_enhancement"]) >= {"line", "status", "gate", "blockers", "decision"}
    # 状态口径四态分开且互斥
    assert report["search_enhancement"]["status"] in {"implemented", "partial", "not_executed", "blocked"}
    assert report["claims"]["lines_reported_separately"] is True

    # CLI：--matrix-check 退出码 0（全部行有回归）
    assert se.main(["--matrix-check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True

    # CLI：--demo 产出对照+矩阵+双线报告完整结构
    out_file = tmp_path / "demo.json"
    assert se.main(["--demo", "--out", str(out_file)]) == 0
    demo = json.loads(out_file.read_text(encoding="utf-8"))
    assert demo["comparison"]["kind"] == "skill-search-fair-comparison"
    assert demo["compat_matrix"]["ok"] is True
    assert demo["dual_line_report"]["claims"]["no_budget_reports_not_executed"] is True


def test_next_action_covers_all_terminal_and_waiting_statuses():
    cases = [
        ({"status": "running", "phase": "baseline_evaluating"}, "continue_run"),
        ({"status": "paused", "phase": "searching"}, "resume"),
        ({"status": "blocked_budget", "phase": "mutation"}, "add_budget_then_resume"),
        ({"status": "blocked_source_changed", "phase": "searching"}, "reopen_new_search"),
        ({"status": "blocked_protocol_drift", "phase": "searching"}, "refreeze_protocol_then_reopen"),
        ({"status": "cancelled", "phase": "searching"}, "reopen_new_search"),
        ({"status": "failed", "phase": "baseline_evaluating"}, "inspect_then_reopen"),
        ({"status": "cleanup_failed", "phase": "awaiting_review"}, "fix_release_blockers"),
        ({"status": "running", "phase": "completed"}, "request_certification"),
        ({"status": "unknown-status", "phase": ""}, "inspect_state"),
    ]
    for state, expected in cases:
        action = se.infer_next_action(state, can_publish=True)
        assert action["code"] == expected, f"{state} -> {action}"
    blocked = se.infer_next_action(
        {"status": "running", "phase": "completed", "cleanup": {"release_blockers": ["v_verification_failed"]}},
        can_publish=False,
    )
    assert blocked["code"] == "fix_release_blockers"
