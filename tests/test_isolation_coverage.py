from pathlib import Path
import xml.etree.ElementTree as ET

from eval.isolation_coverage import verify

NODE = "tests/example.py::test_example"


def report(tmp_path, *states):
    root = ET.Element("testsuite")
    for state in states:
        case = ET.SubElement(root, "testcase", classname="tests.example", name="test_example")
        if state:
            ET.SubElement(case, state)
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path)
    return path


def test_coverage_requires_actual_successful_execution(tmp_path):
    assert verify(report(tmp_path, ""), [NODE]) == []
    assert verify(report(tmp_path, "skipped"), [NODE]) == ["not_executed_successfully:" + NODE]
    assert verify(report(tmp_path, "failure"), [NODE]) == ["not_executed_successfully:" + NODE]
    assert verify(report(tmp_path, "error"), [NODE]) == ["not_executed_successfully:" + NODE]


def test_coverage_rejects_missing_and_duplicate_cases(tmp_path):
    assert verify(tmp_path / "absent.xml", [NODE]) == ["missing_junit"]
    assert verify(report(tmp_path), [NODE]) == ["missing_or_duplicate:" + NODE]
    assert verify(report(tmp_path, "", ""), [NODE]) == ["missing_or_duplicate:" + NODE]
