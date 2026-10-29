"""闭环修复回归：JSONL 读取的坏行与整体失败必须可见。

背景（模块扫描 L0056，判读真缺陷）：
`_read_jsonl_file` 原实现两层静默——坏行 pass、整文件异常 pass 后返回空表。
前端只能看到"没有数据"，无法区分"真没有"与"文件坏了/没权限"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server.app as app


def test_good_lines_are_read(tmp_path):
    path = tmp_path / "ok.jsonl"
    path.write_text(
        json.dumps({"a": 1}) + "\n" + json.dumps({"b": 2}) + "\n", encoding="utf-8")
    rows = app._read_jsonl_file(path)
    assert rows == [{"a": 1}, {"b": 2}]
    assert app.last_jsonl_errors == []


def test_bad_line_is_reported_not_silently_dropped(tmp_path):
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        json.dumps({"a": 1}) + "\n{broken json\n" + json.dumps({"b": 2}) + "\n",
        encoding="utf-8")
    rows = app._read_jsonl_file(path)
    assert rows == [{"a": 1}, {"b": 2}], "好行必须照常返回"
    assert app.last_jsonl_errors, "坏行必须留下诊断"
    assert app.last_jsonl_errors[0]["kind"] == "bad-line"
    assert app.last_jsonl_errors[0]["line"] == 2


def test_missing_file_reports_read_failure(tmp_path):
    rows = app._read_jsonl_file(tmp_path / "absent.jsonl")
    assert rows == []
    assert app.last_jsonl_errors
    assert app.last_jsonl_errors[0]["kind"] == "read-failed"


def test_undecodable_file_reports_read_failure(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(b"\xff\xfe\x00\x00")
    rows = app._read_jsonl_file(path)
    assert rows == []
    assert app.last_jsonl_errors
    assert app.last_jsonl_errors[0]["kind"] == "read-failed"
