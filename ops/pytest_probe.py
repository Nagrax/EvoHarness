"""Read-only controller mounted by TrustedPytestRunner, outside the task bundle."""

import json
import sys
from pathlib import Path

import pytest


class Probe:
    def __init__(self):
        self.collected = []
        self.reports = []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        self.reports.append(
            {"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome}
        )


def main():
    report_path, *paths = sys.argv[1:]
    # Match `python -m pytest`: the read-only controller lives outside the
    # project, so its script directory must not replace the project import root.
    sys.path.insert(0, str(Path.cwd()))
    probe = Probe()
    code = pytest.main(
        [
            *paths,
            "-q",
            "--continue-on-collection-errors",
            "-o",
            "junit_family=xunit1",
            "--junitxml=" + report_path,
        ],
        plugins=[probe],
    )
    print(
        "\nEVOHARNESS_PYTEST_RECEIPT="
        + json.dumps(
            {
                "version": 1,
                "collected": probe.collected,
                "reports": probe.reports,
                "exit_code": int(code),
            },
            ensure_ascii=False,
        )
    )
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
