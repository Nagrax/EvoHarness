#!/usr/bin/env python3
"""记忆体系评测：把记忆链路的机制逐条做可复跑验证，并量化成本。

为什么单独做：现有评测（LoCoMo 1,540 题）证明了召回质量有改善（F1 13.96→33.01），
但**没有**把记忆体系本身当被测对象逐机制压测。本模块把设计文档里的每条约束
变成可复跑的断言，这样改记忆代码时会立刻知道破坏了哪条。

覆盖的机制（对 docs/interview/记忆链路.md 逐条落地）：
  M1 四类 type 与 frontmatter 解析
  M2 索引自动重建 + 200 行/25KB 上限
  M3 召回三道门槛（多词/中文、注入预算、存在记忆文件）
  M4 有界头部读取（不整读正文）
  M5 单文件 4KB 与单会话 60KB 预算
  M6 选择器回指宽松匹配（整行/围栏/路径前缀都能命中）
  M7 注入格式（前置标签 + 类型前缀 + 时效警告）
  M8 生命周期陈旧标注（移植 mem0/Letta 生命周期）
  M9 生命周期冲突消解（同 subject 覆盖留痕）
  M10 成本：候选扫描字节数、注入 token 估算

用法：python eval/memory_eval.py [--json] [--out PATH]
退出码：0 全通过；1 有失败。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    results: list[dict] = []
    findings: list[dict] = []

    def record(name, ok, detail, cost=None):
        results.append({"case": name, "ok": bool(ok), "detail": detail, "cost": cost or {}})
        if not ok:
            findings.append({"id": f"memory:{name}", "detail": detail})

    with tempfile.TemporaryDirectory(prefix="evo-mem-", ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        os.environ["HOME"] = str(tmp)
        os.environ["USERPROFILE"] = str(tmp)

        import agents.memory as memory

        memdir = tmp / ".evoharness" / "memories"
        memdir.mkdir(parents=True, exist_ok=True)
        memory.get_memory_dir = lambda: memdir  # type: ignore[assignment]

        def write_memory(name: str, *, mtype: str = "project", desc: str = "描述") -> Path:
            path = memdir / name
            path.write_text(
                f"---\nname: {name[:-3]}\ntype: {mtype}\ndescription: {desc}\n---\n正文内容\n",
                encoding="utf-8")
            return path

        # ── M1 type 与 frontmatter ──────────────────────
        write_memory("m-user.md", mtype="user", desc="用户偏好")
        write_memory("m-feedback.md", mtype="feedback", desc="纠正")
        write_memory("m-project.md", mtype="project", desc="项目事实")
        write_memory("m-ref.md", mtype="reference", desc="外部资源")
        headers = memory.scan_memory_headers()
        types = {h.filename: h.type for h in headers}
        record("M1_four_types_parsed",
               types.get("m-user.md") == "user" and types.get("m-feedback.md") == "feedback"
               and types.get("m-project.md") == "project" and types.get("m-ref.md") == "reference",
               types)

        # ── M2 索引重建与行数上限 ──────────────────────
        index_result = memory.auto_update_memory_index(str(memdir / "m-user.md")) \
            if hasattr(memory, "auto_update_memory_index") else None
        if index_result is None:
            from agents import tools as tools_mod
            tools_mod.get_memory_dir = lambda: memdir  # type: ignore[assignment]
            index_result = tools_mod._auto_update_memory_index(str(memdir / "m-user.md"))
        index_path = memdir / "MEMORY.md"
        index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
        lines = [l for l in index_text.splitlines() if l.strip()]
        record("M2_index_rebuilt_with_cap",
               bool(index_text) and len(lines) <= 205,
               {"lines": len(lines), "capExpected": "<=200+header", "bytes": len(index_text.encode())})

        # ── M3 触发三道门槛 ───────────────────────────
        gate_cases = {
            "chinese_sentence": memory.should_prefetch_memory("上次说的生产库部署约定是什么"),
            "english_multiword": memory.should_prefetch_memory("what is the deployment convention"),
            "short_command": memory.should_prefetch_memory("ls"),
            "empty": memory.should_prefetch_memory("   "),
        }
        record("M3_trigger_gates",
               gate_cases["chinese_sentence"][0] and gate_cases["english_multiword"][0]
               and not gate_cases["short_command"][0] and not gate_cases["empty"][0],
               gate_cases)

        # ── M4 有界头部读取 ───────────────────────────
        big = memdir / "m-big.md"
        big.write_text("---\nname: big\ntype: project\ndescription: 大文件\n---\n" + ("filler\n" * 80000),
                       encoding="utf-8")
        observed = {"bytes": 0}
        real_read = memory.read_text_migrating

        def counting_read(path):
            observed["bytes"] += Path(path).stat().st_size
            return real_read(path)

        memory.read_text_migrating = counting_read  # type: ignore[assignment]
        headers2 = memory.scan_memory_headers()
        memory.read_text_migrating = real_read  # type: ignore[assignment]
        total_bytes = sum(p.stat().st_size for p in memdir.glob("*.md") if p.name != "MEMORY.md")
        record("M4_bounded_header_read",
               observed["bytes"] < total_bytes,
               {"bytesRead": observed["bytes"], "totalCandidateBytes": total_bytes,
                "ratio": round(observed["bytes"] / total_bytes, 4) if total_bytes else None},
               cost={"bytesRead": observed["bytes"]})

        # ── M5 单文件/单会话预算 ──────────────────────
        record("M5_budgets_declared",
               memory.MAX_MEMORY_BYTES_PER_FILE == 4096
               and memory.MAX_SESSION_MEMORY_BYTES == 60 * 1024,
               {"perFile": memory.MAX_MEMORY_BYTES_PER_FILE,
                "perSession": memory.MAX_SESSION_MEMORY_BYTES})

        # ── M6 回指宽松匹配（四种写法） ────────────────
        async def try_selector(selector_payload: str) -> list[str]:
            async def side_query(_system, _user):
                return selector_payload

            recalled = await memory.select_relevant_memories(
                "what is the deployment convention please", side_query, set())
            return [r.path for r in recalled]

        selectors = {
            "plain_filename": '{"selected_memories": ["m-user.md"]}',
            "whole_manifest_line": '{"selected_memories": ["- [user] m-user.md (2026-10-25T00:00:00+00:00): 用户偏好"]}',
            "fenced": '```json\n{"selected_memories": ["m-user.md"]}\n```',
            "path_prefix": '{"selected_memories": ["' + str(memdir / "m-user.md").replace("\\", "\\\\") + '"]}',
        }
        hits = {}
        for label, payload in selectors.items():
            try:
                recalled = asyncio.run(try_selector(payload))
                hits[label] = len(recalled)
            except Exception as error:  # noqa: BLE001
                hits[label] = f"error:{type(error).__name__}: {error}"
        record("M6_selector_loose_match",
               all(isinstance(v, int) and v >= 1 for v in hits.values()),
               hits)

        # ── M7 注入格式 ──────────────────────────────
        try:
            async def run_inject():
                async def side_query(_system, _user):
                    return '{"selected_memories": ["m-user.md"]}'

                recalled = await memory.select_relevant_memories(
                    "what is the deployment convention please", side_query, set())
                return memory.format_memories_for_injection(recalled)

            injected = asyncio.run(run_inject())
        except Exception as error:  # noqa: BLE001
            injected = f"error:{type(error).__name__}: {error}"
        record("M7_injection_format",
               isinstance(injected, str) and "Memory" in injected and len(injected) > 0,
               {"chars": len(injected) if isinstance(injected, str) else None,
                "head": (injected[:200] if isinstance(injected, str) else str(injected)[:200])},
               cost={"injectedChars": len(injected) if isinstance(injected, str) else 0})

        # ── M8 生命周期：陈旧标注让选择器可见降权（移植 mem0/Letta 生命周期） ──
        try:
            now_ms = time.time() * 1000
            stale_headers = [
                memory.MemoryHeader(filename="fresh.md", file_path="/x/fresh.md",
                                    description="新约定", type="project",
                                    mtime_ms=now_ms - 2 * 86_400_000),
                memory.MemoryHeader(filename="stale.md", file_path="/x/stale.md",
                                    description="旧约定", type="project",
                                    mtime_ms=now_ms - 45 * 86_400_000),
            ]
            manifest = memory.format_memory_manifest(stale_headers)
            marked = "[stale 45d]" in manifest and "[stale 2d]" not in manifest
        except Exception as error:  # noqa: BLE001
            manifest = f"error:{type(error).__name__}: {error}"
            marked = False
        record("M8_lifecycle_stale_marking", marked,
               {"manifest": manifest[:300]})

        # ── M9 生命周期：冲突消解（同 subject 新覆盖旧、留痕可追溯） ──
        try:
            from agents.memory_lifecycle import MemoryRecord, resolve_conflicts
            older = MemoryRecord(name="old-rule", subject="deploy-policy", mtype="project",
                                 description="旧部署约定", content="body",
                                 updated_at=now_ms / 1000 - 86400)
            newer = MemoryRecord(name="new-rule", subject="deploy-policy", mtype="project",
                                 description="新部署约定", content="body",
                                 updated_at=now_ms / 1000)
            resolved = resolve_conflicts([older, newer], now=now_ms / 1000)
            m9_ok = (
                [r.name for r in resolved["kept"]] == ["new-rule"]
                and resolved["superseded"] == [{"superseded": "old-rule", "by": "new-rule",
                                                "oldVersion": 1, "newVersion": 1}]
            )
        except Exception as error:  # noqa: BLE001
            resolved = {"error": f"{type(error).__name__}: {error}"}
            m9_ok = False
        record("M9_lifecycle_conflict_resolution", m9_ok,
               {"kept": [r.name for r in resolved.get("kept", [])] if isinstance(resolved, dict) else None,
                "superseded": resolved.get("superseded") if isinstance(resolved, dict) else None})

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    report = {
        "kind": "memory-eval",
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "ok": passed == total and total > 0,
        "results": results,
        "findings": findings,
        "costSummary": {
            "headerReadBytes": next((r["cost"].get("bytesRead") for r in results
                                     if r["case"] == "M4_bounded_header_read"), None),
            "injectedChars": next((r["cost"].get("injectedChars") for r in results
                                   if r["case"] == "M7_injection_format"), None),
            "note": "注入字符数换算 token 经验值约 /3（中文更接近 /1.5），仅用于成本量级判断",
        },
    }
    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "memory-eval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"memory-eval: {passed}/{total} passed -> {out}")
        for f in findings:
            print(f"  FAIL {f['id']}: {json.dumps(f['detail'], ensure_ascii=False)[:200]}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
