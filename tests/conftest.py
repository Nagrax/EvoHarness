"""根套件共享 fixture。

背景：test_mcp_client_hardening::test_close_releases_stdio_pipes 在自己的
warnings 捕获窗口中断言无 ResourceWarning；套件里任何先行测试泄漏的
sqlite 连接若恰在该窗口内被 GC 回收，会让该测试偶发失败（跨测试污染，
单跑必过）。对声明了 warnings 断言的少数敏感测试文件，在测试开始前强制
回收一轮垃圾，把先行测试的泄漏在窗口打开前清掉——不修改任何测试断言，
泄漏的测试仍会在自身阶段产生可观察的 ResourceWarning。
"""

from __future__ import annotations

import gc

import pytest

# 断言“窗口内无 ResourceWarning”的敏感测试模块（basename，不带 .py）。
_WARNING_SENSITIVE_MODULES = frozenset(
    {
        "test_mcp_client_hardening",
        "test_review_fixes",
        "test_code_fix_workflow",
    }
)


@pytest.fixture(autouse=True)
def _gc_before_warnings_sensitive_tests(request):
    if request.node.fspath.purebasename.replace(".py", "") in _WARNING_SENSITIVE_MODULES:
        gc.collect()
    yield


def pytest_collection_modifyitems(config, items):
    # These scenarios execute real commands in Linux containers. The same nodes
    # are mandatory (not skippable) in Linux CI, verified from their JUnit report.
    from eval.isolation_coverage import REQUIRED_TESTS
    from evoharness.work.tool_isolation import DockerShellRunner
    required = set(REQUIRED_TESTS)
    if DockerShellRunner().available():
        return
    marker = pytest.mark.skip(reason="requires Linux Docker backend; mandatory in Linux isolation CI")
    for item in items:
        if item.nodeid in required:
            item.add_marker(marker)
