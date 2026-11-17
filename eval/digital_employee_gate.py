#!/usr/bin/env python3
"""EvoHarness 数字员工统一测试门禁（DE-01）。

背景（docs/digital-employee/LOCAL-AUDIT.md §4.4 / A14 / A16）：
- 旧口径只有三条分进程 pytest 命令，缺统一入口；收集错误、缺证据、live 试点
  三种"非通过"混在一起，无法解释，也容易吞掉。
- 根套件里有一部分测试验证的是"冻结实验证据"（依赖未跟踪的 eval/runs/
  实验产物），clean checkout 上必然失败；把它们与纯产品回归分开成显式 job，
  失败才可归因（指南 §2.2：划清纯产品回归/冻结证据验收/真实服务试点）。
- 仅改 cwd/HOME 不构成隔离（A16）：本门禁在跑套件前后对受保护文件做
  sha256 快照，任何写入（台账/配置/DB/凭据）都判为隔离破坏，整体非零退出。

三个显式 job（--jobs 逗号选择，默认全部）：
  product  产品回归：根 tests/（剔除下列冻结证据类文件）+ feishu-daily-agent/tests/
           （独立 cwd，避免同名 tests/app 包污染）+ integration/tests/（存在才跑），
           每个套件分进程、离线假凭据、临时 HOME。
  evidence 冻结证据验收：只跑依赖实验产物/冻结协议的分析验收测试；
           证据包缺失 → 状态 missing_evidence（明确非通过，不假装绿）。
  live     真实试点：本门禁只检查前提条件并如实报告 not_run_live（非通过）；
           真实试点验收需要真实凭据、授权与人工协议，绝不在离线环境伪造通过。

用法：
  python eval/digital_employee_gate.py                       # 三个 job 全跑
  python eval/digital_employee_gate.py --jobs product        # 只跑产品回归
  python eval/digital_employee_gate.py --output-dir DIR      # 报告/日志落盘 DIR
  python eval/digital_employee_gate.py --repo-root PATH      # 被门禁的仓库根
  python eval/digital_employee_gate.py --protect-root PATH   # 额外写保护根（可重复）
  python eval/digital_employee_gate.py --timeout SECONDS     # 每套件超时（默认 1800）

退出码：0 全部通过；1 存在测试失败；2 隔离破坏（受保护文件被写）；
3 缺冻结证据或缺真实环境（job 明确非通过）；4 用法/内部错误。

旧的三条分进程 pytest 命令继续可用（兼容入口）；本门禁失败时阻止交付，
不允许通过回退旧命令隐藏真实失败。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE_VERSION = 1

# ---------------------------------------------------------------------------
# 冻结证据依赖的测试文件（相对仓库根）。
# 判据（docs/digital-employee/evidence/test-summary.json，2026-11-13 审计）：
# 这些文件在 clean checkout（无未跟踪 eval/runs 实验产物）上失败的直接原因
# 是证据包缺失（FileNotFoundError / missing-evidence / budget-missing），
# 而不是产品逻辑回归。它们从 product job 剔除、进入 evidence job —— 两个 job
# 合计覆盖根套件全部文件，没有任何测试被移出统计。
# ---------------------------------------------------------------------------
EVIDENCE_DEPENDENT_TESTS = [
    "tests/test_docs_consistency.py",
    "tests/test_evidence_index.py",
    "tests/test_final_audit.py",
    "tests/test_frozen_protocol.py",
    "tests/test_eval_protocol_freeze.py",
    "tests/test_long_function_assessment.py",
    "tests/test_claim_audit.py",
    "tests/test_gate_baseline_integrity.py",
    "tests/test_loop_source_health.py",
]

# evidence job 的证据包探针：全部缺失 → missing_evidence（不执行、不伪造）。
EVIDENCE_PROBE_PATHS = [
    "eval/runs/gaia/summary.json",
    "eval/runs/long-function-assessment.json",
    "eval/runs/docs-consistency.json",
]

# 失败归因为"证据缺失"的消息特征（大小写不敏感子串）。
MISSING_EVIDENCE_MARKERS = [
    "no such file or directory",
    "filenotfounderror",
    "missing-evidence:",
    "证据不存在：",
    "budget-missing",
    "not a directory",
]

# ---------------------------------------------------------------------------
# 受保护文件（相对仓库根 / 每个 --protect-root）。测试运行期间被写入即隔离破坏。
# 口径 = 台账 / 配置 / DB / 凭据（A16、任务纪律："测试不触碰部署数据/凭据"）。
# ---------------------------------------------------------------------------
PROTECTED_GLOBS = [
    ".env",
    "feishu-daily-agent/.env",
    "feishu-daily-agent/token_key*",
    "feishu-daily-agent/*.db",
    "feishu-daily-agent/*.db-*",
    "feishu-daily-agent/data/**",
    "integration/data/**",
    "eval/loop/*.json",
    "eval/loop/*.json.*",
]

# 离线假凭据：覆盖而非透传，子进程拿不到真实密钥（也不记录任何真实值）。
FAKE_ENV = {
    "FEISHU_TEST_MODE": "1",
    "FEISHU_DEPLOYMENT_MODE": "offline",
    "FEISHU_AUTO_REPLY_ENABLED": "0",
    "FEISHU_SHADOW_MODE": "1",
    "FEISHU_ALLOW_LIVE_MODEL": "0",
    "FEISHU_APP_ID": "offline-gate-app",
    "FEISHU_APP_SECRET": "offline-gate-secret",
    "FEISHU_TOKEN_SECRET": "offline-test-only-token-secret",
    "FEISHU_OWNER_OPEN_ID": "ou_gate_owner",
    "FEISHU_BOT_OPEN_ID": "ou_gate_bot",
    "LLM_API_KEY": "offline-gate-key",
    "LLM_BASE_URL": "http://127.0.0.1:9",
    "LLM_MODEL": "offline-gate-model",
    "OPENAI_API_KEY": "offline-gate-key",
    "ANTHROPIC_API_KEY": "offline-gate-key",
    "APIKEY": "offline-gate-key",
    "API": "http://127.0.0.1:9/v1",
}

EXIT_OK = 0
EXIT_TEST_FAILURE = 1
EXIT_ISOLATION_BREACH = 2
EXIT_MISSING_PREREQ = 3
EXIT_USAGE = 4

EXIT_CODE_MEANING = {
    EXIT_OK: "全部所选 job 通过",
    EXIT_TEST_FAILURE: "存在测试失败（产品回归或证据类真实回归）",
    EXIT_ISOLATION_BREACH: "隔离破坏：受保护文件（台账/配置/DB/凭据）被测试写入",
    EXIT_MISSING_PREREQ: "缺冻结证据或缺真实环境：对应 job 明确非通过",
    EXIT_USAGE: "用法/内部错误",
}


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 受保护文件快照（写保护验证）


def protected_files(root: Path) -> list[str]:
    """root 下命中受保护 glob 的相对路径（去重、排序）。"""
    hits: set[str] = set()
    for pattern in PROTECTED_GLOBS:
        for path in root.glob(pattern):
            if path.is_file():
                try:
                    hits.add(path.relative_to(root).as_posix())
                except ValueError:
                    continue
    return sorted(hits)


def snapshot_protected(roots: list[Path]) -> dict[str, str]:
    """{root_label: {relpath: sha256|"<absent>"}}。"""
    snapshot: dict[str, dict[str, str]] = {}
    for root in roots:
        label = str(root)
        entry: dict[str, str] = {}
        for rel in protected_files(root):
            path = root / rel
            entry[rel] = sha256_file(path) if path.is_file() else "<absent>"
        snapshot[label] = entry
    return snapshot


def diff_snapshots(
    before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for label, files in after.items():
        old = before.get(label, {})
        for rel, digest in files.items():
            if old.get(rel) != digest:
                kind = (
                    "created"
                    if rel not in old
                    else "deleted"
                    if digest == "<absent>"
                    else "modified"
                )
                changes.append({"root": label, "path": rel, "change": kind})
        for rel in old:
            if rel not in files:
                changes.append({"root": label, "path": rel, "change": "deleted"})
    return changes


# ---------------------------------------------------------------------------
# 源码摘要（收集数/退出码/源码哈希三件套之一）


def source_digest(repo_root: Path) -> dict:
    roots = [
        repo_root / "agents",
        repo_root / "server",
        repo_root / "evoharness",
        repo_root / "eval",
        repo_root / "integration",
        repo_root / "feishu-daily-agent" / "app",
        repo_root / "tests",
        repo_root / "feishu-daily-agent" / "tests",
    ]
    files: list[str] = []
    for base in roots:
        if base.is_dir():
            files.extend(p.relative_to(repo_root).as_posix() for p in sorted(base.rglob("*.py")))
    gate = Path(__file__).resolve()
    aggregate = hashlib.sha256()
    count = 0
    for rel in sorted(set(files)):
        path = repo_root / rel
        aggregate.update(rel.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(sha256_file(path).encode("ascii"))
        aggregate.update(b"\0")
        count += 1
    try:
        gate_rel = gate.relative_to(repo_root).as_posix()
    except ValueError:
        gate_rel = str(gate)
    return {
        "files": count,
        "aggregate_sha256": aggregate.hexdigest(),
        "gate_file": gate_rel,
        "gate_sha256": sha256_file(gate),
    }


def git_head(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


# ---------------------------------------------------------------------------
# 套件执行


def isolated_env(repo_root: Path, sandbox_home: Path) -> dict:
    env = os.environ.copy()
    env.update(FAKE_ENV)
    env["EVOHARNESS_ROOT"] = str(repo_root)
    env["HOME"] = str(sandbox_home)
    env["USERPROFILE"] = str(sandbox_home)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    sandbox_home.mkdir(parents=True, exist_ok=True)
    for var in ("TMP", "TEMP", "TMPDIR"):
        value = os.environ.get(var)
        if value:
            sandbox_tmp = sandbox_home / Path(value).name
            sandbox_tmp.mkdir(parents=True, exist_ok=True)
            env[var] = str(sandbox_tmp)
    return env


def parse_junit(path: Path) -> dict:
    """解析 pytest junitxml：计数、node ID、skip 理由、收集错误。

    pytest 把收集错误记为 classname=pytest.errors 的 <error> 用例；
    普通错误/失败按 <failure>/<error> 子元素区分。收集错误绝不吞掉。
    """
    result = {
        "collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
        "collection_errors": [], "node_ids": [], "skips": [], "problems": [],
    }
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        result["problems"].append({"id": "<junit-parse>", "kind": "error",
                                   "message": f"junitxml 解析失败：{exc}"})
        return result
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    for suite in suites:
        result["collected"] += int(suite.get("tests", 0) or 0)
        result["failed"] += int(suite.get("failures", 0) or 0)
        result["errors"] += int(suite.get("errors", 0) or 0)
        result["skipped"] += int(suite.get("skipped", 0) or 0)
        for case in suite.iter("testcase"):
            classname = case.get("classname", "")
            name = case.get("name", "")
            node_id = f"{classname}::{name}" if classname else name
            result["node_ids"].append(node_id)
            error = case.find("error")
            failure = case.find("failure")
            skipped = case.find("skipped")
            if skipped is not None:
                result["skips"].append({
                    "nodeid": node_id,
                    "reason": (skipped.get("message") or skipped.get("type") or "").strip(),
                })
                continue
            if error is not None:
                message = (error.get("message") or "") + (error.text or "")
                # pytest 9 junit：收集错误的 classname 为空、name 是模块路径，
                # error message 以 "collection failure" 开头；旧格式为 pytest.errors。
                is_collection = (
                    classname == "pytest.errors"
                    or name.endswith(".py")
                    or "collection failure" in message.lower()
                )
                if is_collection:
                    result["collection_errors"].append({
                        "id": name, "kind": "collection-error",
                        "message": message.strip()[:400],
                    })
                else:
                    result["problems"].append({"id": node_id, "kind": "error",
                                               "message": message.strip()[:400]})
                continue
            if failure is not None:
                message = (failure.get("message") or "") + (failure.text or "")
                result["problems"].append({"id": node_id, "kind": "failure",
                                           "message": message.strip()[:400]})
    result["passed"] = max(
        result["collected"] - result["failed"] - result["errors"] - result["skipped"], 0
    )
    return result


def run_suite(
    label: str,
    args: list[str],
    cwd: Path,
    env: dict,
    timeout: int,
    workdir: Path,
) -> dict:
    junit = workdir / f"junit-{label}.xml"
    junit.unlink(missing_ok=True)
    cmd = [sys.executable, "-X", "utf8", "-m", "pytest", *args, "-q",
           "--continue-on-collection-errors", f"--junitxml={junit}"]
    started = _dt.datetime.now()
    timed_out = False
    stdout_tail = ""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        exit_code: int | str = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        exit_code = "timeout"
        timed_out = True
        out = exc.stdout or b""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        output = out
    seconds = (_dt.datetime.now() - started).total_seconds()
    log_path = workdir / f"{label}.log"
    log_path.write_text(output, encoding="utf-8")
    stdout_tail = "\n".join(output.splitlines()[-12:])
    parsed = parse_junit(junit) if junit.is_file() else {
        "collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
        "collection_errors": [], "node_ids": [], "skips": [],
        "problems": [{"id": "<junit-missing>", "kind": "error",
                      "message": "junitxml 未生成（套件可能超时或崩溃）"}],
    }
    return {
        "suite": label,
        "cwd": str(cwd),
        "command": cmd,
        "exit_code": exit_code,
        "timeout": timed_out,
        "seconds": round(seconds, 1),
        "collected": parsed["collected"],
        "passed": parsed["passed"],
        "failed": parsed["failed"],
        "errors": parsed["errors"],
        "skipped": parsed["skipped"],
        "collection_errors": parsed["collection_errors"],
        "node_ids": parsed["node_ids"],
        "skips": parsed["skips"],
        "problems": parsed["problems"],
        "stdout_tail": stdout_tail,
        "log_path": str(log_path),
        "junit_path": str(junit),
    }


def suite_green(report: dict) -> bool:
    return (
        report["exit_code"] == 0
        and report["failed"] == 0
        and report["errors"] == 0
        and not report["collection_errors"]
        and not report["timeout"]
    )


# ---------------------------------------------------------------------------
# job: product


def job_product(repo_root: Path, timeout: int, workdir: Path) -> dict:
    sandbox_home = workdir / "home"
    env = isolated_env(repo_root, sandbox_home)

    suites: list[dict] = []

    # ① 根套件：剔除冻结证据类文件（它们进 evidence job，两个 job 合计全覆盖）
    ignore_args: list[str] = []
    excluded, included = [], []
    for rel in EVIDENCE_DEPENDENT_TESTS:
        if (repo_root / rel).is_file():
            ignore_args += ["--ignore", rel]
            excluded.append(rel)
        else:
            included.append(rel)
    if (repo_root / "tests").is_dir():
        suites.append(run_suite(
            "core", ["tests", *ignore_args], repo_root, env, timeout, workdir))
    else:
        suites.append({"suite": "core", "status": "not_applicable",
                       "reason": "tests/ 目录不存在"})
    record = {
        "job": "product",
        "description": "产品回归（离线隔离：临时 HOME + 假凭据 + 写保护快照）",
        "excluded_to_evidence_job": excluded,
        "evidence_files_absent_in_this_tree": included,
        "suites": suites,
    }
    # ② 飞书套件：独立 cwd，避免同名 tests/app 包污染（LOCAL-AUDIT §4.4）
    feishu_dir = repo_root / "feishu-daily-agent"
    if (feishu_dir / "tests").is_dir():
        record["suites"].append(run_suite(
            "feishu", ["tests"], feishu_dir, env, timeout, workdir))
    else:
        record["suites"].append({
            "suite": "feishu", "status": "not_applicable",
            "reason": "feishu-daily-agent/tests/ 不存在（本树无飞书子项目）",
        })
    # ③ integration 套件：导入增量（de/import-8bc392d）才有
    if (repo_root / "integration" / "tests").is_dir():
        record["suites"].append(run_suite(
            "integration", ["integration/tests"], repo_root, env, timeout, workdir))
    else:
        record["suites"].append({
            "suite": "integration", "status": "not_applicable",
            "reason": "integration/tests/ 不存在（位于导入分支 de/import-8bc392d）",
        })
    runnable = [s for s in record["suites"] if "status" not in s]
    record["all_green"] = bool(runnable) and all(suite_green(s) for s in runnable)
    record["exit_code"] = EXIT_OK if record["all_green"] else EXIT_TEST_FAILURE
    record["status"] = "passed" if record["all_green"] else "failed"
    return record


# ---------------------------------------------------------------------------
# job: evidence


def classify_problems(problems: list[dict]) -> dict[str, list]:
    classification: dict[str, list] = {"missing_evidence": [], "regression": []}
    for problem in problems:
        message = (problem.get("message") or "").lower()
        if any(marker in message for marker in MISSING_EVIDENCE_MARKERS):
            classification["missing_evidence"].append(problem["id"])
        else:
            classification["regression"].append(problem["id"])
    return classification


def job_evidence(repo_root: Path, timeout: int, workdir: Path) -> dict:
    missing = [rel for rel in EVIDENCE_PROBE_PATHS if not (repo_root / rel).is_file()]
    all_missing = len(missing) == len(EVIDENCE_PROBE_PATHS)
    record = {
        "job": "evidence",
        "description": "冻结证据验收（依赖 eval/runs 实验产物与冻结协议）",
        "probe_paths": EVIDENCE_PROBE_PATHS,
        "missing_artifacts": missing,
    }
    targets = [rel for rel in EVIDENCE_DEPENDENT_TESTS if (repo_root / rel).is_file()]
    if all_missing and not targets:
        # 证据包整体缺失：明确非通过，不执行、不伪造（不吞掉也不谎报）。
        record.update({
            "status": "missing_evidence",
            "exit_code": EXIT_MISSING_PREREQ,
            "reason": (
                "冻结证据包缺失（clean checkout 不含未跟踪 eval/runs 产物）。"
                "需要校验过的证据包后重跑本 job；这不是产品回归，但也不能计作通过。"
            ),
            "suites": [],
        })
        return record
    sandbox_home = workdir / "home-evidence"
    env = isolated_env(repo_root, sandbox_home)
    if not targets:
        record.update(status="missing_evidence", exit_code=EXIT_MISSING_PREREQ,
                      reason="没有可执行的冻结证据验收测试", suites=[])
        return record
    suites = [run_suite("evidence-core", targets, repo_root, env, timeout, workdir)]
    classification = classify_problems(suites[0]["problems"])
    record.update({
        "suites": suites,
        "classification": classification,
        "executed_files": targets,
    })
    if suites[0]["collection_errors"] or classification["regression"]:
        record["status"] = "failed"
        record["exit_code"] = EXIT_TEST_FAILURE
    elif all_missing or classification["missing_evidence"] or not suite_green(suites[0]):
        record["status"] = "missing_evidence"
        record["exit_code"] = EXIT_MISSING_PREREQ
    else:
        record["status"] = "passed"
        record["exit_code"] = EXIT_OK
    return record


# ---------------------------------------------------------------------------
# job: live


def job_live(repo_root: Path) -> dict:
    """真实试点 job：离线门禁中永远 not_run_live（明确非通过）。

    只检查可离线核实的前提，不读取也不输出任何凭据内容。
    """
    env_file = repo_root / "feishu-daily-agent" / ".env"
    prerequisites = [
        {
            "name": "真实凭据配置（feishu-daily-agent/.env）",
            "ok": env_file.is_file(),
            "detail": "文件存在" if env_file.is_file() else "文件缺失",
        },
        {
            "name": "试点授权与人工验收协议",
            "ok": False,
            "detail": "需要试点负责人授权记录与人工验收流程（离线不可自动判定）",
        },
        {
            "name": "真实飞书租户与发送身份",
            "ok": False,
            "detail": "需要真实 app 凭据、OAuth 用户授权与真实消息回执（离线不可得）",
        },
    ]
    return {
        "job": "live",
        "description": "真实试点（live）：本门禁不做离线伪造，明确非通过",
        "status": "not_run_live",
        "exit_code": EXIT_MISSING_PREREQ,
        "prerequisites": prerequisites,
        "reason": (
            "live 试点验收需要真实凭据、授权与人工协议；离线门禁将其记为"
            "not_run_live（非通过），绝不以 mock 结果冒充实机验收。"
        ),
        "suites": [],
    }


# ---------------------------------------------------------------------------
# 主流程


def build_report(repo_root: Path, selected: list[str], jobs: list[dict],
                 protect_changes: list[dict], protect_roots: list[Path]) -> dict:
    codes = [job.get("exit_code", EXIT_USAGE) for job in jobs]
    breach = bool(protect_changes)
    if EXIT_USAGE in codes:
        overall = EXIT_USAGE
    elif breach:
        overall = EXIT_ISOLATION_BREACH
    elif EXIT_TEST_FAILURE in codes:
        overall = EXIT_TEST_FAILURE
    elif EXIT_MISSING_PREREQ in codes:
        overall = EXIT_MISSING_PREREQ
    else:
        overall = EXIT_OK
    lines = [f"job {job['job']}: {job['status']} (exit_code={job.get('exit_code')})"
             for job in jobs]
    if breach:
        lines.append(f"隔离破坏：{len(protect_changes)} 个受保护文件变更（见 write_guard.changed）")
    return {
        "gate_version": GATE_VERSION,
        "generated_at": now_iso(),
        "repo_root": str(repo_root),
        "git_head": git_head(repo_root),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "selected_jobs": selected,
        "jobs": {job["job"]: job for job in jobs},
        "write_guard": {
            "protected_roots": [str(p) for p in protect_roots],
            "protected_globs": PROTECTED_GLOBS,
            "breach": breach,
            "changed": protect_changes,
        },
        "overall": {
            "exit_code": overall,
            "meaning": EXIT_CODE_MEANING[overall],
            "summary": "；".join(lines),
        },
    }


def print_summary(report: dict) -> None:
    print("=" * 72)
    print("EvoHarness 数字员工统一测试门禁（DE-01）")
    print(f"repo: {report['repo_root']}  git_head: {report['git_head']}")
    print(f"python {report['python']} @ {report['platform']}")
    for name, job in report["jobs"].items():
        print(f"[{name}] status={job['status']} exit_code={job.get('exit_code')}")
        for suite in job.get("suites", []):
            if "status" in suite and suite.get("status") == "not_applicable":
                print(f"    - {suite['suite']}: not_applicable（{suite['reason']}）")
                continue
            print(
                f"    - {suite['suite']}: exit={suite['exit_code']} "
                f"collected={suite['collected']} passed={suite['passed']} "
                f"failed={suite['failed']} errors={suite['errors']} "
                f"skipped={suite['skipped']}"
            )
            for problem in suite.get("collection_errors", []):
                print(f"      收集错误（不吞掉）: {problem['id']} :: {problem['message'][:120]}")
        for change in report["write_guard"]["changed"]:
            print(f"    隔离破坏: {change['change']} {change['root']}/{change['path']}")
    print(f"overall: exit_code={report['overall']['exit_code']}"
          f"（{report['overall']['meaning']}）")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="EvoHarness 数字员工统一测试门禁")
    parser.add_argument("--jobs", default="product,evidence,live",
                        help="逗号分隔：product,evidence,live")
    parser.add_argument("--repo-root", default=str(REPO))
    parser.add_argument("--protect-root", action="append", default=[],
                        help="额外写保护根（可重复，如被测副本对应的原仓库）")
    parser.add_argument("--output-dir", default=None,
                        help="报告 gate-report.json 与各套件日志落盘目录")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="每个套件子进程超时秒数")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    if not repo_root.is_dir():
        print(f"用法错误：repo-root 不存在：{repo_root}", file=sys.stderr)
        return EXIT_USAGE
    selected = [job.strip() for job in args.jobs.split(",") if job.strip()]
    unknown = [job for job in selected if job not in {"product", "evidence", "live"}]
    if not selected or unknown or args.timeout <= 0:
        print(f"用法错误：未知 job：{unknown}", file=sys.stderr)
        return EXIT_USAGE

    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    workdir = output_dir if output_dir else Path(tempfile.mkdtemp(prefix="evoh-gate-"))
    protect_roots = [repo_root] + [Path(p).resolve() for p in args.protect_root]

    before = snapshot_protected(protect_roots)
    jobs: list[dict] = []
    if "product" in selected:
        jobs.append(job_product(repo_root, args.timeout, workdir))
    if "evidence" in selected:
        jobs.append(job_evidence(repo_root, args.timeout, workdir))
    if "live" in selected:
        jobs.append(job_live(repo_root))
    after = snapshot_protected(protect_roots)
    changes = diff_snapshots(before, after)

    report = build_report(repo_root, selected, jobs, changes, protect_roots)
    report["source_digest"] = source_digest(repo_root)
    if output_dir:
        (output_dir / "gate-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已落盘：{output_dir / 'gate-report.json'}")
    print_summary(report)
    if not output_dir:
        shutil.rmtree(workdir, ignore_errors=True)
    return report["overall"]["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
