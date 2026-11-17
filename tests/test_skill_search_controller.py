"""DE-34 持久多代搜索控制器回归：每阶段故障重启、并发防护、预算 Later、漂移与隔离。

对应 tasks.json DE-34 四条 acceptance_scenarios（全部离线：合成执行器/确定性归因
与 patch 提供者/固定时钟/DE-15 真实预算端口 + sqlite）：

1. S1 每阶段故障重启：不重抽父代、不生成另一 patch、不重复扣费、已完成 Case 不丢；
2. S2 两 worker/旧 generation 不能覆盖新状态；无预算不耗失败次数（Later），补预算后从原阶段续跑；
3. S3 协议/源漂移拒绝续用旧分数；取消后无新副作用；恢复失败为 cleanup_failed 而非 completed；
4. S4 控制器关闭/暂停不影响原在线抽取（extraction_window 独立）；多空间不串账。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.skill_search import contracts as sc
from agents.skill_search import controller as ct
from agents.skill_search import evaluator as ev
from agents.skill_search import feedback as fb
from agents.skill_search import modules as sm
from agents.skill_search import protocol as sp
from agents.skill_search.contracts import (
    build_search_manifest,
    freeze_protocol,
    search_namespace,
)
from agents.skill_search.controller import (
    ControllerConfig,
    De15BudgetPort,
    FaultInjected,
    SearchController,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


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


# ── 合成夹具 ────────────────────────────────────────────────────────────────


def _question(case_id: str) -> str:
    return f"case {case_id} 执行部署发布任务 topic-{case_id}-alpha"


def _case(case_id: str, *, role: str = "S") -> dict:
    return {
        "case_id": case_id,
        "origin_group": "score",
        "origin_split": "mutate_dev",
        "family": "deploy",
        "question": _question(case_id),
        "expected": f"case {case_id} 期望：灰度比例 10 percent 且生成发布摘要",
        "protocol_role": role,
    }


def _trace(case_id: str) -> list[dict]:
    return [
        {"event_id": "e-1", "kind": "user_message", "text": _question(case_id)},
        {
            "event_id": "e-2",
            "kind": "tool_call",
            "text": f"tool deploy_rollout set canary ratio 5 percent for service A ({case_id})",
        },
        {
            "event_id": "e-3",
            "kind": "verdict",
            "text": f"verifier failed canary ratio mismatch expected 10 percent got 5 ({case_id})",
        },
    ]


class ScriptedExecutor:
    """合成执行器：按 (candidate_id, case_id) 脚本打分；记录调用与回执（崩溃对账用）。"""

    adapter_id = "synthetic-de34"

    def __init__(
        self,
        scores: dict[str, dict[str, float]],
        *,
        usage: int = 5,
        fault_after: tuple[str, str] | None = None,
        infra_fail: set[tuple[str, str]] | None = None,
    ):
        self.scores = scores
        self.usage = usage
        self.fault_after = fault_after
        self.infra_fail = infra_fail or set()
        self.calls: list[tuple[str, str]] = []
        self.receipts: dict[tuple[str, str], dict] = {}
        self.protocols: list[dict] = []

    async def execute(self, *, case, candidate, frozen_protocol):
        candidate_id = str(candidate["candidate_id"])
        case_id = str(case["case_id"])
        self.calls.append((candidate_id, case_id))
        self.protocols.append(dict(frozen_protocol or {}))
        receipt = self._outcome(candidate_id, case_id, candidate, frozen_protocol).to_dict()
        self.receipts[(candidate_id, case_id)] = receipt
        if self.fault_after == (candidate_id, case_id):
            raise FaultInjected(f"process died after external call {candidate_id}/{case_id}")
        if (candidate_id, case_id) in self.infra_fail:
            raise RuntimeError("provider 429: infra error (not a skill failure)")
        return ev.ExecutionOutcome(**{k: v for k, v in receipt.items()})

    def _outcome(self, candidate_id, case_id, candidate, frozen_protocol):
        score = float(self.scores.get(candidate_id, {}).get(case_id, 0.0))
        return ev.ExecutionOutcome(
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
        )

    def get_receipt(self, candidate_id: str, case_id: str):
        return self.receipts.get((candidate_id, case_id))


class Attributor:
    """确定性归因器：对父代全部 failed Case 产出可核验的 SKILL_ISSUE 提案。"""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, ctx):
        self.calls.append(str((ctx.get("parent") or {}).get("candidate_id") or ""))
        proposals = []
        for evaluation in ctx.get("evaluations") or []:
            if str(evaluation.get("terminal_status") or "") != fb.TERMINAL_SCORED_FAIL:
                continue
            case_id = str(evaluation.get("case_id") or "")
            proposals.append(
                {
                    "case_id": case_id,
                    "category": fb.ATTR_SKILL_ISSUE,
                    "sub_kind": fb.SKILL_SUB_BODY_DEFECT,
                    "module_id": "cap.workflow",
                    "citations": [
                        {"event_ref": f"trace://{case_id}#e-2"},
                        {"event_ref": f"trace://{case_id}#e-3"},
                    ],
                    "observed": "canary ratio set to 5 percent",
                    "expected": "canary ratio should be 10 percent",
                    "failure_atoms": ["canary ratio wrong"],
                    "confidence": 0.9,
                }
            )
        return proposals


class PatchProvider:
    """确定性 patch 提供者：把灰度比例 5% 改为 10%（绑定父版本 hash 的 replace 操作）。"""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, brief):
        self.calls.append(brief)
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
    """隔离端口：候选挂载/种子恢复回执。"""

    def __init__(self, *, fail_restore: bool = False):
        self.mounts: list[str] = []
        self.restores: list[dict] = []
        self.fail_restore = fail_restore

    def mount(self, candidate_id: str, text: str) -> dict:
        self.mounts.append(f"{candidate_id}:{len(text)}")
        return {"ok": True, "mounted": True, "candidate_id": candidate_id, "mount_id": f"m-{len(self.mounts)}"}

    def restore_seed(self) -> dict:
        receipt = (
            {"ok": False, "reason": "restore_hook_failed"}
            if self.fail_restore
            else {"ok": True, "restored": True, "mode": "seed_mount_restored"}
        )
        self.restores.append(receipt)
        return receipt


def _make_budget(tmp_path, *, space: str = "ops", org_limit: int = 100_000, space_limit: int = 100_000):
    from evoharness.governance import budget as de15
    from evoharness.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db = tmp_path / f"budget-{space}.db"
    engine = create_engine("sqlite:///" + str(db), future=True, connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        de15.set_limit(session, scope_kind="org", scope_id="org-1", limit_tokens=org_limit)
        de15.set_limit(session, scope_kind="space", scope_id=space, limit_tokens=space_limit)
        session.commit()
    return De15BudgetPort(factory, org_id="org-1", space_id=space), engine


def _world(tmp_path, *, space: str = "ops", with_v: bool = True):
    skill_dir = tmp_path / "deploy-rollout"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
    cases = [_case("case-1"), _case("case-2")]
    if with_v:
        cases.append(_case("case-v", role="V"))
    assigned = sp.assign_protocol_roles(cases)
    assert assigned["ok"] is True, assigned
    frozen = sp.freeze_case_protocol(assigned["cases"])
    assert frozen["ok"] is True
    freeze = frozen["freeze"]
    spec_out = sm.build_module_spec(SKILL_TEXT, MODULE_DEFS, skill_name="deploy-rollout")
    assert spec_out["ok"] is True
    manifest_out = build_search_manifest(
        skill_dir=skill_dir,
        tenant="acme",
        space=space,
        protocol=freeze_protocol(
            splits=sp.protocol_freeze_entry(freeze),
            algorithm_version="skill-search-controller-v1",
            models={"evaluator": "synthetic", "reflection": "synthetic"},
        ),
        module_spec=spec_out["spec"],
    )
    assert manifest_out["ok"] is True, manifest_out
    manifest = manifest_out["manifest"]
    trace_store = fb.TraceStore({f"trace://{cid}": _trace(cid) for cid in ("case-1", "case-2", "case-v")})
    return {
        "skill_dir": skill_dir,
        "cases": assigned["cases"],
        "freeze": freeze,
        "spec": spec_out["spec"],
        "manifest": manifest,
        "trace_store": trace_store,
        "base_dir": tmp_path / "evo",
    }


def _make_controller(
    world,
    executor,
    budget,
    *,
    isolation=None,
    config=None,
    fault_hook=None,
    clock=None,
    store=None,
    attributor=None,
    patch_provider=None,
):
    return SearchController(
        manifest=world["manifest"],
        cases=world["cases"],
        freeze=world["freeze"],
        module_spec=world["spec"],
        executor=executor,
        budget=budget,
        config=config
        or ControllerConfig(
            prerequisites=dict(READY),
            trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
            max_iterations=2,
            max_case_workers=1,
        ),
        store=store,
        base_dir=world["base_dir"],
        attributor=attributor or Attributor(),
        patch_provider=patch_provider or PatchProvider(),
        isolation=isolation if isolation is not None else FakeIsolation(),
        trace_store=world["trace_store"],
        fault_hook=fault_hook,
        clock=clock or (lambda: 1000.0),
    )


# 基线分数：case-1 失败（归因→变异的驱动力），case-2 通过；子代修复 case-1。
SEED_SCORES = {"case-1": 0.2, "case-2": 1.0}
CHILD_SCORES = {"case-1": 1.0, "case-2": 1.0, "case-v": 1.0}


# ── 启用条件：前置就绪门（缺则拒绝启动并给原因，零副作用） ────────────────────


def test_not_ready_refuses_to_start_without_side_effects(tmp_path):
    world = _world(tmp_path)
    executor = ScriptedExecutor({"seed": SEED_SCORES})
    budget, engine = _make_budget(tmp_path)
    config = ControllerConfig(
        prerequisites={"de30_evaluator": True, "de31_feedback": True, "de32_mutator": True},
        trace_refs={},
    )
    controller = _make_controller(world, executor, budget, config=config)
    result = controller.start()
    assert result["ok"] is False
    assert result["missing"] == ["local_baseline"]
    store = ct.st.SearchStore(search_namespace(world["manifest"], world["base_dir"]))
    assert store.exists() is False  # 零副作用：状态未初始化
    assert budget.usage_summary()["used_total"] == 0  # 零预留
    engine.dispose()


# ── S1：每阶段故障重启——不重抽父代、不生成另一 patch、不重复扣费、Case 不丢 ──


STAGE_SEQUENCE = [
    "baseline_evaluating",
    "searching",  # 轮次边界（round.phase 仍为 parent_selection）
    "parent_selection",
    "feedback",
    "mutation",
    "validation",
    "deployment",
    "evaluation",
    "judging",
    "finalizing",
    "cleanup",
]


def test_s1_every_stage_crash_resume_never_redraws_parent_or_patch(tmp_path):
    world = _world(tmp_path)
    executor = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
    budget, engine = _make_budget(tmp_path)
    attributor = Attributor()
    patch_provider = PatchProvider()
    isolation = FakeIsolation()
    clock = {"t": 1000.0}
    crashed: list[str] = []

    def fault_hook(stage: str) -> None:
        if stage in STAGE_SEQUENCE and stage not in crashed:
            crashed.append(stage)
            raise FaultInjected(f"fault@{stage}")

    config = ControllerConfig(
        prerequisites=dict(READY),
        trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
        max_iterations=2,
        max_case_workers=1,
        lease_seconds=300.0,
    )
    store = ct.st.SearchStore(search_namespace(world["manifest"], world["base_dir"]))
    first = _make_controller(
        world, executor, budget, isolation=isolation, config=config,
        fault_hook=fault_hook, clock=lambda: clock["t"], store=store,
        attributor=attributor, patch_provider=patch_provider,
    )
    with pytest.raises(FaultInjected):
        first.start(worker="worker-a")

    # 之后每次 run 在下一个未崩溃阶段再崩一次，直到所有阶段都经历过崩溃-恢复并完成
    final = None
    for _ in range(40):
        assert len(crashed) <= len(STAGE_SEQUENCE)
        clock["t"] += 1000.0  # 租约过期，接管恢复
        resume = _make_controller(
            world, executor, budget, isolation=isolation, config=config,
            fault_hook=fault_hook, clock=lambda: clock["t"], store=store,
            attributor=attributor, patch_provider=patch_provider,
        )
        try:
            resume.run(worker="worker-b")
        except FaultInjected:
            continue
        final = resume.status()
        if final["phase"] == "completed":
            break
    assert final is not None and len(crashed) == len(STAGE_SEQUENCE), (crashed, final)

    final = resume.status()
    assert final["phase"] == "completed", final
    assert final["status"] == "completed"
    assert final["champion"] == "cand-r1"
    assert final["can_publish"] is True

    # 不重抽父代：round-1 的父代只抽一次（draw_index 推进 2 = 两轮各一次）
    state = store.state
    assert state["sampler"]["state"]["draw_index"] == 2
    round1 = next(r for r in state["completed_rounds"] if r["index"] == 1)
    round2 = next(r for r in state["completed_rounds"] if r["index"] == 2)
    assert round1["parent_id"] == "seed"
    assert round2["parent_id"] == "cand-r1"
    # 不生成另一 patch：patch 提供者只调用一次；归因每轮恰好一次（第二轮无失败题，无提案）
    assert attributor.calls == ["seed", "cand-r1"]
    assert len(patch_provider.calls) == 1
    # 已完成 Case 不丢：每个 (候选, Case) 只真实执行一次
    assert sorted(executor.calls) == [
        ("cand-r1", "case-1"),
        ("cand-r1", "case-2"),
        ("cand-r1", "case-v"),
        ("seed", "case-1"),
        ("seed", "case-2"),
    ]
    # 不重复扣费：全部结算一次，无失败结算、无遗留开放预留（崩溃恢复未产生重复账目）
    # 期望 = 基线 2×5 + 反思 2轮×8 + 变异 8 + judge 6 + 执行 2×5 + 最终验证 5 = 55
    summary = budget.usage_summary()
    assert summary["failed_settled"] == 0
    assert summary["reserved_open"] == 0
    assert summary["used_total"] == 2 * 5 + 2 * 8 + 8 + 6 + 2 * 5 + 5
    assert store.state["open_reservations"] == {}
    costs = final["costs"]
    for category in ("baseline", "reflection", "mutation", "execution", "judge", "final_verification"):
        assert costs[category]["calls"] >= 1, category
        assert costs[category]["used_tokens"] > 0, category
    assert costs.get("retry", {}).get("calls", 0) == 0  # 无 infra 重试
    # 池与 Attempt：seed + 子代入池；隔离回执落盘
    assert final["pool_size"] == 2
    assert final["attempts"] == 2
    ns = search_namespace(world["manifest"], world["base_dir"])
    restore_receipts = list((ns / "receipts").glob("restore-seed-*.json"))
    assert len(restore_receipts) == 1
    assert json.loads(restore_receipts[0].read_text(encoding="utf-8"))["ok"] is True
    assert isolation.restores and isolation.restores[-1]["ok"] is True
    engine.dispose()


def test_s1_mid_call_crash_reconciles_receipt_without_reexecution_or_double_charge(tmp_path):
    world = _world(tmp_path)
    # 崩溃点：基线 case-1 外部调用已完成（回执已留）→ 进程死在结果落盘前
    executor = ScriptedExecutor(
        {"seed": SEED_SCORES, "cand-r1": CHILD_SCORES}, fault_after=("seed", "case-1")
    )
    budget, engine = _make_budget(tmp_path)
    config = ControllerConfig(
        prerequisites=dict(READY),
        trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
        max_iterations=2,
        max_case_workers=1,
    )
    store = ct.st.SearchStore(search_namespace(world["manifest"], world["base_dir"]))
    first = _make_controller(world, executor, budget, config=config, store=store, clock=lambda: 1000.0)
    with pytest.raises(FaultInjected):
        first.start(worker="worker-a")
    # 崩溃现场：两个 Case 都已预留（批内全部先预留再执行）；case-1 在途无终态
    inflight = store.state["baseline"]["cases"]["case-1"]
    assert inflight["status"] == ct.CASE_IN_FLIGHT
    assert len(store.state["open_reservations"]) == 2  # case-1 已执行（有回执）、case-2 未执行

    resume = _make_controller(world, executor, budget, config=config, store=store, clock=lambda: 5000.0)
    result = resume.run(worker="worker-b")
    assert result["phase"] == "completed", result
    # 已开始的外部调用按回执对账：case-1 不重发（calls==1），case-2 正常执行一次
    assert executor.calls.count(("seed", "case-1")) == 1
    assert executor.calls.count(("seed", "case-2")) == 1
    # 不重复扣费：case-1 的预留只按回执结算一次；总额=完整搜索的全部成本（无重复账目）
    summary = budget.usage_summary()
    assert summary["failed_settled"] == 0
    assert summary["reserved_open"] == 0
    assert summary["used_total"] == 2 * 5 + 2 * 8 + 8 + 6 + 2 * 5 + 5
    assert store.state["open_reservations"] == {}
    bucket = store.state["baseline"]["cases"]
    assert bucket["case-1"]["status"] == ct.CASE_DONE
    assert bucket["case-2"]["status"] == ct.CASE_DONE
    engine.dispose()


# ── S2：两 worker 租约互斥 + 无预算 Later 不耗失败次数，补预算后原阶段续跑 ────


def test_s2_second_worker_blocked_by_lease_and_budget_resume_from_same_stage(tmp_path):
    world = _world(tmp_path)
    executor = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
    # 限额 24：基线(10)+反思(8) 后，变异预留(8) 超限 → blocked_budget
    budget, engine = _make_budget(tmp_path, org_limit=24, space_limit=24)
    config = ControllerConfig(
        prerequisites=dict(READY),
        trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
        max_iterations=2,
        max_case_workers=1,
        lease_seconds=300.0,
    )
    clock = {"t": 1000.0}
    store = ct.st.SearchStore(search_namespace(world["manifest"], world["base_dir"]))
    first = _make_controller(world, executor, budget, config=config, store=store, clock=lambda: clock["t"])
    result = first.start(worker="worker-a")
    assert result["status"] == "blocked_budget", result
    assert result["phase"] == "searching"
    assert result["round_phase"] == "mutation"
    # Later 语义：不计失败次数（无 patch 生成、无失败 Attempt、无重试计数）
    assert store.state["round"]["failure_attempts"] == 0
    assert store.state["round"]["patch"] is None
    assert len(store.state["open_reservations"]) == 0  # 无遗留预留
    assert executor.calls == [("seed", "case-1"), ("seed", "case-2")]  # 基线后无新执行

    # 有效租约期内第二 worker 不能接管推进
    clock["t"] += 10.0
    second = _make_controller(world, executor, budget, config=config, store=store, clock=lambda: clock["t"])
    busy = second.run(worker="worker-b")
    assert busy["ok"] is False and busy["error"] == "lease_busy"

    # 补预算后从原阶段（mutation）续跑；feedback 不重算、patch 只生成一次
    budget.set_limits(org_tokens=100_000, space_tokens=100_000)
    clock["t"] += 1000.0  # 租约过期，新 worker 接管
    resume = _make_controller(world, executor, budget, config=config, store=store, clock=lambda: clock["t"])
    final = resume.run(worker="worker-c")
    assert final["phase"] == "completed", final
    assert final["champion"] == "cand-r1"
    assert final["can_publish"] is True
    # 续跑语义断言：归因一次、patch 一次、无失败结算、failure_attempts 仍为 0
    assert executor.calls.count(("seed", "case-1")) == 1
    assert executor.calls.count(("cand-r1", "case-1")) == 1
    assert budget.usage_summary()["failed_settled"] == 0
    assert store.state["round"]["failure_attempts"] == 0
    engine.dispose()


# ── S3：协议/源漂移拒绝续用旧分数；取消后无新副作用；恢复失败 cleanup_failed ──


def test_s3_protocol_and_source_drift_refuse_reuse_of_old_scores(tmp_path):
    world = _world(tmp_path)
    executor = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
    budget, engine = _make_budget(tmp_path)
    config = ControllerConfig(
        prerequisites=dict(READY),
        trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
        max_iterations=1,
        max_case_workers=1,
    )
    clock = {"t": 1000.0}
    store = ct.st.SearchStore(search_namespace(world["manifest"], world["base_dir"]))
    first = _make_controller(world, executor, budget, config=config, store=store, clock=lambda: clock["t"])
    # 跑完基线后停在 round-1 评估阶段（注入故障）
    def fault_hook(stage):
        if stage == "mutation":
            raise FaultInjected("fault@mutation")

    first_run = _make_controller(
        world, executor, budget, config=config, store=store, clock=lambda: clock["t"], fault_hook=fault_hook
    )
    with pytest.raises(FaultInjected):
        first_run.start(worker="worker-a")
    baseline_pool = len(store.load_pool().candidates)
    baseline_digest = store.load_pool().content_digest()
    assert baseline_pool == 1  # seed 已入池，旧分数已在案

    # 协议漂移：同命名空间换用新协议（评测模型环境变化 → 新协议指纹）→ 拒绝续用旧分数
    manifest_with_new_protocol = build_search_manifest(
        skill_dir=world["skill_dir"],
        tenant=world["manifest"].tenant,
        space=world["manifest"].space,
        search_id=world["manifest"].search_id,
        protocol=freeze_protocol(
            splits=sp.protocol_freeze_entry(world["freeze"]),
            algorithm_version="skill-search-controller-v1",
            models={"evaluator": "synthetic-v2"},  # 评测模型环境变化 → 新协议
        ),
        module_spec=world["spec"],
    )
    assert manifest_with_new_protocol["ok"] is True
    drifted = SearchController(
        manifest=manifest_with_new_protocol["manifest"],
        cases=world["cases"],
        freeze=world["freeze"],
        module_spec=world["spec"],
        executor=executor,
        budget=budget,
        config=config,
        store=store,
        attributor=Attributor(),
        patch_provider=PatchProvider(),
        isolation=FakeIsolation(),
        trace_store=world["trace_store"],
        clock=lambda: clock["t"] + 1000.0,
    )
    result = drifted.run(worker="worker-d")
    assert result["ok"] is False
    assert result["status"] == "blocked_protocol_drift"
    # 旧分数未被续用：池内容不变、无新执行、无新扣费
    assert store.load_pool().content_digest() == baseline_digest
    assert executor.calls == [("seed", "case-1"), ("seed", "case-2")]

    # 源漂移：SKILL.md 字节变化 → blocked_source_changed（同协议、同 manifest 也会被拒）
    (world["skill_dir"] / "SKILL.md").write_text(SKILL_TEXT.replace("灰度 5%", "灰度 6%"), encoding="utf-8")
    source_drift = _make_controller(world, executor, budget, config=config, store=store, clock=lambda: clock["t"] + 2000.0)
    result = source_drift.run(worker="worker-e")
    assert result["ok"] is False
    assert result["status"] == "blocked_source_changed"
    assert len(source_drift.pool.candidates) == baseline_pool
    engine.dispose()


def test_s3_cancel_stops_new_side_effects_and_cleanup_failure_blocks_publish(tmp_path):
    world = _world(tmp_path)
    executor = ScriptedExecutor(
        {"seed": SEED_SCORES, "cand-r1": CHILD_SCORES}, fault_after=("cand-r1", "case-1")
    )
    budget, engine = _make_budget(tmp_path)
    config = ControllerConfig(
        prerequisites=dict(READY),
        trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
        max_iterations=2,
        max_case_workers=1,
    )
    clock = {"t": 1000.0}
    store = ct.st.SearchStore(search_namespace(world["manifest"], world["base_dir"]))
    first = _make_controller(world, executor, budget, config=config, store=store, clock=lambda: clock["t"])
    with pytest.raises(FaultInjected):
        first.start(worker="worker-a")  # 崩在 round-1 评估：case-1 已执行（有回执）

    cancel_view = _make_controller(world, executor, budget, config=config, store=store, clock=lambda: 5000.0)
    assert cancel_view.request_cancel("user requested stop") is True
    calls_after_cancel_request = len(executor.calls)
    result = cancel_view.run(worker="worker-b")
    assert result["status"] == "cancelled", result
    # 取消后无新副作用：round-1 候选的 case-2 不再执行；已开始调用只按回执对账
    assert len(executor.calls) == calls_after_cancel_request
    assert executor.calls.count(("cand-r1", "case-2")) == 0
    after = budget.usage_summary()
    assert after["reserved_open"] == 0  # 无遗留预留（对账结算/释放）
    assert store.state["status"] == "cancelled"
    assert store.state["cleanup"]["restored"] is True  # 取消路径也恢复挂载

    # 恢复失败：隔离种子挂载恢复失败 → cleanup_failed 而非 completed，发布阻断
    world2 = _world(tmp_path / "w2")
    executor2 = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
    budget2, engine2 = _make_budget(tmp_path / "w2")
    isolation2 = FakeIsolation(fail_restore=True)
    done = _make_controller(world2, executor2, budget2, config=config, isolation=isolation2, clock=lambda: 1000.0)
    result = done.start(worker="worker-a")
    assert result["status"] == "cleanup_failed", result
    assert result["phase"] == "awaiting_review"  # 停在清理，绝不宣布 completed
    assert result["can_publish"] is False
    assert "isolation_restore_failed" in result["release_blockers"]
    assert store_state_status(world2) == "cleanup_failed"
    engine.dispose()
    engine2.dispose()


def store_state_status(world) -> str:
    ns = search_namespace(world["manifest"], world["base_dir"])
    return json.loads((ns / "state.json").read_text(encoding="utf-8"))["status"]


# ── S4：控制器关闭/暂停不影响在线抽取；多空间不串账 ───────────────────────────


def test_s4_paused_controller_does_not_block_online_extraction(tmp_path, monkeypatch):
    world = _world(tmp_path)
    executor = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
    budget, engine = _make_budget(tmp_path)
    config = ControllerConfig(
        prerequisites=dict(READY),
        trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
        max_iterations=2,
        max_case_workers=1,
    )
    store = ct.st.SearchStore(search_namespace(world["manifest"], world["base_dir"]))
    crashed: list[str] = []

    def fault_hook(stage: str) -> None:
        if stage == "baseline_evaluating" and stage not in crashed:
            crashed.append(stage)
            raise FaultInjected("fault@baseline_evaluating")

    controller = _make_controller(
        world, executor, budget, config=config, store=store, clock=lambda: 1000.0, fault_hook=fault_hook
    )
    with pytest.raises(FaultInjected):
        controller.start(worker="worker-a")
    # 故障后请求暂停：恢复运行停在暂停标记上，不继续推进
    assert controller.request_pause("maintenance window") is True
    result = _make_controller(
        world, executor, budget, config=config, store=store, clock=lambda: 5000.0
    ).run(worker="worker-b")
    assert result["status"] == "paused"
    assert result["phase"] == "baseline_evaluating"

    # 控制器暂停期间：原在线抽取路径（extraction_window）完全独立可用
    from agents import session as session_store
    from agents import extraction_window as ew

    monkeypatch.setattr(session_store, "SESSION_DIR", tmp_path / "sessions")
    window = {
        "messages": [{"role": "user", "content": "把灰度比例改成 10 percent"}],
        "next_user_feedback": "",
    }
    saved = ew.save_pending_window("sess-extract-1", window)
    assert saved["status"] == "pending"
    claimed = ew.claim_window("sess-extract-1", saved["windowId"], feedback="ok", now=123.0)
    assert claimed is not None and claimed["status"] == "claimed"
    completed = ew.mark_completed("sess-extract-1", saved["windowId"], summary="extracted")
    assert completed["status"] == "completed"
    # 暂停的搜索状态未被抽取路径触碰
    assert controller.status()["status"] == "paused"
    # 控制器模块不 import/不拦截 extraction_window（独立调用入口，非常驻拦截器）
    import agents.skill_search.controller as ctrl_module

    assert "extraction_window" not in ctrl_module.__dict__
    source = Path(ctrl_module.__file__).read_text(encoding="utf-8")
    assert "import extraction_window" not in source
    assert "from ..extraction_window" not in source
    assert "from agents.extraction_window" not in source
    assert "from .extraction_window" not in source
    engine.dispose()


def test_s4_multi_space_searches_do_not_cross_accounts(tmp_path):
    worlds = {space: _world(tmp_path / f"w-{space}", space=space) for space in ("ops", "research")}
    executors = {}
    budgets = {}
    engines = {}
    controllers = {}
    for space, world in worlds.items():
        executors[space] = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
        budgets[space], engines[space] = _make_budget(tmp_path / f"w-{space}", space=space)
        controllers[space] = _make_controller(world, executors[space], budgets[space], clock=lambda: 1000.0)
    # 空间 A 只跑基线（注入故障停在轮次开始前），空间 B 全量完成
    def fault_hook(stage):
        if stage == "searching":
            raise FaultInjected("fault@searching-ops")

    ops_controller = SearchController(
        manifest=worlds["ops"]["manifest"],
        cases=worlds["ops"]["cases"],
        freeze=worlds["ops"]["freeze"],
        module_spec=worlds["ops"]["spec"],
        executor=executors["ops"],
        budget=budgets["ops"],
        config=ControllerConfig(
            prerequisites=dict(READY),
            trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
            max_iterations=2,
            max_case_workers=1,
        ),
        store=ct.st.SearchStore(search_namespace(worlds["ops"]["manifest"], worlds["ops"]["base_dir"])),
        attributor=Attributor(),
        patch_provider=PatchProvider(),
        isolation=FakeIsolation(),
        trace_store=worlds["ops"]["trace_store"],
        fault_hook=fault_hook,
        clock=lambda: 1000.0,
    )
    with pytest.raises(FaultInjected):
        ops_controller.start(worker="worker-ops")
    assert controllers["research"].start(worker="worker-research")["phase"] == "completed"
    # 各空间命名空间相互独立：状态/池/候选/回执互不可见
    ns_ops = search_namespace(worlds["ops"]["manifest"], worlds["ops"]["base_dir"])
    ns_res = search_namespace(worlds["research"]["manifest"], worlds["research"]["base_dir"])
    assert ns_ops != ns_res
    assert "ops" in str(ns_ops) and "research" in str(ns_res)
    # 空间 B 完成后，空间 A 的状态文件字节未被触碰（不串账）
    ops_state_before = (ns_ops / "state.json").read_text(encoding="utf-8")
    assert "cand-r1" not in ops_state_before or json.loads(ops_state_before)["phase"] == "searching"
    res_pool = json.loads((ns_res / "pool.json").read_text(encoding="utf-8"))
    assert any(c["candidate_id"] == "cand-r1" for c in res_pool["candidates"])
    # 预算按空间隔离：各空间只归集自己搜索的成本
    # research 全程 = 2×5 基线 + 2×8 反思 + 8 变异 + 6 judge + 2×5 执行 + 5 最终验证 = 55
    assert budgets["research"].usage_summary()["used_total"] == 2 * 5 + 2 * 8 + 8 + 6 + 2 * 5 + 5
    assert budgets["ops"].usage_summary()["used_total"] == 2 * executors["ops"].usage  # 仅基线
    # 执行器收到的冻结协议 payload 绑定各自的搜索命名空间（缓存/回执不可能跨空间命中）
    for space in ("ops", "research"):
        scopes = {p.get("scope") for p in executors[space].protocols}
        manifest = worlds[space]["manifest"]
        expected_scope = f"{manifest.tenant}/{manifest.space}/{manifest.lineage}/{manifest.search_id}"
        assert scopes == {expected_scope}, (space, scopes)
    for engine in engines.values():
        engine.dispose()


# ── 有界并发与确定性（Case 并发结果与串行一致） ───────────────────────────────


def test_bounded_case_concurrency_completes_all_cases(tmp_path):
    world = _world(tmp_path)
    executor = ScriptedExecutor({"seed": SEED_SCORES, "cand-r1": CHILD_SCORES})
    budget, engine = _make_budget(tmp_path)
    config = ControllerConfig(
        prerequisites=dict(READY),
        trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
        max_iterations=1,
        max_case_workers=3,
    )
    controller = _make_controller(world, executor, budget, config=config, clock=lambda: 1000.0)
    result = controller.start(worker="worker-a")
    assert result["phase"] == "completed", result
    assert result["champion"] == "cand-r1"
    # 每个 Case 恰好执行一次（并发不重跑、不丢结果）
    for pair in [("seed", "case-1"), ("seed", "case-2"), ("cand-r1", "case-1"), ("cand-r1", "case-2")]:
        assert executor.calls.count(pair) == 1
    # max_iterations=1：单轮后收敛；V 验证对冠军执行
    assert executor.calls.count(("cand-r1", "case-v")) == 1
    engine.dispose()


# ── infra 失败：有界重试后阻塞候选（Attempt 保留，不伪装技能 0 分） ────────────


def test_infra_error_retries_bounded_then_blocks_candidate(tmp_path):
    world = _world(tmp_path)
    executor = ScriptedExecutor(
        {"seed": SEED_SCORES, "cand-r1": CHILD_SCORES},
        infra_fail={("cand-r1", "case-1"), ("cand-r1", "case-1:retry")},
    )
    budget, engine = _make_budget(tmp_path)
    config = ControllerConfig(
        prerequisites=dict(READY),
        trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
        max_iterations=1,
        max_case_workers=1,
        max_infra_retries_per_case=1,
    )
    controller = _make_controller(world, executor, budget, config=config, clock=lambda: 1000.0)
    result = controller.start(worker="worker-a")
    assert result["phase"] == "completed", result  # 候选被阻塞但搜索可解释地收敛
    # infra 失败计入失败次数（区别于预算 Later），重试一次后 case-1 仍失败 → 候选阻塞
    state = controller._require_state()
    round1 = next(r for r in state["completed_rounds"] if r["index"] == 1)
    assert round1["decision"] == "blocked"
    assert round1["failure_attempts"] >= 2  # 两次 infra 失败都计入
    assert result["attempts"] == 2  # seed 入池 Attempt + 阻塞 Attempt 落账（未入池尝试也保留）
    # infra 失败的调用按真实结算归集（ok=False），无免费副作用
    assert budget.usage_summary()["failed_settled"] >= 2
    engine.dispose()
