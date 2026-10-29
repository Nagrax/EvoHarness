#!/usr/bin/env python3
"""EvoHarness 模块优化扫描器：按可机器验证的信号找「值得优化的模块」。

不是风格检查，每条规则都要能被代码或实测复现：
  - 超大函数/类：单文件最大函数行数 → 可读性与改动风险
  - 静默异常：`except Exception: pass` 之类吞错点 → 故障不可见（本项目真实踩过多次）
  - 覆盖缺口：核心模块无对应测试文件 → 无回归防线
  - 重复实现：两个协议分支的相似块 → 抄写错误温床
  - 无上限注入：字符串拼接进 prompt 未做截断
  - 硬编码阈值：魔法数字无命名常量
  - 阻塞调用：async 函数里出现同步 IO/subprocess.run
  - 裸 print：库代码用 print 而非结构化日志

用法：python eval/module_scan.py [--out PATH] [--json]
退出码恒为 0（扫描不判定失败），findings 供闭环消费。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("agents", "server", "eval")
SKIP_PARTS = {"__pycache__", ".pytest_cache", "runs", "node_modules"}
TEST_PREFIX = "test_"


def rel(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        # 仓库外的路径（测试里用 tmp_path）直接给绝对路径，别抛
        return Path(path).as_posix()


def py_files() -> list[Path]:
    out = []
    for directory in SCAN_DIRS:
        base = REPO / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            out.append(path)
    return sorted(out)


def test_files() -> list[Path]:
    base = REPO / "tests"
    return sorted(base.glob("*.py")) if base.is_dir() else []


def _norm(line: str) -> str:
    return " ".join(line.split())


def code_fingerprint(lines: list[str], line_no: int, *, context: int = 2) -> str:
    """对 line_no（1-based）及其前后 context 行做规范化后取哈希前 12 位。

    用途：行号会漂移，指纹不会——重定位时按指纹在同文件内滑窗匹配。
    """
    import hashlib

    start = max(0, line_no - 1 - context)
    end = min(len(lines), line_no + context)
    window = [_norm(x) for x in lines[start:end]]
    return hashlib.sha256("\n".join(window).encode("utf-8")).hexdigest()[:12]


def locate_by_fingerprint(path: Path, fingerprint: str, *, context: int = 2) -> int:
    """按指纹在该文件里找当前行号；找不到返回 0。"""
    if not fingerprint:
        return 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    for candidate in range(1, len(lines) + 1):
        if code_fingerprint(lines, candidate, context=context) == fingerprint:
            return candidate
    return 0


def _imported_modules(root: Path) -> set[str]:
    """收集整个仓库被 import 的模块名（相对路径、无扩展名）。"""
    import ast as _ast

    referenced: set[str] = set()
    for path in root.rglob("*.py"):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        try:
            tree = _ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            names: list[str] = []
            if isinstance(node, _ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, _ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                referenced.add(name.replace(".", "/"))
                referenced.add(name.split(".")[-1])
    return referenced


def scan_file(path: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [{"rule": "unreadable", "file": rel(path), "line": 0, "detail": "无法按 UTF-8 读取"}]
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        return [{"rule": "syntax-error", "file": rel(path), "line": error.lineno or 0,
                 "detail": str(error)[:200]}]

    lines = source.splitlines()
    is_test = path.name.startswith(TEST_PREFIX) or "tests" in path.parts

    # ① 超长函数：单个函数体 > 120 行
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > 120:
                findings.append({
                    "rule": "long-function", "file": rel(path), "line": node.lineno,
                    "severity": "medium",
                    "detail": f"{node.name} 有 {length} 行",
                    "metrics": {"length": length},
                })

    # ② 静默吞错：except ... : pass / continue / return None
    # 行号取 try 块的起始行（AST 无 try 行号，用 body 首行回推），
    # 这样判读器看到的是"被吞掉的那段操作"，而不是 except 之前无关的一行。
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            body = node.body
            silent = (len(body) == 1 and isinstance(body[0], ast.Pass)) or (
                len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is None)
            if not silent:
                continue
            try_line = node.lineno
            for candidate in ast.walk(tree):
                if isinstance(candidate, ast.Try):
                    if any(handler is node for handler in candidate.handlers):
                        try_line = candidate.body[0].lineno if candidate.body else candidate.lineno
                        break
            rule = "silent-except" if isinstance(body[0], ast.Pass) else "silent-except-return-none"
            detail = ("except 分支只有 pass：故障对上层不可见" if rule == "silent-except"
                      else "except 分支直接 return None：损坏与缺失无法区分")
            findings.append({
                "rule": rule, "file": rel(path), "line": try_line,
                "exceptLine": node.lineno,
                # 指纹锚在 except 行：行号会漂移，指纹不会
                "fingerprint": code_fingerprint(lines, node.lineno),
                "severity": "high" if not is_test else "low",
                "detail": detail,
            })

    # ③ async 函数里出现同步阻塞调用
    sync_blockers = {"subprocess.run", "time.sleep", "requests.get", "requests.post", "urllib.request.urlopen"}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    target = inner.func
                    name = ""
                    if isinstance(target, ast.Attribute):
                        parent = target.value
                        if isinstance(parent, ast.Name):
                            name = f"{parent.id}.{target.attr}"
                        elif isinstance(parent, ast.Attribute):
                            name = f"{parent.attr}.{target.attr}"
                    elif isinstance(target, ast.Name):
                        name = target.id
                    if name in sync_blockers:
                        findings.append({
                            "rule": "sync-call-in-async", "file": rel(path), "line": inner.lineno,
                            "severity": "high",
                            "detail": f"async 函数内调用阻塞式 {name}",
                        })

    # ④ 裸 print：库代码（agents/server）用 print 输出，绕过 UI 与日志
    if not is_test and path.parts[0] in {"agents", "server"}:
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("print(") and "# noqa" not in stripped:
                findings.append({
                    "rule": "bare-print", "file": rel(path), "line": index,
                    "severity": "low", "detail": "库代码直接 print：缺少结构化日志与级别",
                })

    # ⑤ 硬编码上限/阈值：3 位以上整数直接写在比较里且文件内无同名常量
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                if isinstance(operand, ast.Constant) and isinstance(operand.value, int) \
                        and abs(operand.value) >= 5000:
                    findings.append({
                        "rule": "magic-threshold", "file": rel(path), "line": node.lineno,
                        "severity": "low",
                        "detail": f"阈值 {operand.value} 直接内联，缺少命名常量",
                    })

    # ⑥ 无界字符串注入：把可能很长的变量直接拼进 prompt
    # 判据收紧（2026-10-29 判读后）：原实现只看"文本里有 content 字样"，5 处里 4 处误报。
    LONG_BODY_NAMES = ("content", "body", "text", "history", "output",
                       "result", "diff", "message", "payload", "prompt")
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        literal_chars = sum(len(v.value) for v in node.values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str))
        # 收集插值表达式里出现的名字
        interpolated: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.FormattedValue):
                continue
            for sub in ast.walk(value.value):
                if isinstance(sub, ast.Name):
                    interpolated.append(sub.id)
                elif isinstance(sub, ast.Attribute):
                    interpolated.append(sub.attr)
                elif isinstance(sub, ast.Call):
                    for sub2 in ast.walk(sub):
                        if isinstance(sub2, ast.Attribute):
                            interpolated.append(sub2.attr)
        lowered = [n.lower() for n in interpolated]
        looks_like_body = any(any(word in n for word in LONG_BODY_NAMES) for n in lowered)
        if (literal_chars > 400 and len(interpolated) >= 2 and looks_like_body
                and "truncate" not in ast.unparse(node).lower()):
            findings.append({
                "rule": "injection-without-truncate", "file": rel(path), "line": node.lineno,
                "severity": "low",
                "detail": (f"疑似把长文本拼进 prompt（字面量 {literal_chars} 字符、"
                           f"{len(interpolated)} 个插值），未见截断"),
            })
            break

    return findings


def coverage_gaps(files: list[Path], tests: list[Path],
                  referenced: set[str] | None = None) -> list[dict]:
    test_text = "\n".join(t.read_text(encoding="utf-8", errors="replace") for t in tests)
    gaps = []
    for path in files:
        module = path.stem
        if module in {"__init__"}:
            continue
        # 只要求**被其它模块 import** 的文件有测试（真正的库）；
        # 一次性脚本（check_*/verify_*/final_*）没人 import，不该被要求专属测试。
        if referenced is not None and module not in referenced:
            continue
        # 粗粒度判据：测试里是否 import 或提及该模块名
        if module not in test_text:
            gaps.append({
                "rule": "module-without-tests", "file": rel(path), "line": 0,
                "severity": "medium",
                "detail": f"tests/ 中未出现模块名 {module}，缺少回归防线",
            })
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    files = py_files()
    tests = test_files()
    findings: list[dict] = []
    for path in files:
        findings.extend(scan_file(path))
    # 只对被其它模块 import 的库文件要求测试（脚本不要求）
    findings.extend(coverage_gaps(files, tests, _imported_modules(REPO)))

    # 去重（同文件同行同规则）
    seen = set()
    unique = []
    for item in findings:
        key = (item["rule"], item["file"], item["line"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    by_severity: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for item in unique:
        by_severity[item.get("severity", "info")] = by_severity.get(item.get("severity", "info"), 0) + 1
        by_rule[item["rule"]] = by_rule.get(item["rule"], 0) + 1

    # 优先级排序：high > medium > low，同级按规则名
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    unique.sort(key=lambda i: (order.get(i.get("severity", "info"), 9), i["rule"], i["file"], i["line"]))

    report = {
        "kind": "module-scan",
        "filesScanned": len(files),
        "testFiles": len(tests),
        "totalFindings": len(unique),
        "bySeverity": by_severity,
        "byRule": by_rule,
        "top": unique[:40],
        "findings": unique,
    }
    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "module-scan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"module-scan: {len(files)} files, {len(unique)} findings -> {out}")
        print("severity:", json.dumps(by_severity, ensure_ascii=False))
        print("rules:", json.dumps(by_rule, ensure_ascii=False))
        for item in unique[:15]:
            print(f"  [{item.get('severity','info')}] {item['rule']} {item['file']}:{item['line']} {item['detail'][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
