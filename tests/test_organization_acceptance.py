"""DE-44 回归：GitHub 单产品交付与完整组织验收（指南 §21 八场景）。

全部离线（临时 SQLite、无网络、无飞书；三态表只读本地 git 索引）：

1. §21 场景 1（无飞书根入口初始化合成组织、一个目标跨两单元三岗位协作交付）
   与场景 7（费用/等待/失败归属、预算耗尽停止新调用）离线端到端组合断言；
2. 场景 2（离任/撤权围栏 + 接任不继承个人 token）与场景 6（跨入口一致权限/
   状态；核心无飞书 SDK 反向依赖——ast 静态断言 evoharness/ 不 import 飞书 SDK）；
3. 三态表生成逻辑：远端列恒为未推送（未经授权），本地/Git 列有真实文件断言；
4. clean checkout 报告结构：统计行解析 + 失败归类清单结构可执行；
5. ORGANIZATION-ACCEPTANCE 与 FINAL-ACCEPTANCE 状态一致（引用同一 45 项表，
   无矛盾行）。

接线说明：场景执行直接复用 eval/organization_eval.py 的真实服务组合
（DE-43 种子 + DE-28 契约层 + DE-38/39/40/41/42 领域服务），不在测试里复刻逻辑。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import eval.organization_eval as org_eval  # noqa: E402
from eval.organization_eval import (  # noqa: E402
    CLEAN_CHECKOUT_SUITES,
    FAILURE_CLASSES,
    REMOTE_STATE,
    ScenarioEnv,
    check_s1,
    check_s2,
    check_s6,
    check_s7,
    clean_checkout_report,
    deliverable_inventory,
    offline_fixture_inventory,
    parse_pytest_summary_line,
    scan_core_for_feishu_sdk,
    validate_clean_checkout_report,
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # 场景 1 前提：无飞书配置（相关环境变量一律不存在），业务库为临时 SQLite
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("EVOHARNESS_DATABASE_URL", raising=False)
    return ScenarioEnv(tmp_path)


def _assert_all_pass(scenario_checks: list[dict]):
    failed = [c for c in scenario_checks if not c["ok"]]
    assert not failed, failed


# ── 组 1：场景 1 + 场景 7 离线端到端组合 ─────────────────────────────────────

def test_scenario1_synthetic_org_end_to_end_without_feishu(env):
    outcome = check_s1(env)
    _assert_all_pass(outcome["checks"])
    names = {c["name"] for c in outcome["checks"]}
    assert {"seed_without_feishu_credentials", "human_and_ai_memberships",
            "plan_dependency_dag_confirmed", "two_unit_three_position_delivery",
            "objective_milestones_evidenced_by_acceptance"} <= names


def test_scenario7_cost_waiting_failure_attribution(env):
    outcome = check_s7(env)
    _assert_all_pass(outcome["checks"])
    names = {c["name"] for c in outcome["checks"]}
    assert {"reservation_attributed_to_principal",
            "budget_exhausted_stops_new_calls", "cost_rollup_records_real_usage",
            "failure_attribution_recorded",
            "failed_run_not_takeoverable_not_fake_inflight",
            "failed_task_requeued_by_owner",
            "waiting_visible_in_view_with_reason"} <= names


# ── 组 2：场景 2 围栏 + 场景 6 跨入口一致/无飞书 SDK 反向依赖 ────────────────

def test_scenario2_revocation_fence_and_successor(env):
    outcome = check_s2(env)
    _assert_all_pass(outcome["checks"])
    names = {c["name"] for c in outcome["checks"]}
    assert {"revocation_fences_running_run", "fenced_run_cannot_continue",
            "fenced_run_cannot_be_claimed", "in_flight_receipt_reconciliation_open",
            "offboard_ends_assignments", "successor_has_no_personal_token_columns",
            "takeover_moves_run_to_waiting_recovery",
            "takeover_receipt_open_not_fake_success",
            "successor_can_start_new_run"} <= names


def test_scenario6_cross_entry_consistency_and_no_feishu_sdk(env):
    outcome = check_s6(env, REPO)
    _assert_all_pass(outcome["checks"])
    # 静态反向依赖断言独立复核：ast 扫描核心包，飞书 SDK 零 import
    assert scan_core_for_feishu_sdk(REPO) == []
    server_app = (REPO / "server" / "app.py").read_text(encoding="utf-8")
    assert "from evoharness.__main__ import org_view" in server_app


# ── 组 3：三态表生成逻辑（远端恒未推送；本地/Git 列真实断言） ────────────────

def test_scenario8_deliverable_inventory_remote_never_pushed():
    rows = deliverable_inventory(REPO)
    assert rows, "三态交付清单不能为空"
    for row in rows:
        assert row["remote"] == REMOTE_STATE
        assert "未推送" in REMOTE_STATE and "git push" in REMOTE_STATE
    by_path = {row["path"]: row for row in rows}
    # 真实存在且已入 Git（分支跟踪）的交付物
    readme = by_path["README.md"]
    assert readme["local_exists"] is True and readme["tracked_in_git_branch"] is True
    # 本任务新建文件：本地存在；已入 Git 与否是主控提交后的独立事实（不伪造）
    org_eval_row = by_path["eval/organization_eval.py"]
    assert org_eval_row["local_exists"] is True
    # 不存在的路径：本地不存在且未跟踪，远端列仍恒为未推送
    import eval.organization_eval as module
    original = module.DELIVERABLE_PATHS
    try:
        module.DELIVERABLE_PATHS = original + ["no/such/deliverable.txt"]
        rows2 = deliverable_inventory(REPO)
    finally:
        module.DELIVERABLE_PATHS = original
    ghost = next(r for r in rows2 if r["path"] == "no/such/deliverable.txt")
    assert ghost["local_exists"] is False and ghost["tracked_in_git_branch"] is False
    assert ghost["remote"] == REMOTE_STATE
    # 离线 fixture 清单：证据依赖测试可点名且在树内存在
    fixtures = offline_fixture_inventory(REPO)
    assert fixtures["evidence_dependent_tests"]
    assert all(item["exists_in_tree"] for item in fixtures["evidence_dependent_tests"])


# ── 组 4：clean checkout 报告结构（统计行 + 失败归类清单可执行） ─────────────

def _valid_clean_checkout_report() -> dict:
    suites = {
        "core": {"command": ["python", "-m", "pytest", "tests", "-q"],
                 "exit_code": 0,
                 "summary_line": "1200 passed, 2 skipped in 301.2s",
                 "stats": {"passed": 1200, "failed": 0, "errors": 0,
                           "skipped": 2, "warnings": 0}},
        "feishu": {"command": ["python", "-m", "pytest", "tests", "-q"],
                   "exit_code": 0,
                   "summary_line": "400 passed in 88.0s",
                   "stats": {"passed": 400, "failed": 0, "errors": 0,
                             "skipped": 0, "warnings": 0}},
        "integration": {"command": ["python", "-m", "pytest", "integration/tests", "-q"],
                        "exit_code": 1,
                        "summary_line": "1 failed, 9 passed in 12.3s",
                        "stats": {"passed": 9, "failed": 1, "errors": 0,
                                  "skipped": 0, "warnings": 0}},
    }
    failures = [{"suite": "integration",
                 "node_id": "integration/tests/test_x.py::test_needs_local_artifact",
                 "classification": "missing_evidence_artifact",
                 "detail": "FileNotFoundError: eval/runs/..."}]
    return clean_checkout_report(clone_source="file:///tmp/evo-clone", branch="codex/evo-v14-iteration",
                                 commit="abc123", suites=suites, failures=failures,
                                 notes=["clean checkout 实测由验收流程执行"])


def test_parse_pytest_summary_line_variants():
    assert parse_pytest_summary_line(
        "3 failed, 1200 passed, 2 skipped in 30.1s") == {
        "passed": 1200, "failed": 3, "errors": 0, "skipped": 2, "warnings": 0}
    assert parse_pytest_summary_line("1247 passed in 5.0s")["passed"] == 1247
    assert parse_pytest_summary_line("1 error in 2.0s")["errors"] == 1
    assert parse_pytest_summary_line("no statistics here") is None
    assert parse_pytest_summary_line("") is None


def test_scenario8_clean_checkout_report_structure():
    # 结构完整 + 统计行一致 + 失败已归类 → 无错误
    report = _valid_clean_checkout_report()
    assert validate_clean_checkout_report(report) == []
    assert sorted(report["suites"]) == sorted(CLEAN_CHECKOUT_SUITES)
    # 统计行解析与 stats 不一致必须报错（统计行不可伪造）
    broken = json.loads(json.dumps(report))
    broken["suites"]["core"]["stats"]["passed"] = 1199
    assert "summary_line_stats_mismatch:core" in validate_clean_checkout_report(broken)
    # 缺套件 / 缺字段 / 未归类失败 / 未知分类 / 远端被改成已推送都必须报错
    broken2 = json.loads(json.dumps(report))
    del broken2["suites"]["feishu"]
    assert "suite_missing:feishu" in validate_clean_checkout_report(broken2)
    broken3 = json.loads(json.dumps(report))
    del broken3["commit"]
    assert "missing_field:commit" in validate_clean_checkout_report(broken3)
    broken4 = json.loads(json.dumps(report))
    broken4["failures"] = []
    assert "failures_not_classified:integration" in validate_clean_checkout_report(broken4)
    broken5 = json.loads(json.dumps(report))
    broken5["failures"][0]["classification"] = "unknown_class"
    assert any(e.startswith("failure_class_unknown:") for e in
               validate_clean_checkout_report(broken5))
    broken6 = json.loads(json.dumps(report))
    broken6["remote"] = "已推送"
    assert "remote_state_must_be_unpushed:已推送" in validate_clean_checkout_report(broken6)
    # not_applicable 套件必须给原因（integration/tests 不存在时如实记录）
    partial = json.loads(json.dumps(report))
    partial["suites"]["integration"] = {"status": "not_applicable",
                                        "reason": "integration/tests/ 不存在"}
    partial["failures"] = []
    assert validate_clean_checkout_report(partial) == []
    assert set(FAILURE_CLASSES) == {"missing_evidence_artifact", "live_dependency",
                                    "distributable_fixture_gap", "regression"}


def test_organization_eval_cli_selected_scenario_and_markdown(tmp_path):
    out_json = tmp_path / "report.json"
    out_md = tmp_path / "report.md"
    code = org_eval.main(["--root", str(REPO), "--only", "S4",
                          "--output", str(out_json), "--markdown", str(out_md)])
    assert code == 0
    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert report["scenarios"][0]["id"] == "S4"
    assert report["scenarios"][0]["result"] == "verified_offline"
    assert report["final_acceptance_ref"] == "eval/acceptance_check.py"
    markdown = out_md.read_text(encoding="utf-8")
    assert "S4" in markdown and "交付物三态表" in markdown


# ── 组 5：ORGANIZATION-ACCEPTANCE 与 FINAL-ACCEPTANCE 状态一致 ───────────────

from delivery_fixtures import delivery_fixture
CONTRACT_ROOT = delivery_fixture()
FINAL_ACCEPTANCE = CONTRACT_ROOT / "docs/digital-employee/acceptance/FINAL-ACCEPTANCE.md"
ORG_ACCEPTANCE = CONTRACT_ROOT / "docs/digital-employee/acceptance/DE-44/ORGANIZATION-ACCEPTANCE.md"


def _final_table_ids(text: str) -> set[str]:
    ids = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("| DE-") and stripped.endswith("|"):
            cell = stripped.split("|")[1].strip()
            if re.fullmatch(r"DE-\d{2}", cell):
                ids.add(cell)
    return ids


def _final_table_statuses(text: str) -> dict[str, str]:
    """解析 DE-xx 行的状态列（第 2 列）。

    §2 主表优先：§8 搜索线子表会重复 DE-29~35 行，保留**首次**出现（§2 在前）。
    """
    statuses: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("| DE-") and stripped.endswith("|")):
            continue
        cells = [c.strip() for c in stripped.split("|")]
        # cells[0] 为空（行首分隔符），cells[1]=DE-xx，cells[2]=状态
        if len(cells) >= 3 and re.fullmatch(r"DE-\d{2}", cells[1]):
            statuses.setdefault(cells[1], cells[2])
    return statuses


def test_organization_acceptance_consistent_with_final_acceptance():
    final_text = FINAL_ACCEPTANCE.read_text(encoding="utf-8")
    org_text = ORG_ACCEPTANCE.read_text(encoding="utf-8")
    # 引用同一份 45 项工单状态表（ORG 以仓库相对路径引用）
    assert "docs/digital-employee/acceptance/FINAL-ACCEPTANCE.md" in org_text
    # §2 主表 45 项全覆盖（§8 搜索线子表重复 DE-29~35，按唯一 ID 计）
    final_ids = _final_table_ids(final_text)
    expected = {f"DE-{i:02d}" for i in range(45)}
    assert final_ids == expected, sorted(expected - final_ids)
    # v1.1（2026-11-15 审核 R10 更正）：FINAL §2 与原台账 deMapping 实时一致，
    # 逐项比对（替代旧"时点快照原句"断言——快照口径已废除，此断言更强）。
    backlog_path = CONTRACT_ROOT / "eval" / "loop" / "BACKLOG.json"
    de_mapping = json.loads(backlog_path.read_text(encoding="utf-8"))["deMapping"]["tasks"]
    final_status = _final_table_statuses(final_text)
    for de_id in sorted(expected):
        live = de_mapping.get(de_id, {}).get("status", "pending")
        shown = final_status[de_id]
        # DE-26/27 为原计划可选工单：FINAL 表标 optional（更精确），与 deMapping 无条目
        # 按 pending 计等价；其余工单必须逐项严格一致。
        if de_id in {"DE-26", "DE-27"}:
            assert shown in {"optional", live}, (
                f"{de_id}: FINAL-ACCEPTANCE={shown} vs deMapping={live}")
        else:
            assert shown == live, (
                f"{de_id}: FINAL-ACCEPTANCE={shown} vs deMapping={live}")
    # 统计行与 deMapping 实际计数一致（文档统计行可带括号注解，按数值断言）
    counts: dict[str, int] = {}
    for entry in de_mapping.values():
        counts[entry.get("status", "pending")] = counts.get(entry.get("status", "pending"), 0) + 1
    summary_match = re.search(
        r"45 项 = done (\d+).*?blocked (\d+).*?(?:optional|pending) (\d+)", final_text)
    assert summary_match, "FINAL-ACCEPTANCE 缺少统计行"
    assert int(summary_match.group(1)) == counts.get("done", 0)
    assert int(summary_match.group(2)) == counts.get("blocked", 0)
    # optional 计数按 §2 表格实数（DE-26/27）
    assert sum(1 for v in final_status.values() if v == "optional") == int(summary_match.group(3))
    # ORG 不得宣称 45 项全部完成或企业生产成熟；否定语境（"不得宣称：X"）是合法引用，
    # 先剥离该句式再断言
    forbidden_claims = ("45 项全部 done", "45 项全部完成", "企业生产成熟",
                        "全部验收通过", "生产环境全面自动")
    for claim in forbidden_claims:
        cleaned = org_text.replace(f"**不得宣称**：{claim}", "")
        assert claim not in cleaned, claim
    # ORG 引用指南 §21 并保持诚实口径标记
    assert "docs/DIGITAL-EMPLOYEE-EXECUTION-GUIDE.md" in org_text
    assert "not_run_live" in org_text
    # FINAL §1 声明与 deMapping 同步（v1.1 口径），不再自称时点快照
    final_head = final_text.split("## 2.")[0]
    assert "时点快照" not in final_head.replace("不再是时点快照", "")
