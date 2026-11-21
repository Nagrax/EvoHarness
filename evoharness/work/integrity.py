"""被测 bundle 摘要：把检查回执绑定到实际参与检查的源码集合。

回执只绑定 test_report 文件内容还不够——报告未变但源码在检查后被改写时，
旧检查结论不能再证明新 bundle。本摘要按「工作区源码文本快照」计算（排除
执行器自留的产物/证据/沙箱/检查点目录），检查时写入回执，验收时可从
Run 工作区重算比对。

不导入 workflows.code_fix（它会反向导入执行链，造成循环依赖）。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from evoharness.ids import sha256_json, sha256_text

EXCLUDED_PARTS = frozenset({"artifacts", "evidence", ".agent-env", "checkpoints",
                            "__pycache__", ".git", ".venv", "node_modules"})

# 测试集清点上限：防止异常工作区把回执撑爆；超出部分不参与差集。
TEST_NODE_LIMIT = 5000


def snapshot_texts(root: str | Path) -> dict[str, str]:
    base = Path(root).resolve()
    rows: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if EXCLUDED_PARTS & set(relative.parts):
            continue
        try:
            rows[relative.as_posix()] = path.read_text(encoding="utf-8",
                                                       errors="replace")
        except OSError:
            continue
    return rows


def workspace_bundle_digest(workdir: str | Path) -> str:
    return sha256_json(snapshot_texts(workdir))


def _is_test_file(relative: str) -> bool:
    name = Path(relative).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _test_nodes_in_source(relative: str, text: str) -> list[tuple[str, ast.AST]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    nodes: list[tuple[str, ast.AST]] = []

    def class_nodes(node: ast.ClassDef, prefix: str) -> None:
        for child in node.body:
            if (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name.startswith("test")):
                nodes.append((f"{relative}::{prefix}{child.name}", child))
            elif isinstance(child, ast.ClassDef) and child.name.startswith("Test"):
                class_nodes(child, prefix + child.name + "::")

    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test")):
            nodes.append((f"{relative}::{node.name}", node))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            class_nodes(node, node.name + "::")
    return nodes


def _walk_test_nodes(snapshot: dict[str, str]) -> list[tuple[str, ast.AST]]:
    nodes: list[tuple[str, ast.AST]] = []
    for relative, text in sorted(snapshot.items()):
        if not _is_test_file(relative):
            continue
        nodes.extend(_test_nodes_in_source(relative, text))
        if len(nodes) >= TEST_NODE_LIMIT:
            return nodes[:TEST_NODE_LIMIT]
    return nodes


def test_node_inventory(snapshot: dict[str, str]) -> list[str]:
    """静态清点测试 node（模块级 test_* 与 Test 类方法），不执行代码。

    参数化变体（``[param]``）与 fixture 生成的测试不计入静态基线；差集判定
    只覆盖「整条测试被删/改名」这一可确定性证明的部分，不冒充全部弱化检测。
    """
    return [node_id for node_id, _node in _walk_test_nodes(snapshot)]


def test_node_digests(snapshot: dict[str, str]) -> dict[str, str]:
    """每个测试 node 的 AST 摘要：格式/注释不变，断言与函数体改写即变化。

    旧测试被改写（弱化断言、换实现）→ 摘要变化；新增测试不影响既有 node。
    """
    return {node_id: sha256_text(ast.dump(node))
            for node_id, node in _walk_test_nodes(snapshot)}


def test_support_digests(snapshot: dict[str,str]) -> dict[str,str]:
    """Protect fixtures, parameter constants, helpers and pytest selection too."""
    protected = {}
    for relative,text in sorted(snapshot.items()):
        path = Path(relative)
        if not (_is_test_file(relative) or 'tests' in path.parts or path.name in
                {'conftest.py','pytest.ini','pyproject.toml','setup.cfg','tox.ini'}):
            continue
        if path.suffix == '.py':
            try:
                text = ast.dump(ast.parse(text))
            except (SyntaxError,ValueError):
                pass
        protected['test-source:'+relative] = sha256_text(text)
    return protected


def run_workspace(checkpoint_json: str) -> Path | None:
    """从 Run 检查点取冻结工作区；不存在/不可解析返回 None（不伪造）。"""
    try:
        frozen = json.loads(checkpoint_json or "{}").get("frozen") or {}
    except ValueError:
        return None
    raw = str(frozen.get("workspace") or "")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


# ── EVO-03（DE-13）：参数化变体保护接入真实 pytest 全量 node 集 ────────────────


def base_node_id(node_id: str) -> str:
    """参数化变体 node 归一到基础 node：剥掉末段 ``[param]`` 后缀。

    ``tests/test_a.py::test_x[p1]`` → ``tests/test_a.py::test_x``；
    静态清点基线只有基础 node，与真实收集结果比对前先归一，避免把
    变体误判成整条删除。
    """
    head, sep, last = str(node_id).rpartition("::")
    if sep and last.endswith("]") and "[" in last:
        return head + sep + last[: last.rindex("[")]
    return str(node_id)


def collect_test_nodes(root: str | Path, *, timeout_seconds: float = 120.0) -> dict:
    """真实 pytest --collect-only（实际收集器）的测试 node 全集（含参数化变体）。

    strict 验收用它对冻结工作区重新收集，与回执记录的基线 node 集做差集：
    整条测试被删、参数化变体缩减据此明确拒绝（静态 AST 清点看不见变体，
    pytest_generate_tests / parametrize 常量被改时函数摘要不变）。

    - exit 0 → 收集成功；exit 5 → 工作区确无测试（空集是合法结论）；
    - 其余退出码（收集错误）/超时/子进程异常 → ``ok=False``，调用方
      fail-closed 拒绝，不静默放行；
    - ``-p no:cacheprovider`` 不写缓存，保持冻结工作区字节不变。
    调用前 bundle 摘要已核验一致：此处执行的收集代码与隔离检查器在检查期
    执行过的是同一份内容，不引入新的宿主执行面。
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", "--continue-on-collection-errors", "."],
            cwd=str(Path(root).resolve()), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=float(timeout_seconds),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                 "PYTHONIOENCODING": "utf-8"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "nodes": [], "error": f"collect_error:{exc}"[:200]}
    if proc.returncode not in {0, 5}:
        return {"ok": False, "nodes": [],
                "error": f"collect_failed_exit_{proc.returncode}"[:200]}
    nodes = [line.strip() for line in (proc.stdout or "").splitlines() if "::" in line]
    return {"ok": True, "nodes": nodes[:TEST_NODE_LIMIT], "error": ""}
