"""Require real Linux-container test execution where that backend is available.

Windows Server runners provide native Windows containers. Their native product
regressions still run; Linux isolation scenarios must pass in both Linux CI jobs.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

REQUIRED_TESTS = ('tests/test_agent_feedback_loop.py::test_conditional_execution_native_delegation', 'tests/test_agent_feedback_loop.py::test_conditional_execution_provider_port', 'tests/test_agent_feedback_loop.py::test_tool_results_reach_later_model_rounds', 'tests/test_organization_acceptance.py::test_organization_eval_cli_selected_scenario_and_markdown', 'tests/test_organization_acceptance.py::test_scenario1_synthetic_org_end_to_end_without_feishu', 'tests/test_tool_isolation.py::test_adapter_routes_checker_through_isolation_channel', 'tests/test_tool_isolation.py::test_checker_runs_through_isolated_channel', 'tests/test_tool_isolation.py::test_container_has_no_network', 'tests/test_tool_isolation.py::test_directory_escape_rejected_outside_never_created', 'tests/test_tool_isolation.py::test_readonly_input_mount_rejects_writes', 'tests/test_tool_isolation.py::test_workspace_writable_and_host_env_not_passed_through', 'tests/test_worker_durability.py::test_budget_total_240_recorded_once_not_twice', 'tests/test_worker_durability.py::test_crash_after_claim_commit_durable_then_takeover', 'tests/test_worker_durability.py::test_crash_after_external_call_done_before_receipt', 'tests/test_worker_durability.py::test_crash_after_external_call_started_reconciles_unknown', 'tests/test_worker_durability.py::test_crash_after_receipt_keeps_completed_and_reconciles_pending', 'tests/test_worker_durability.py::test_crash_after_reservation_commit_reused_not_duplicated', 'tests/test_worker_durability.py::test_crash_before_acceptance_keeps_waiting_acceptance_without_replay', 'tests/test_worker_durability.py::test_escrow_mutex_no_premature_rejection', 'tests/test_worker_end_to_end.py::test_lease_heartbeat_renews_across_controlled_clock', 'tests/test_worker_end_to_end.py::test_root_entry_end_to_end_worker_execution_and_done', 'tests/test_worker_end_to_end.py::test_tool_whitelist_and_write_guard_enforced', 'tests/test_worker_end_to_end.py::test_worker_budget_exhausted_waits_then_executes_after_topup', 'tests/test_worker_end_to_end.py::test_worker_crash_recovery_takeover', 'tests/test_worker_root_entry.py::test_worker_daemon_graceful_stop_processes_queue', 'tests/test_worker_root_entry.py::test_worker_daemon_processes_two_rounds_then_stops', 'tests/test_worker_root_entry.py::test_worker_once_subprocess_completes_queued_run')


def verify(junit: Path, required=REQUIRED_TESTS) -> list[str]:
    if not junit.is_file():
        return ["missing_junit"]
    root = ET.parse(junit).getroot()
    cases = {}
    for case in root.iter("testcase"):
        key = case.get("classname", "").replace(".", "/") + ".py::" + case.get("name", "")
        cases.setdefault(key, []).append(case)
    problems = []
    for node in required:
        matches = cases.get(node, [])
        if len(matches) != 1:
            problems.append("missing_or_duplicate:" + node)
        elif any(matches[0].find(tag) is not None for tag in ("failure", "error", "skipped")):
            problems.append("not_executed_successfully:" + node)
    return problems


def prepare() -> int:
    try:
        result = subprocess.run(["docker", "info", "--format", "{{.OSType}}"],
                                capture_output=True, text=True, timeout=30)
        linux = result.returncode == 0 and result.stdout.strip() == "linux"
    except (OSError, subprocess.SubprocessError):
        linux = False
    if not linux:
        print("Linux container backend unavailable; isolation is mandatory in Linux CI jobs.")
        return 0 if os.name == "nt" else 1
    return subprocess.run(["docker", "pull", "python:3.13-slim"], check=False).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "verify"])
    parser.add_argument("--junit", type=Path)
    args = parser.parse_args(argv)
    if args.action == "prepare":
        return prepare()
    if args.junit is None:
        parser.error("verify requires --junit")
    problems = verify(args.junit)
    print(json.dumps({"required": len(REQUIRED_TESTS), "problems": problems}))
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
