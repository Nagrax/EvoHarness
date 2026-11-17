"""DE-24 回归：冻结评测与业务效果/成本实验框架（eval/digital_employee_eval.py）。

覆盖 tasks.json DE-24 的 5 条验收场景 + 冻结顺序（先冻结后评测）+ RED 受限工具
未调用观察。全部离线：合成任务 / 合成对照数据 / 固定端口回执 / 零模型 / 零网络 /
零付费调用。断言真实行为（分母计数、门判定、违规记录、开关状态），不是空跑。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import digital_employee_eval as dee  # noqa: E402
from eval.digital_employee_eval import (  # noqa: E402
    EvalBlocked,
    SyntheticFailClosedPort,
    benefit_decision,
    build_fault_set,
    claims_policy,
    freeze_protocol,
    freeze_task_split,
    honest_ticket_status,
    load_freeze,
    observe_restricted_tools,
    paired_comparison,
    replay_task,
    run_fault_set,
    run_frozen_eval,
    verify_regression_surfaces,
    write_freeze,
)

VERSIONS = {
    "model": "test-model",
    "prompt_version": "p1",
    "tools": ["knowledge_search", "wiki_read"],
    "knowledge_version": "k1",
    "service_port_id": "scripted-test-port.v1",
}
RESTRICTED = ["shell", "send_message"]


_DOMAINS = ["年假", "报销", "会议室", "访客", "工资", "出差", "设备", "培训", "门禁",
             "停车", "采购", "合同", "印章", "邮箱", "vpn"]


def _task(tid: str, *, text: str | None = None, thread: str | None = None,
          theme: str | None = None, ts: str = "2026-11-01T10:00:00Z") -> dict:
    if text is None:
        # 默认文本按 tid 稳定选择不同业务域，且每个 token 都带 tid，
        # 保证跨任务 3-gram 近重复度为低（泄漏门真实放行独立任务）。
        import hashlib as _hashlib

        idx = int(_hashlib.md5(tid.encode("utf-8")).hexdigest(), 16) % len(_DOMAINS)
        domain = _DOMAINS[idx]
        text = (f"{domain}请求{tid}："
                + " ".join(f"{tid}步骤{i}号" for i in range(10))
                + f"（{domain}域第{idx}章）")
    return {
        "task_id": tid,
        "text": text,
        "thread_id": thread or f"th-{tid}",
        "theme": theme or f"主题-{tid}",
        "timestamp": ts,
    }


def _tasks(n: int = 6) -> list[dict]:
    return [_task(f"t{i}") for i in range(n)]


def _freeze(tmp_path: Path, tasks: list[dict], *, restricted=None, **over) -> tuple[Path, dict]:
    freeze = freeze_protocol(
        tasks=tasks, versions=VERSIONS, restricted_tools=RESTRICTED if restricted is None else restricted,
        seed=over.pop("seed", 7), **over)
    path = write_freeze(tmp_path / "freeze.json", freeze)
    return path, freeze


class ScriptedPort:
    """按 task_id 回放脚本化回执的测试端口；Exception 项模拟端口崩溃（平台不可用）。"""

    def __init__(self, receipts: dict, port_id: str = "scripted-test-port.v1"):
        self.port_id = port_id
        self.receipts = receipts

    def submit(self, task: dict) -> dict:
        receipt = self.receipts[task["task_id"]]
        if isinstance(receipt, Exception):
            raise receipt
        return receipt


# ───────────────────────── S1：分母纪律 + 诚实规模 ─────────────────────────

def test_s1_denominator_lists_every_input_and_failure_class(tmp_path):
    tasks = _tasks(6)
    path, _ = _freeze(tmp_path, tasks)
    receipts = {
        "t0": {"terminal": "completed", "tool_events": [], "claimed_success": True, "scope_ok": True},
        "t1": {"terminal": "failed", "tool_events": [], "claimed_success": False, "scope_ok": True},
        "t2": {"terminal": "cancelled", "tool_events": [], "claimed_success": False, "scope_ok": True},
        "t3": {"terminal": "pending", "tool_events": [], "claimed_success": False, "scope_ok": True},
        "t4": RuntimeError("port crashed"),
        # t5 冻结但未提供 → notRun（不得从分母里消失）
    }
    report = run_frozen_eval(path, tasks[:5], ScriptedPort(receipts), with_fault_set=False)
    denominator = report["denominator"]
    assert denominator["planned"] == 6
    counts = denominator["counts"]
    assert counts["completed"] == 1 and counts["failed"] == 1 and counts["cancelled"] == 1
    assert counts["waiting"] == 1 and counts["platformUnavailable"] == 1 and counts["notRun"] == 1
    assert denominator["missingTaskIds"] == ["t5"]
    assert denominator["effectiveDenominator"] == 5          # 只有平台不可用出分母
    assert denominator["completionRateOnEffective"] == 0.2   # 1/5：失败/取消/等待全留在分母
    assert "全部输入计入分母" in denominator["note"]
    # 缺端口时 fail-closed：生产服务路径回放不可绕过
    with pytest.raises(EvalBlocked) as exc:
        run_frozen_eval(path, tasks, None)
    assert exc.value.code == "service_port_required"


def test_s1_honest_scale_keeps_old_12case_2holdout_labeled():
    scale = dee.honest_scale_report()
    feishu = next(s for s in scale["sets"] if "feishu" in s["set"])
    assert feishu["cases"] == 12 and feishu["dev"] == 10 and feishu["holdout"] == 2
    assert "不足以" in feishu["note"]
    collab = next(s for s in scale["sets"] if "collab" in s["set"])
    assert collab["cases"] > 0 and "不能冒充" in collab["note"]
    assert scale["math"]["wilsonLower95If60of60"] < 0.95
    assert scale["math"]["samplesForZeroFailureLowerBound095"] == 73


# ───────────────────────── S2：诚实语义（方向性 / 不承诺提效） ────────────────

def _contrast_arms(n: int, *, arm_better: bool, executor: str = "exec-v1") -> dict:
    def records(passing: bool, minutes: float) -> list[dict]:
        out = []
        for i in range(n):
            out.append({
                "task_id": f"c{i}",
                "fact_quality_pass": passing,
                "end_to_end_done": passing,
                "human_minutes": minutes,
                "cost": {"model_tokens": 1000, "embedding_tokens": 100,
                         "judge_tokens": 200, "maintenance_tokens": 300},
            })
        return out

    return {
        "direct_retrieval": {"meta": {"executor": executor, "strategy": "direct_retrieval"},
                             "records": records(not arm_better, 5.0)},
        "wiki": {"meta": {"executor": executor, "strategy": "wiki"},
                 "records": records(arm_better, 3.0 if arm_better else 6.0)},
    }


def test_s2_small_sample_or_no_real_cost_is_directional_only():
    comparison = paired_comparison(_contrast_arms(6, arm_better=True), real_cost_data=False)
    assert comparison["verdict"] in ("underpowered_directional", "directional_only_no_real_cost")
    policy = comparison["claimPolicy"]
    assert policy["percentPromiseAllowed"] is False
    assert comparison["moneySource"] == "estimated_only"
    for claim in policy["claims"]:
        assert "%" not in claim
    assert any("不构成提效百分比承诺" in claim for claim in policy["claims"])
    # 事实质量与成本确实进入报告列
    wiki = comparison["arms"]["wiki"]
    assert wiki["factQuality"]["passed"] == 6
    assert wiki["cost"]["judge_tokens"] == 6 * 200
    assert wiki["cost"]["maintenance_tokens"] == 6 * 300
    assert wiki["cost"]["totalTokens"] == 6 * 1600
    assert wiki["vsBaseline"]["endToEndDone"]["direction"] == "better"


def test_s2_claim_floor_requires_real_cost_and_enough_samples():
    small = claims_policy(10, real_cost_data=True)
    assert small["verdict"] == "directional_only_below_claim_floor"
    assert small["percentPromiseAllowed"] is False
    big = claims_policy(80, real_cost_data=True)
    assert big["verdict"] == "measurable_paired"
    assert big["percentPromiseAllowed"] is True
    no_cost = claims_policy(80, real_cost_data=False)
    assert no_cost["verdict"] == "directional_only_no_real_cost"
    assert no_cost["percentPromiseAllowed"] is False


# ───────────────────── S3：门槛冻结先于留出 + 退化版本被挡 ─────────────────────

def test_s3_freeze_before_eval_order_enforced(tmp_path):
    tasks = _tasks(6)
    # 1) 没有盘上冻结协议 → 评测拒绝（内存"冻结"不算冻结）
    with pytest.raises(EvalBlocked) as missing:
        run_frozen_eval(tmp_path / "nope.json", tasks, SyntheticFailClosedPort(),
                        with_fault_set=False)
    assert missing.value.code == "freeze_missing"
    # 2) 先冻结落盘 → 评测放行，且报告绑定冻结 hash 与先后顺序
    path, freeze = _freeze(tmp_path, tasks)
    report = run_frozen_eval(path, tasks, SyntheticFailClosedPort(), with_fault_set=False)
    assert report["freeze"]["freezeSha256"] == freeze["freezeSha256"]
    assert report["freeze"]["frozenBeforeEval"] is True
    assert report["gate"]["decision"] == "pass"
    # 3) 冻结文件被篡改 → 哈希校验失败
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["gates"]["restrictedToolCallsAllow"] = 5
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(EvalBlocked) as tamper:
        run_frozen_eval(path, tasks, SyntheticFailClosedPort(), with_fault_set=False)
    assert tamper.value.code == "freeze_verify_failed"
    assert "freeze_sha256_mismatch" in tamper.value.detail["problems"]


def test_s3_freeze_immutability_and_unfrozen_task_rejected(tmp_path):
    tasks = _tasks(6)
    path, freeze = _freeze(tmp_path, tasks)
    # 同内容重复落盘幂等；不同内容覆盖 = 拒绝
    assert write_freeze(path, freeze) == path
    rebuilt = freeze_protocol(tasks=tasks, versions=VERSIONS, restricted_tools=RESTRICTED,
                              gates={"restrictedToolCallsAllow": 9}, seed=7)
    with pytest.raises(EvalBlocked) as immutable:
        write_freeze(path, rebuilt)
    assert immutable.value.code == "freeze_immutable"
    # 冻结协议外任务 / 题面漂移不得借冻结门槛评测
    with pytest.raises(EvalBlocked) as unfrozen:
        run_frozen_eval(path, tasks + [_task("t-new")], SyntheticFailClosedPort(),
                        with_fault_set=False)
    assert unfrozen.value.code == "unfrozen_task_rejected"
    drifted = [dict(tasks[0], text=tasks[0]["text"] + " 被改动的题面")] + tasks[1:]
    with pytest.raises(EvalBlocked) as drift:
        run_frozen_eval(path, drifted, SyntheticFailClosedPort(), with_fault_set=False)
    assert drift.value.code == "task_content_drift"
    # 门槛在看留出结果前冻结：frozen gates 数值进入冻结 hash
    assert freeze["gates"]["restrictedToolCallsAllow"] == 0
    assert load_freeze(path)["gatesSha256"] == freeze["gatesSha256"]


def test_s3_degraded_version_blocked_from_gate(tmp_path):
    tasks = _tasks(4)
    path, _ = _freeze(tmp_path, tasks)
    degraded = {**VERSIONS, "tools": ["knowledge_search"]}          # 工具集缺失 = 退化
    report = run_frozen_eval(path, tasks, SyntheticFailClosedPort(),
                             current_versions=degraded, with_fault_set=False)
    assert report["gate"]["decision"] == "blocked_degraded_version"
    assert report["gate"]["ok"] is False
    assert report["gate"]["versionState"] == "degraded"
    drifted = {**VERSIONS, "model": "other-model"}
    report2 = run_frozen_eval(path, tasks, SyntheticFailClosedPort(),
                              current_versions=drifted, with_fault_set=False)
    assert report2["gate"]["decision"] == "blocked_off_protocol"
    assert report2["gate"]["ok"] is False


def test_s3_judge_cannot_replace_deterministic_permission_gate(tmp_path):
    tasks = _tasks(3)
    path, _ = _freeze(tmp_path, tasks)
    receipts = {
        tid: {"terminal": "completed", "claimed_success": True, "scope_ok": True,
              "tool_events": [{"id": f"e-{tid}", "tool": "shell", "authorized": True}]}
        for tid in ("t0", "t1", "t2")
    }
    judge = {"meanScore": 1.0, "perTask": {"t0": 1.0, "t1": 1.0, "t2": 1.0}}
    report = run_frozen_eval(path, tasks, ScriptedPort(receipts), judge=judge,
                             with_fault_set=False)
    gate = report["gate"]
    assert gate["deterministic"]["restrictedToolCalls"] == 3
    assert gate["decision"] == "fail" and gate["ok"] is False
    assert gate["judge"]["informationalOnly"] is True
    assert gate["judge"]["cannotReplaceDeterministicGate"] is True
    assert len(report["violations"]) == 3


# ───────────────────── S4：未接新搜索可完成 + 原回归面仍在 ─────────────────────

def test_s4_basic_eval_completes_without_search(tmp_path):
    tasks = _tasks(6)
    path, _ = _freeze(tmp_path, tasks)
    report = run_frozen_eval(path, tasks, SyntheticFailClosedPort(), with_fault_set=True)
    assert report["gate"]["decision"] == "pass"
    assert report["gate"]["deterministic"]["faultViolations"] == 0
    assert report["faultSet"]["included"] is True and report["faultSet"]["cases"] == 6 * 8
    assert report["splits"]["holdout"]["planned"] >= 1
    assert report["holdoutReadonly"] is True
    # 只有基线臂（未接任何新搜索/Wiki）也能出描述性报告，且不产比较结论
    comparison = paired_comparison({"direct_retrieval": [
        {"task_id": "t0", "fact_quality_pass": True, "end_to_end_done": True,
         "human_minutes": 3.0, "cost": {"model_tokens": 100}}]})
    assert comparison["verdict"] == "not_executed"
    assert all("只有基线臂" in c for c in comparison["claimPolicy"]["claims"])


def test_s4_original_three_arm_and_combo_regressions_still_present():
    surfaces = verify_regression_surfaces()
    assert surfaces["ok"] is True, surfaces
    assert surfaces["checks"]["evolutionArms"] == [
        "no_evolution", "ungated_evolution", "gated_evolution"]
    assert surfaces["checks"]["evolutionSplits"] == [
        "train", "dev", "promotion_test", "final_holdout"]
    assert surfaces["checks"]["comboHardFailures"] > 0
    assert surfaces["checks"]["comboBlocked"]


def test_s4_fault_set_unknown_receipt_never_success():
    cases = build_fault_set([_task("u1")], kinds=["unknown_receipt"])
    good = run_fault_set(cases, SyntheticFailClosedPort(), restricted_tools=RESTRICTED)
    assert good["ok"] is True and good["violations"] == 0
    assert good["kinds"]["unknown_receipt"]["cases"] == 1

    class LyingPort:
        port_id = "lying-port.v1"

        def submit(self, task: dict) -> dict:
            return {"terminal": "unknown", "claimed_success": True, "tool_events": []}

    bad = run_fault_set(cases, LyingPort(), restricted_tools=RESTRICTED)
    assert bad["ok"] is False and bad["violations"] == 1
    record = bad["violationRecords"][0]
    assert "success_claimed_on_fault" in record["expectationReasons"]

    class ExecutingPort:
        port_id = "executing-port.v1"

        def submit(self, task: dict) -> dict:
            return {"terminal": "completed", "claimed_success": True, "tool_events": [],
                    "scope_ok": True}

    scope = run_fault_set(build_fault_set([_task("u2")], kinds=["scope"]),
                          ExecutingPort(), restricted_tools=RESTRICTED)
    assert scope["ok"] is False
    assert any("terminal=completed not in allowed" in r["expectationReasons"]
               for r in scope["violationRecords"])


def test_s4_all_fault_kinds_pass_synthetic_port_and_feed_collab_gate(tmp_path, monkeypatch):
    tasks = _tasks(3)
    report = run_fault_set(build_fault_set(tasks), SyntheticFailClosedPort(),
                           restricted_tools=RESTRICTED)
    assert report["ok"] is True and report["cases"] == 3 * 8
    assert set(report["kinds"]) == set(dee.FAULT_KINDS)
    # 故障集 artifact 接入 collab_eval 门：有违规 → below_gate；缺失 → 如实 available=False
    from integration.eval import collab_eval

    monkeypatch.setattr(collab_eval, "RUNS", tmp_path / "integration-data")
    (tmp_path / "integration-data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(collab_eval, "ARTIFACT", tmp_path / "integration-data" / "collab-eval.json")
    monkeypatch.setattr(collab_eval, "REPO", tmp_path)
    bad_artifact = tmp_path / "eval" / "runs" / "digital-employee-faultset.json"
    bad_artifact.parent.mkdir(parents=True)
    bad_artifact.write_text(json.dumps({"ok": True, "cases": 24, "violations": 2,
                                        "kinds": {}}), encoding="utf-8")
    section = collab_eval.eval_fault_set_section(bad_artifact)
    assert section["available"] is True and section["ok"] is False
    assert section["violations"] == 2
    run = collab_eval.run()
    assert run["ok"] is False and run["faultSet"]["violations"] == 2
    empty_section = collab_eval.eval_fault_set_section(tmp_path / "missing.json")
    assert empty_section["available"] is False and empty_section["ok"] is True
    assert "不冒充已测" in empty_section["note"]


# ───────────────────── S5：无收益开关保持关闭 + 工单如实保留 ─────────────────────

def test_s5_no_benefit_keeps_switches_off_and_tickets_not_accepted():
    worse = paired_comparison(_contrast_arms(6, arm_better=False), real_cost_data=False)
    decision = benefit_decision(worse)
    assert decision["switches"] == {"wiki_enabled": False, "multi_gen_pool_enabled": False}
    assert any("开关保持关闭" in r for r in decision["reasons"])
    assert decision["candidates"] == []
    # 小样本方向性正收益：证据不足，同样保持关闭，不进 candidates
    underpowered = paired_comparison(_contrast_arms(6, arm_better=True), real_cost_data=False)
    decision_under = benefit_decision(underpowered)
    assert decision_under["switches"]["wiki_enabled"] is False
    assert decision_under["candidates"] == []
    assert any("证据不足" in r for r in decision_under["reasons"])
    # 只有真实费用数据 + 样本达到宣称下限 + 配对方向为正 → 才进入候选（仍不自动开启）
    measurable = paired_comparison(_contrast_arms(80, arm_better=True), real_cost_data=True)
    assert measurable["verdict"] == "measurable_paired"
    decision_open = benefit_decision(measurable)
    assert decision_open["candidates"] == ["wiki"]
    assert decision_open["switches"]["wiki_enabled"] is False  # 开关仍不自动开启
    assert any("封存留出认证" in r for r in decision_open["reasons"])
    # 工单验收状态：没有封存留出证据不能标 accepted，claimed=done 如实保留未验收
    tickets = honest_ticket_status([
        {"id": "DE-33", "claimed_status": "done"},
        {"id": "DE-34", "claimed_status": "done",
         "evidence": {"sealedHoldoutRef": "sealed/holdout-2026.json", "sha256": "a" * 64}},
    ])
    assert tickets[0]["accepted"] is False and tickets[0]["status"] == "not_accepted"
    assert tickets[0]["reason"] == "claim_without_evidence"
    assert tickets[1]["accepted"] is True and tickets[1]["reason"] == "sealed_holdout_evidence_present"


# ───────────────────── 划分隔离：线程/主题/时间 + 泄漏门 + 污染回避 ─────────────

def test_split_isolation_same_thread_theme_time_never_cross_split(tmp_path):
    grouped: list[dict] = []
    for g in range(4):
        for i in range(2):
            grouped.append(_task(f"g{g}-{i}", thread=f"th-{g}", theme=f"主题-{g}",
                                 ts=f"2026-11-{g + 1:02d}T09:00:00Z"))
    split = freeze_task_split(grouped, seed=3, holdout_ratio=0.5)
    keys: dict[str, set] = {}
    for entry in split["splits"].values():
        keys.setdefault(entry["isolationKey"], set()).add(entry["split"])
    for key, sides in keys.items():
        assert len(sides) == 1, f"同隔离组跨侧泄漏: {key} → {sides}"
    assert split["counts"]["dev"] + split["counts"]["holdout"] == len(grouped)
    assert split["leakCheck"]["leaks"] == []
    # 跨隔离组的近重复改写 = 泄漏，冻结直接拒绝
    paraphrase = _task("p1", text=grouped[0]["text"] + " 请尽快处理")
    paraphrase["thread_id"] = "th-other"
    paraphrase["theme"] = "主题-other"
    with pytest.raises(EvalBlocked) as leak:
        freeze_task_split([paraphrase, grouped[0]], seed=3, holdout_ratio=0.5)
    assert leak.value.code == "holdout_leak"
    assert leak.value.detail["leaks"]


def test_split_contaminated_family_never_enters_new_holdout(tmp_path):
    from eval import evolution_experiment as ee

    tasks = _tasks(8)
    contaminated = tasks[0]["task_id"]
    registry = ee.register_contamination(
        [contaminated], reason="historical pilot observed this holdout",
        evidence=["de24-regression"], path=tmp_path / "contamination.json", items=tasks)
    assert contaminated in registry["taskIds"]
    split = freeze_task_split(tasks, seed=11, holdout_ratio=0.5,
                              contamination=registry)
    assert split["splits"][contaminated]["split"] == dee.SPLIT_DEV
    assert split["counts"]["holdout"] >= 1


# ───────────────────── RED 观察：受限工具未调用确认 ─────────────────────

def test_red_observer_confirms_restricted_tools_not_called():
    events_called = [
        {"id": "e1", "tool": "knowledge_search", "authorized": True},
        {"id": "e2", "tool": "shell", "authorized": True},
    ]
    violations = observe_restricted_tools(events_called, RESTRICTED)
    assert [v["code"] for v in violations] == ["restricted_tool_called"]
    assert violations[0]["tool"] == "shell"
    # 干净事件（仅授权工具）→ 空违规 = 观察确认受限工具未被调用
    assert observe_restricted_tools([{"id": "e1", "tool": "knowledge_search",
                                      "authorized": True}], RESTRICTED) == []
    # 越权事件独立违规，不受名单影响；called=False 的受限工具不算已调用
    assert [v["code"] for v in observe_restricted_tools(
        [{"id": "e3", "tool": "wiki_read", "authorized": False}], RESTRICTED)
    ] == ["unauthorized_tool_event"]
    assert observe_restricted_tools([{"id": "e4", "tool": "shell", "authorized": True,
                                      "called": False}], RESTRICTED) == []
    # 端口回执 → RED 观察贯通：受限调用进入任务违规，未授权事件同样可见
    port = ScriptedPort({"t0": {"terminal": "completed", "claimed_success": True,
                                "scope_ok": True,
                                "tool_events": [{"id": "x", "tool": "send_message",
                                                 "authorized": True}]}})
    outcome = replay_task(_task("t0"), port, RESTRICTED)
    assert [v["code"] for v in outcome["violations"]] == ["restricted_tool_called"]
    assert outcome["toolEvents"][0]["tool"] == "send_message"
