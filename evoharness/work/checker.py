"""Full pytest collection, JUnit and protected test sources in isolated checks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

from evoharness.work import integrity
from evoharness.work.tool_isolation import DockerShellRunner
from evoharness.workflows.code_fix import TestReport

MARKER = "EVOHARNESS_PYTEST_RECEIPT="
DEFAULT_CHECKER_IMAGE = "evoharness-checker:latest"


def parse_evidence(output, junit, exit_code):
    """Cross-check the controller's complete node inventory with actual JUnit."""
    try:
        records = [
            line[len(MARKER) :]
            for line in output.splitlines()
            if line.startswith(MARKER)
        ]
        if (
            len(records) != 1
            or not junit.is_file()
            or junit.stat().st_size > 20_000_000
        ):
            raise ValueError("checker_receipt_missing")
        receipt = json.loads(records[0])
        collected = receipt["collected"]
        reports = receipt["reports"]
        if (
            receipt["version"] != 1
            or receipt["exit_code"] != exit_code
            or not collected
            or len(collected) > integrity.TEST_NODE_LIMIT
            or len(set(collected)) != len(collected)
            or not all(isinstance(node, str) and "::" in node for node in collected)
        ):
            raise ValueError("checker_inventory_invalid")
        if any(
            item["nodeid"] not in collected
            or item["when"] not in {"setup", "call", "teardown"}
            or item["outcome"] not in {"passed", "failed", "skipped"}
            for item in reports
        ):
            raise ValueError("checker_events_invalid")
        called = {item["nodeid"] for item in reports if item["when"] == "call"}
        skipped = {item["nodeid"] for item in reports if item["outcome"] == "skipped"}
        failed = {item["nodeid"] for item in reports if item["outcome"] == "failed"}
        if set(collected) != called | skipped | failed:
            raise ValueError("checker_tests_not_executed")
        root = ET.parse(junit).getroot()
        cases = list(root.iter("testcase"))
        if len(cases) != len(collected):
            raise ValueError("checker_junit_inventory_mismatch")
        # JUnit's actual case names retain parametrized variants.
        actual_nodes = []
        for case in cases:
            file = case.get("file")
            if not file:
                raise ValueError("checker_junit_file_missing")
            prefix = file.replace("\\", "/").removesuffix(".py").replace("/", ".")
            classname = case.get("classname", "")
            classes = (
                classname[len(prefix) :].strip(".").replace(".", "::")
                if classname.startswith(prefix)
                else ""
            )
            actual_nodes.append(
                file.replace("\\", "/")
                + "::"
                + (classes + "::" if classes else "")
                + case.get("name", "")
            )
        if set(actual_nodes) != set(collected):
            raise ValueError("checker_junit_nodes_mismatch")
        junit_failed = sum(
            case.find("failure") is not None or case.find("error") is not None
            for case in cases
        )
        junit_skipped = sum(case.find("skipped") is not None for case in cases)
        if junit_failed != len(failed) or junit_skipped != len(skipped):
            raise ValueError("checker_junit_statistics_mismatch")
        return {
            "valid": True,
            "node_ids": collected,
            "skipped_node_ids": sorted(skipped),
            "failed_node_ids": sorted(failed),
            "ran": len(called),
            "junit_sha256": hashlib.sha256(junit.read_bytes()).hexdigest(),
        }
    except (ValueError, KeyError, TypeError, ET.ParseError, OSError) as exc:
        return {"valid": False, "error": str(exc)[:200]}


class TrustedPytestRunner:
    kind = "docker-isolated"

    def __init__(self, *, paths=(".",), shell_runner=None, timeout_seconds=600):
        if not paths or any(
            not isinstance(path, str)
            or path.startswith("-")
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            for path in paths
        ):
            raise ValueError("checker_paths_invalid")
        self.paths = tuple(paths)
        self.shell_runner = shell_runner or DockerShellRunner(
            image=os.environ.get("EVOHARNESS_CHECKER_IMAGE") or DEFAULT_CHECKER_IMAGE,
            timeout_seconds=timeout_seconds,
        )
        self.baseline = None
        self.support = {}

    def _run(self, workdir):
        workdir = Path(workdir)
        directory = workdir / ".agent-env"
        directory.mkdir(exist_ok=True)
        name = "pytest-" + uuid.uuid4().hex + ".xml"
        controller = Path(__file__).resolve().parents[2] / "ops/pytest_probe.py"
        outcome = self.shell_runner.run(
            workdir,
            ["python", "/inputs/0", "/workspace/.agent-env/" + name, *self.paths],
            readonly_mounts=(controller,),
            manifest_path=directory / "checker-isolation.json",
        )
        if outcome.waiting_environment:
            return TestReport(
                ok=False,
                output="waiting_environment: checker image unavailable",
                evidence={"valid": False, "error": "waiting_environment"},
            )
        evidence = parse_evidence(outcome.detail, directory / name, outcome.exit_code)
        return TestReport(
            ok=outcome.ok and evidence.get("valid", False),
            output=outcome.detail[:12000],
            exit_code=outcome.exit_code,
            timed_out=outcome.exit_code is None,
            ran=evidence.get("ran", 0),
            evidence=evidence,
        )

    def prepare(self, workdir):
        report = self._run(workdir)
        self.baseline = report.evidence
        self.support = integrity.test_support_digests(integrity.snapshot_texts(workdir))
        return report

    def run(self, workdir, phase):
        report = self._run(workdir)
        if (
            not report.evidence.get("valid")
            or not self.baseline
            or not self.baseline.get("valid")
        ):
            report.ok = False
            return report
        baseline = set(self.baseline["node_ids"])
        current = set(report.evidence["node_ids"])
        support = integrity.test_support_digests(integrity.snapshot_texts(workdir))
        changed = sorted(
            path for path, sha in self.support.items() if support.get(path) != sha
        )
        removed = sorted(baseline - current)
        skipped = sorted(
            set(report.evidence["skipped_node_ids"])
            - set(self.baseline["skipped_node_ids"])
        )
        report.evidence.update(
            baseline_nodes=sorted(baseline),
            removed_node_ids=removed,
            modified_node_ids=changed,
            unexpected_skips=skipped,
        )
        if removed or changed or skipped or report.ran < 1:
            report.ok = False
        report.output += "\n" + json.dumps(
            report.evidence, ensure_ascii=False, sort_keys=True
        )
        return report
