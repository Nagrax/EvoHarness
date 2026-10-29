"""module-without-tests 精度改进回归（2026-10-29）。

问题：原规则按文件名匹配，把 24 个文件都报"无测试"，其中绝大多数是
一次性验证脚本（check_*/verify_*/final_*），或已由**功能性测试**覆盖的模块
（memory_lifecycle_ab 的行为由 test_memory_filter_arm 等覆盖，但文件名不匹配）。

改进：只有**被其它模块 import** 的文件（真正的库）才要求专属测试。
实测 findings 93 → 70，该规则 24 → 1。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eval.module_scan import _imported_modules, coverage_gaps


def test_imported_modules_captures_known_imports():
    referenced = _imported_modules(REPO)
    assert "module_scan" in referenced or "eval/module_scan" in referenced, (
        "应能识别被 import 的模块（loop.py 里 import 了 module_scan 相关函数）")
    assert isinstance(referenced, set) and referenced


def test_standalone_script_not_required_to_have_tests(tmp_path):
    """没人 import 的脚本不该被要求专属测试。"""
    script = tmp_path / "check_thing.py"
    script.write_text('if __name__ == "__main__":\n    print("ok")\n', encoding="utf-8")
    tests = [tmp_path / "test_other.py"]
    tests[0].write_text("def test_x():\n    pass\n", encoding="utf-8")
    gaps = coverage_gaps([script], tests, referenced={"some_other_module"})
    assert not gaps, f"纯脚本不应被报缺测试：{gaps}"


def test_library_without_test_is_reported(tmp_path):
    """被 import 的库若测试里没提及，仍要报（断言没弱化）。"""
    lib = tmp_path / "real_lib.py"
    lib.write_text("def f():\n    return 1\n", encoding="utf-8")
    tests = [tmp_path / "test_other.py"]
    tests[0].write_text("def test_x():\n    pass\n", encoding="utf-8")
    gaps = coverage_gaps([lib], tests, referenced={"real_lib"})
    assert gaps and gaps[0]["rule"] == "module-without-tests"


def test_covered_library_not_reported(tmp_path):
    lib = tmp_path / "covered_lib.py"
    lib.write_text("def f():\n    return 1\n", encoding="utf-8")
    tests = [tmp_path / "test_covered.py"]
    tests[0].write_text("import covered_lib\n", encoding="utf-8")
    gaps = coverage_gaps([lib], tests, referenced={"covered_lib"})
    assert not gaps


def test_real_scan_gap_count_is_small():
    """真实仓库：该规则的命中数应已收敛（不再大面积误报）。"""
    import json

    path = REPO / "eval" / "runs" / "module-scan.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    gaps = [f for f in data["findings"] if f["rule"] == "module-without-tests"]
    assert len(gaps) <= 5, f"误报仍偏多：{len(gaps)} 个（预期收敛到个位数）"
