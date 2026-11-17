# -*- coding: utf-8 -*-
"""统一测试门禁（eval/digital_employee_gate.py）回归（DE-01）。

背景（docs/digital-employee/LOCAL-AUDIT.md §4.4 / A14 / A16、TASKS.md DE-01）：
- 旧口径只有三条分进程 pytest 命令，缺统一入口；收集错误、缺证据、live 试点
  混在同一个退出码里，不可解释。
- 仅改 cwd/HOME 不构成隔离（A16 实测：完整套件曾把原 BACKLOG 及其备份写出
  重复迁移事件）。
- clean checkout 上的失败必须可归因：产品回归 / 冻结证据缺失 / 真实试点未跑
  三类不能互相冒充（指南 §2.2）。

本文件用**合成迷你仓库**驱动**真实门禁脚本**（子进程执行，不 mock 门禁本身），
逐条验证 DE-01 验收场景；每个迷你仓库都放在 pytest 管理的临时目录，跑完即清，
不触碰真实仓库的任何台账/配置/DB/凭据。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "eval" / "digital_employee_gate.py"

PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
SKIP_TEST = (
    "def test_skip_me():\n"
    "    import pytest\n"
    "    pytest.skip('合成跳过：DE-01 演示理由')\n"
)
# 故意收集失败的文件（导入不存在的模块）
COLLECTION_BROKEN = "import nonexistent_module_for_gate_test  # noqa: F401\n"
# 写受保护台账的"恶意"测试（验证写保护，不是真实产品行为）
LEDGER_WRITER = (
    "from pathlib import Path\n"
    "\n"
    "def test_writes_protected_ledger():\n"
    "    p = Path(__file__).resolve().parents[1] / 'eval' / 'loop' / 'BACKLOG.json'\n"
    "    p.write_text('{}', encoding='utf-8')\n"
)


def build_mini_repo(base: Path, *, chinese: bool = False) -> Path:
    """构造合成迷你仓库：tests/ 全绿 + 受保护台账存在。"""
    name = "门禁测试中文目录" if chinese else "gate-mini"
    root = base / name
    (root / "tests").mkdir(parents=True)
    (root / "eval" / "loop").mkdir(parents=True)
    (root / "tests" / "test_ok.py").write_text(PASSING_TEST + SKIP_TEST,
                                               encoding="utf-8")
    (root / "eval" / "loop" / "BACKLOG.json").write_text(
        '{"version": 1, "items": []}\n', encoding="utf-8")
    return root


def run_gate(repo_root: Path, jobs: str, tmp: Path) -> tuple[int, dict]:
    """以子进程运行真实门禁脚本，返回 (退出码, 报告 JSON)。"""
    out_dir = tmp / "gate-out"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(GATE),
         "--jobs", jobs, "--repo-root", str(repo_root),
         "--output-dir", str(out_dir), "--timeout", "300"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900,
    )
    report_path = out_dir / "gate-report.json"
    assert report_path.is_file(), (
        f"门禁未落盘报告。stdout 尾部：\n{proc.stdout[-800:]}\n"
        f"stderr 尾部：\n{proc.stderr[-800:]}"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return proc.returncode, report


def first_suite(report: dict, job: str = "product", name: str = "core") -> dict:
    for suite in report["jobs"][job]["suites"]:
        if suite.get("suite") == name:
            return suite
    raise AssertionError(f"找不到套件 {job}/{name}")


# ---------- 场景 1：clean checkout（中英文路径）得到可解释结果 ----------


def test_gate_product_job_passes_in_english_path(tmp_path):
    _, report = run_gate(build_mini_repo(tmp_path), "product", tmp_path)
    assert report["jobs"]["product"]["status"] == "passed"
    assert report["overall"]["exit_code"] == 0
    suite = first_suite(report)
    assert suite["collected"] == 2 and suite["failed"] == 0 and suite["errors"] == 0
    # 缺失的套件必须显式 not_applicable 并给理由，不能静默消失
    reasons = {s["suite"]: s.get("reason") for s in report["jobs"]["product"]["suites"]
               if s.get("status") == "not_applicable"}
    assert set(reasons) == {"feishu", "integration"}
    assert all(reasons.values())


def test_gate_product_job_passes_in_chinese_path(tmp_path):
    """中文路径 clean checkout 同样可解释（可移植性验收场景）。"""
    _, report = run_gate(build_mini_repo(tmp_path, chinese=True), "product", tmp_path)
    assert report["jobs"]["product"]["status"] == "passed"
    assert report["overall"]["exit_code"] == 0


def test_gate_collection_error_is_surfaced_not_swallowed(tmp_path):
    """收集错误必须计入 errors 并单独列出，其余测试照常执行。"""
    root = build_mini_repo(tmp_path)
    (root / "tests" / "test_broken.py").write_text(COLLECTION_BROKEN,
                                                   encoding="utf-8")
    (root / "tests" / "test_more.py").write_text(PASSING_TEST, encoding="utf-8")
    code, report = run_gate(root, "product", tmp_path)
    assert report["overall"]["exit_code"] == 1
    suite = first_suite(report)
    assert suite["errors"] >= 1, "收集错误被吞掉"
    assert any("test_broken" in err["id"] for err in suite["collection_errors"])
    assert suite["passed"] >= 2, "--continue-on-collection-errors 下其余测试仍执行"
    assert code == 1


def test_gate_real_failure_reports_regression(tmp_path):
    """真实断言失败 → 退出码 1 + 失败 node ID 在案。"""
    root = build_mini_repo(tmp_path)
    (root / "tests" / "test_fail.py").write_text(
        "def test_should_fail():\n    assert 2 + 2 == 5\n", encoding="utf-8")
    code, report = run_gate(root, "product", tmp_path)
    assert code == 1
    suite = first_suite(report)
    assert suite["failed"] == 1
    assert any("test_should_fail" in p["id"] for p in suite["problems"])


# ---------- 场景 2 之三 job 语义：缺证据 / live 不冒充通过 ----------


def test_gate_evidence_job_missing_evidence_is_explicit_non_pass(tmp_path):
    """无冻结证据包 → evidence job = missing_evidence（非通过），不执行不伪造。"""
    root = build_mini_repo(tmp_path)
    code, report = run_gate(root, "evidence", tmp_path)
    job = report["jobs"]["evidence"]
    assert job["status"] == "missing_evidence"
    assert job["exit_code"] == 3
    assert job["reason"], "缺证据理由必须写明"
    assert job["suites"] == []
    assert code == 3


def test_gate_evidence_job_runs_when_bundle_present(tmp_path):
    """证据包在位 → evidence job 真实执行证据类测试。"""
    root = build_mini_repo(tmp_path)
    (root / "eval" / "runs").mkdir(parents=True)
    (root / "eval" / "runs" / "gaia").mkdir()
    (root / "eval" / "runs" / "gaia" / "summary.json").write_text("{}", encoding="utf-8")
    (root / "tests" / "test_final_audit.py").write_text(PASSING_TEST,
                                                        encoding="utf-8")
    code, report = run_gate(root, "evidence", tmp_path)
    job = report["jobs"]["evidence"]
    assert job["status"] == "passed"
    assert "tests/test_final_audit.py" in job["executed_files"]
    assert code == 0


def test_gate_live_job_never_fakes_pass(tmp_path):
    """live 试点在离线门禁中 = not_run_live（非通过），且不执行任何套件。"""
    root = build_mini_repo(tmp_path)
    code, report = run_gate(root, "live", tmp_path)
    job = report["jobs"]["live"]
    assert job["status"] == "not_run_live"
    assert job["exit_code"] == 3
    assert job["suites"] == []
    assert job["reason"]
    # 前提清单必须逐项给出事实，而不是只丢一个 ok:true
    assert job["prerequisites"] and all("ok" in p and "detail" in p
                                        for p in job["prerequisites"])
    assert code == 3


# ---------- 场景 2 之隔离：仅改 cwd 不算隔离，写保护必须验证真实写入 ----------


def test_gate_write_guard_flags_protected_ledger_mutation(tmp_path):
    """测试写受保护台账 → 隔离破坏，整体退出码 2，变更文件点名。"""
    root = build_mini_repo(tmp_path)
    (root / "tests" / "test_evil_writer.py").write_text(LEDGER_WRITER,
                                                        encoding="utf-8")
    code, report = run_gate(root, "product", tmp_path)
    assert report["write_guard"]["breach"] is True
    changed = report["write_guard"]["changed"]
    assert any(c["path"].endswith("eval/loop/BACKLOG.json")
               and c["change"] == "modified" for c in changed)
    assert report["overall"]["exit_code"] == 2
    assert code == 2


def test_gate_write_guard_silent_when_tests_behave(tmp_path):
    root = build_mini_repo(tmp_path)
    _, report = run_gate(root, "product", tmp_path)
    assert report["write_guard"]["breach"] is False
    assert report["write_guard"]["changed"] == []


def test_gate_write_guard_covers_extra_protect_root(tmp_path):
    """--protect-root 额外根（副本测试写原仓库场景）也被快照核对。"""
    original = tmp_path / "original-repo"
    (original / "eval" / "loop").mkdir(parents=True)
    ledger = original / "eval" / "loop" / "BACKLOG.json"
    ledger.write_text('{"version": 1}\n', encoding="utf-8")

    copy = build_mini_repo(tmp_path)
    # 副本里的测试写"原仓库"的台账（模拟 A16 的绝对路径越界写）
    (copy / "tests" / "test_escape.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "def test_writes_original_ledger():\n"
        "    p = Path(r'" + str(ledger).replace("\\", "\\\\") + "')\n"
        "    p.write_text('{\"version\": 2}', encoding='utf-8')\n",
        encoding="utf-8")
    out_dir = tmp_path / "gate-out2"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(GATE),
         "--jobs", "product", "--repo-root", str(copy),
         "--protect-root", str(original),
         "--output-dir", str(out_dir), "--timeout", "300"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900,
    )
    report = json.loads((out_dir / "gate-report.json").read_text(encoding="utf-8"))
    assert any(c["root"] == str(original) for c in report["write_guard"]["changed"])
    assert proc.returncode == 2
    assert report["overall"]["exit_code"] == 2


# ---------- 场景 3：node ID / skip 理由 / 计数 / 退出码 / 源码哈希可对照 ----------


def test_gate_records_nodeids_skip_reasons_counts_and_digest(tmp_path):
    root = build_mini_repo(tmp_path)
    _, report = run_gate(root, "product", tmp_path)
    suite = first_suite(report)
    assert "tests.test_ok::test_ok" in suite["node_ids"]
    skips = {s["nodeid"]: s["reason"] for s in suite["skips"]}
    assert skips.get("tests.test_ok::test_skip_me") == "合成跳过：DE-01 演示理由"
    assert suite["exit_code"] == 0 and suite["collected"] == 2
    digest = report["source_digest"]
    assert re.fullmatch(r"[0-9a-f]{64}", digest["aggregate_sha256"])
    assert digest["files"] >= 1
    assert re.fullmatch(r"[0-9a-f]{64}", digest["gate_sha256"])


def test_gate_product_excludes_evidence_files_but_keeps_them_visible(tmp_path):
    """冻结证据类文件不进 product job，但必须在报告里留痕（不静默移除）。"""
    root = build_mini_repo(tmp_path)
    (root / "tests" / "test_evidence_index.py").write_text(PASSING_TEST,
                                                           encoding="utf-8")
    (root / "tests" / "test_final_audit.py").write_text(PASSING_TEST,
                                                        encoding="utf-8")
    _, report = run_gate(root, "product,evidence", tmp_path)
    excluded = report["jobs"]["product"]["excluded_to_evidence_job"]
    assert "tests/test_evidence_index.py" in excluded
    assert "tests/test_final_audit.py" in excluded
    core = first_suite(report)
    assert not any("test_evidence_index" in nid for nid in core["node_ids"])
    # 证据文件由 evidence job 承接（此处证据包缺失 → 明确非通过而非消失）
    assert report["jobs"]["evidence"]["status"] == "missing_evidence"


def test_gate_overall_exit_precedence_breach_beats_failure(tmp_path):
    """既有测试失败又有隔离破坏时，隔离破坏（2）优先级高于测试失败（1）。"""
    root = build_mini_repo(tmp_path)
    (root / "tests" / "test_fail.py").write_text(
        "def test_should_fail():\n    assert False\n", encoding="utf-8")
    (root / "tests" / "test_evil_writer.py").write_text(LEDGER_WRITER,
                                                        encoding="utf-8")
    code, report = run_gate(root, "product", tmp_path)
    assert code == 2
    assert report["jobs"]["product"]["exit_code"] == 1  # 测试失败事实同时保留


# ---------- 门禁自身可移植性契约 ----------


def test_gate_source_has_no_absolute_paths():
    """门禁脚本不得携带绝对路径/平台专有调用（中英文路径、Windows/Linux 可移植）。"""
    source = GATE.read_text(encoding="utf-8")
    # 剔除 URL（http://127.0.0.1:9 一类假端点）后再查盘符路径
    without_urls = re.sub(r"https?://\S+", "", source)
    assert not re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", without_urls), (
        "门禁源码包含盘符绝对路径"
    )
    for forbidden in ("os.startfile", "winreg", "msvcrt", "posix_spawn"):
        assert forbidden not in source, f"门禁依赖平台专有 API：{forbidden}"


def test_gate_usage_error_exit_code(tmp_path):
    """未知 job → 退出码 4（用法错误），不落报告不误报。"""
    root = build_mini_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(GATE),
         "--jobs", "no-such-job", "--repo-root", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    assert proc.returncode == 4
    assert "未知 job" in (proc.stderr or "") + (proc.stdout or "")


def test_gate_declares_evidence_split_matches_audit_baseline():
    """证据类文件清单必须与 2026-11-13 审计证据里的失败归因一致，不得随意增删。"""
    import ast

    tree = ast.parse(GATE.read_text(encoding="utf-8"))
    constants = {
        node.targets[0].id: [elt.value for elt in node.value.elts]
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"EVIDENCE_DEPENDENT_TESTS", "EVIDENCE_PROBE_PATHS"}
        and isinstance(node.value, ast.List)
    }
    expected = {
        "tests/test_docs_consistency.py",
        "tests/test_evidence_index.py",
        "tests/test_final_audit.py",
        "tests/test_frozen_protocol.py",
        "tests/test_eval_protocol_freeze.py",
        "tests/test_long_function_assessment.py",
        "tests/test_claim_audit.py",
        "tests/test_gate_baseline_integrity.py",
        "tests/test_loop_source_health.py",
    }
    assert set(constants["EVIDENCE_DEPENDENT_TESTS"]) == expected


@pytest.mark.parametrize("args", [["--jobs", ""], ["--jobs", ",,"], ["--timeout", "0"]])
def test_gate_rejects_empty_jobs_and_nonpositive_timeout(tmp_path, args):
    root = build_mini_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(GATE), "--repo-root", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 4


def test_gate_digest_binds_product_source_and_preserves_full_log(tmp_path):
    root = build_mini_repo(tmp_path)
    (root / "agents").mkdir()
    source = root / "agents" / "product.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_verbose.py").write_text(
        "def test_verbose():\n"
        "    print('BEGIN_FULL_LOG')\n"
        "    print('\\n'.join('detail-%d' % i for i in range(30)))\n"
        "    assert False, 'EXPECTED_LOG_PROBE'\n", encoding="utf-8")
    code, before = run_gate(root, "product", tmp_path)
    assert code == 1
    suite = first_suite(before)
    log = Path(suite["log_path"]).read_text(encoding="utf-8")
    assert "BEGIN_FULL_LOG" in log and "detail-29" in log
    assert Path(suite["junit_path"]).is_file()
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _, after = run_gate(root, "product", tmp_path)
    assert before["source_digest"]["aggregate_sha256"] != after["source_digest"]["aggregate_sha256"]


def test_missing_bundle_does_not_hide_evidence_suite_regressions(tmp_path):
    root = build_mini_repo(tmp_path)
    (root / "tests" / "test_final_audit.py").write_text(
        "def test_missing_constraint_regression():\n"
        "    assert False, 'missing constraint in product'\n", encoding="utf-8")
    code, report = run_gate(root, "evidence", tmp_path)
    job = report["jobs"]["evidence"]
    assert code == 1
    assert job["status"] == "failed"
    assert job["suites"][0]["failed"] == 1
    assert job["classification"]["regression"] == [
        "tests.test_final_audit::test_missing_constraint_regression"]
