#!/usr/bin/env python3
"""桥接层确定性评测器（eval-workflow.v1，docs/EVAL-WORKFLOW-DESIGN.md §2）。

角色区分（设计 §1）：pytest 保证"开发时刻不回归"；本评测器产出**运行时刻的
量化 artifact**（precision/分组通过率/severe 计数）——进趋势（kpi_trend）、进
信号源（eval/collab_signal）、进闭环报告。两者共享 golden_cases.json 口径。

分层漏斗对齐 funnel.v1：
- intake/nudge/ask/audit = 串行层（CTX/BOUND/EXEC/CNT 的桥接层投影）
- red = 并行安全层：失败即 safety_failed，overall=0（独立上浮，不被短路）
- faultSet = DE-24 故障集回放 artifact 接入（存在才计门；缺失如实标注 available=False，不冒充已测）

纯函数 + 零网络 + 确定性输出（A 级证据：可复跑 + sha256）。
用法：
  python integration/eval/collab_eval.py --run    # 评测并写 artifact
  python integration/eval/collab_eval.py --check  # 只看结论 JSON
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

INTEGRATION = Path(__file__).resolve().parents[1]
REPO = INTEGRATION.parent
sys.path.insert(0, str(REPO))

from integration.config import ensure_dirs  # noqa: E402

GOLDEN = INTEGRATION / "eval" / "golden_cases.json"
GATES = INTEGRATION / "eval" / "gates.json"
RUNS = INTEGRATION / "data"
ARTIFACT = RUNS / "collab-eval.json"
VERSION = "collab-eval.v1"


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _gates() -> dict:
    return json.loads(GATES.read_text(encoding="utf-8"))


# ------------------------------------------------------------- 组评测器

def eval_intake(cases: list[dict]) -> dict:
    sys.path.insert(0, str(INTEGRATION))
    import task_intake

    results = []
    for case in cases:
        expect = case["expect"]
        text, theme = task_intake._parse_theme(case["text"])
        ok = True
        reasons: list[str] = []
        if "theme" in expect and theme != expect["theme"]:
            ok = False
            reasons.append(f"theme={theme!r} expect={expect['theme']!r}")
        if "textAfter" in expect:
            stripped = text
            for prefix in task_intake.TASK_PREFIXES:
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix):].strip()
                    break
            if stripped != expect["textAfter"]:
                ok = False
                reasons.append(f"textAfter={stripped!r}")
        if "titleMax" in expect:
            title = text if len(text) <= 60 else text[:57] + "…"
            if len(title) > expect["titleMax"]:
                ok = False
                reasons.append(f"titleLen={len(title)}")
        results.append({"id": case["id"], "pass": ok, "reasons": reasons})
    return _group("intake", results)


def eval_nudge(cases: list[dict]) -> dict:
    sys.path.insert(0, str(INTEGRATION))
    import nudge

    results = []
    for case in cases:
        expect = case["expect"]
        text = nudge.compose(case["state"])
        ok = True
        reasons: list[str] = []
        if expect["pushed"] and text is None:
            ok = False
            reasons.append("expected push, got None")
        if not expect["pushed"] and text is not None:
            ok = False
            reasons.append(f"expected silence, got push ({len(text)} chars)")
        if ok and expect.get("contains"):
            for token in expect["contains"]:
                if token not in (text or ""):
                    ok = False
                    reasons.append(f"missing token {token!r}")
        results.append({"id": case["id"], "pass": ok, "reasons": reasons})
    return _group("nudge", results)


def eval_ask(cases: list[dict]) -> dict:
    from app.services import agent_bridge  # feishu-daily-agent 侧

    results = []
    for case in cases:
        expect = case["expect"]
        stripped = agent_bridge.strip_ask_prefix(case["text"])
        ok = stripped == expect["stripped"]
        reasons = [] if ok else [f"stripped={stripped!r} expect={expect['stripped']!r}"]
        results.append({"id": case["id"], "pass": ok, "reasons": reasons})
    return _group("ask", results)


def eval_audit(cases: list[dict]) -> dict:
    """审计链写入/读回一致性（写临时文件，不碰真实 audit.jsonl）。"""
    from integration import audit

    results = []
    for case in cases:
        ok, reasons = True, []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                saved_audit, saved_data = audit.AUDIT, audit.DATA
                try:
                    audit.AUDIT = Path(tmp) / "audit.jsonl"
                    audit.DATA = Path(tmp)
                    audit.record(case["action"], actor="collab-eval", result="probe")
                    entry = json.loads(
                        audit.AUDIT.read_text(encoding="utf-8").strip().splitlines()[-1])
                finally:
                    audit.AUDIT, audit.DATA = saved_audit, saved_data
            for field in case["expect"]["fields"]:
                if field not in entry:
                    ok = False
                    reasons.append(f"missing field {field!r}")
        except Exception as exc:  # noqa: BLE001 — 审计链坏 = CNT 层失败，必须可见
            ok, reasons = False, [f"{type(exc).__name__}: {exc}"[:120]]
        results.append({"id": case["id"], "pass": ok, "reasons": reasons})
    return _group("audit", results)


def eval_red(cases: list[dict]) -> dict:
    """RED 并行安全层：注入样例不得让解析器崩溃/泄漏主题；问路由不吞注入前缀语义。"""
    sys.path.insert(0, str(INTEGRATION))
    import task_intake
    from app.services import agent_bridge

    results = []
    for case in cases:
        expect = case["expect"]
        ok, reasons = True, []
        try:
            text, theme = task_intake._parse_theme(case["text"])
            if expect.get("noThemeLeak") and theme is not None:
                ok = False
                reasons.append(f"injection text produced theme {theme!r}")
        except Exception as exc:  # noqa: BLE001
            ok, reasons = False, [f"parse crashed: {type(exc).__name__}"]
        if "strippedStartsWith" in expect:
            stripped = agent_bridge.strip_ask_prefix(case["text"])
            if not (stripped or "").startswith(expect["strippedStartsWith"]):
                ok = False
                reasons.append(f"stripped={stripped!r}")
        results.append({"id": case["id"], "pass": ok, "reasons": reasons})
    return _group("red", results)


def _group(name: str, results: list[dict]) -> dict:
    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    return {
        "group": name,
        "cases": total,
        "passed": passed,
        "passRate": round(passed / total, 4) if total else 1.0,
        "caseResults": results,
    }


def eval_fault_set_section(artifact_path: Path | None = None) -> dict:
    """DE-24 故障集接入（scope/201/401/OAuth/投影/唤醒/真实执行/未知回执）。

    读取 eval/digital_employee_eval.py 产出的故障集回放 artifact；artifact 存在且
    有违规 → 计入 below_gate（faultViolationsAllow 冻结为 0）。artifact 缺失时
    available=False：故障覆盖未执行，如实标注、不计门，也不冒充已覆盖。
    """
    path = artifact_path or (REPO / "eval" / "runs" / "digital-employee-faultset.json")
    if not path.is_file():
        return {"available": False, "cases": 0, "violations": 0, "ok": True,
                "path": str(path),
                "note": "故障集 artifact 不存在：本轮故障覆盖未执行，不计入本次门（不冒充已测）"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cases = int(data.get("cases") or 0)
        violations = int(data.get("violations") or 0)
        allow = int(_gates()["gates"].get("faultViolationsAllow", 0))
        ok = bool(data.get("ok")) and violations <= allow
        return {"available": True, "cases": cases, "violations": violations,
                "allow": allow, "ok": ok, "kinds": data.get("kinds") or {},
                "faultSetSha256": data.get("faultSetSha256"),
                "portId": data.get("portId"), "path": str(path)}
    except (ValueError, OSError, KeyError) as exc:
        return {"available": True, "cases": 0, "violations": 0, "ok": False,
                "path": str(path), "error": f"{type(exc).__name__}: {exc}"[:120],
                "note": "故障集 artifact 不可读：按失败处理，不静默"}


# ------------------------------------------------------------- 主流程

def run() -> dict:
    ensure_dirs()
    golden = _golden()
    gates = _gates()

    sys.path.insert(0, str(REPO / "feishu-daily-agent"))
    group_fns = {
        "intake": eval_intake,
        "nudge": eval_nudge,
        "ask": eval_ask,
        "audit": eval_audit,
        "red": eval_red,
    }
    groups: dict[str, dict] = {}
    for name, fn in group_fns.items():
        try:
            groups[name] = fn(golden["groups"][name])
        except Exception as exc:  # noqa: BLE001 — 组崩溃必须可见，不得静默
            groups[name] = {"group": name, "cases": 0, "passed": 0, "passRate": 0.0,
                            "error": f"{type(exc).__name__}: {exc}"[:160]}

    cases_total = sum(g["cases"] for g in groups.values())
    passed_total = sum(g["passed"] for g in groups.values())
    safety_failed = groups["red"]["passRate"] < gates["gates"]["groupPassRateMin"]
    overall_rate = round(passed_total / cases_total, 4) if cases_total else 0.0
    severe = sum(len(r["reasons"]) for g in groups.values()
                 for r in g.get("caseResults", []) if not r["pass"] and g["group"] == "red")
    fault_set = eval_fault_set_section()
    below_gate = (
        overall_rate < gates["gates"]["overallMin"]
        or any(g["passRate"] < gates["gates"]["groupPassRateMin"] for g in groups.values())
        or severe > gates["gates"]["severeAllow"]
        or (fault_set.get("available") and not fault_set.get("ok"))
    )
    failing = [
        {"group": g["group"], **case}
        for g in groups.values() for case in g.get("caseResults", []) if not case["pass"]
    ]
    report = {
        "version": VERSION,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "goldenSha256": hashlib.sha256(GOLDEN.read_bytes()).hexdigest(),
        "gatesSha256": hashlib.sha256(GATES.read_bytes()).hexdigest(),
        "ok": not below_gate,
        "safetyFailed": safety_failed,
        "cases": cases_total,
        "passed": passed_total,
        "overallRate": overall_rate,
        "severe": severe,
        "groups": groups,
        "failingCases": failing,
        "gates": gates["gates"],
        "faultSet": fault_set,
        "note": "确定性纯函数评测（A 级证据）；pytest 负责开发期回归，本 artifact 负责运行时量化。",
    }
    ARTIFACT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="评测并写 artifact")
    parser.add_argument("--check", action="store_true", help="只输出结论")
    args = parser.parse_args(argv)

    if args.run or not args.check:
        report = run()
        if not args.check:
            print(json.dumps({"ok": report["ok"],
                              "overallRate": report["overallRate"],
                              "path": str(ARTIFACT)}, ensure_ascii=False))
            return 0 if report["ok"] else 1
    report = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    print(json.dumps({"ok": report["ok"], "overallRate": report["overallRate"]},
                     ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
