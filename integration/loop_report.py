"""闭环运行摘要：BACKLOG + 信号源 + 黄金回放 + git 近况 → 中文报告。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from integration.config import FEISHU_AGENT, REPO, venv_python
from integration.nudge import _item_age_days  # 积压尾部年龄与催办同口径（DE-21）

RUNS = REPO / "eval" / "runs"
BACKLOG = REPO / "eval" / "loop" / "BACKLOG.json"
FEISHU_EVAL = FEISHU_AGENT / "eval" / "runs" / "feishu-eval.json"
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def _latest_autorun() -> dict | None:
    files = sorted(RUNS.glob("loop-autorun-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except ValueError:
        return None


def _backlog_summary() -> dict:
    if not BACKLOG.is_file():
        return {"ok": False, "reason": "backlog-missing"}
    data = json.loads(BACKLOG.read_text(encoding="utf-8"))
    items = data.get("items") or []
    open_items = [i for i in items if i.get("status") not in ("done", "blocked")]
    blocked_items = [i for i in items if i.get("status") == "blocked"]
    open_items.sort(key=lambda i: SEV_ORDER.get(str(i.get("severity")), 9))
    by_sev: dict[str, int] = {}
    for item in open_items:
        by_sev[str(item.get("severity") or "?")] = by_sev.get(str(item.get("severity") or "?"), 0) + 1
    return {
        "total": len(items),
        "open": len(open_items),
        "openBySeverity": by_sev,
        "tailAgeDays": max((_item_age_days(i) for i in open_items), default=0.0),
        "blocked": len(blocked_items),
        "blockedItems": [
            {"id": i["id"], "title": i.get("title"),
             "reason": (i.get("detail") or {}).get("blocked_on", "")}
            for i in blocked_items[:4]
        ],
        "openItems": [
            {"id": i["id"], "severity": i.get("severity"), "title": i.get("title"),
             "source": i.get("source"),
             "theme": (i.get("detail") or {}).get("theme")}
            for i in open_items[:12]
        ],
        "rounds": len(data.get("rounds") or []),
    }


def _feishu_eval_metrics() -> dict:
    if not FEISHU_EVAL.is_file():
        return {"present": False}
    try:
        report = json.loads(FEISHU_EVAL.read_text(encoding="utf-8"))
        approved = (report.get("strategies") or {}).get("approved") or {}
        cases = report.get("cases")
        case_count = len(cases) if isinstance(cases, list) else cases
        return {
            "present": True,
            "cases": case_count,
            "autoPrecision": approved.get("autoPrecision"),
            "autoCoverage": approved.get("autoCoverage"),
            "draftRecall": approved.get("draftRecall"),
            "severeErrors": len(approved.get("severeErrors") or []),
        }
    except ValueError:
        return {"present": False, "invalid": True}


def _git_recent(n: int = 8) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "log", "--oneline", f"-{n}"],
            cwd=str(REPO), capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        return [line for line in (out.stdout or "").splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        return []


def compose() -> str:
    """生成中文闭环报告文本。"""
    backlog = _backlog_summary()
    autorun = _latest_autorun()
    fe = _feishu_eval_metrics()

    lines = ["📊 EvoHarness 闭环自迭代报告", f"🕐 {time.strftime('%Y-%m-%d %H:%M')}"]

    if autorun:
        ok = autorun.get("ok")
        lines.append("")
        lines.append(f"【最近一轮闭环】{'✅ 正常' if ok else '⚠️ 异常'} "
                     f"({autorun.get('at', '?')} UTC)")
        for step in autorun.get("steps") or []:
            mark = "✅" if step.get("exitCode") == 0 else "❌"
            lines.append(f"  {mark} {step.get('name')}: exit={step.get('exitCode')} "
                         f"({step.get('seconds')}s)")
        if autorun.get("failedSteps"):
            lines.append(f"  失败步骤：{autorun['failedSteps']}")
    else:
        lines.append("")
        lines.append("【最近一轮闭环】尚无运行记录（跑 integration/iterate.py --once）")

    lines.append("")
    lines.append(f"【迭代台账】总 {backlog.get('total', '?')} 项，"
                 f"待办 {backlog.get('open', '?')} 项，共 {backlog.get('rounds', '?')} 轮")
    if backlog.get("openBySeverity"):
        lines.append(f"  按严重度：{backlog['openBySeverity']}")
    # DE-21：积压尾部年龄可见（最老未闭合项挂了多久），空台账显示 0 天属正常。
    lines.append(f"  积压尾部年龄：{backlog.get('tailAgeDays', 0):.0f} 天")
    # L9047：按主题分组展示（无主题的归入「未分组」）
    open_items = backlog.get("openItems") or []
    by_theme: dict[str, list[dict]] = {}
    for item in open_items:
        by_theme.setdefault(str(item.get("theme") or "未分组"), []).append(item)
    for theme, theme_items in by_theme.items():
        lines.append(f"  ▸ 主题[{theme}]")
        for item in theme_items:
            lines.append(f"    • [{item['severity']}] {item['id']} {item['title']} "
                         f"(source={item['source']})")
    if not open_items:
        lines.append("  （当前无待办——台账已清空属正常状态）")
    for item in backlog.get("blockedItems") or []:
        lines.append(f"  ⛔ [blocked] {item['id']} {item['title'][:44]}"
                     + (f"（{item['reason'][:36]}）" if item.get("reason") else ""))

    lines.append("")
    if fe.get("present"):
        lines.append(f"【飞书黄金回放】{fe.get('cases')} 用例 | "
                     f"precision={fe.get('autoPrecision')} coverage={fe.get('autoCoverage')} "
                     f"draftRecall={fe.get('draftRecall')} severe={fe.get('severeErrors')}")
    else:
        lines.append("【飞书黄金回放】产物缺失（待跑 eval/replay.py --run）")

    commits = _git_recent()
    if commits:
        lines.append("")
        lines.append("【最近提交】")
        for c in commits[:5]:
            lines.append(f"  {c}")

    # L9046：飞书就绪度（仅静态检查，无网络）
    try:
        from integration.preflight import static_checks

        ready = static_checks()
        lines.append("")
        lines.append(f"【飞书就绪度】{'✅ 可连接' if ready['ready'] else '⏳ 未就绪'}"
                     f"（模式 {ready.get('mode')}）")
        if ready.get("missing"):
            lines.append(f"  缺项：{ready['missing']}")
    except Exception:  # noqa: BLE001 — 报告不因预检失败而失败
        pass

    # KPI 趋势（eval-workflow.v1 §4/§5，B 级证据）
    try:
        sys.path.insert(0, str(REPO / "integration"))
        from integration.eval.kpi_trend import trend_summary

        trend = trend_summary()
        if trend:
            lines.append("")
            lines.append("【KPI 趋势】")
            for source, info in trend.items():
                metrics = " ".join(f"{k}={v}" for k, v in (info.get("metrics") or {}).items())
                lines.append(f"  {source}（{info['records']} 轮记录，{info['at']}）：{metrics}")
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(compose())
