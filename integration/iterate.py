"""闭环自迭代编排器：飞书信号 → 台账 → 报告 → 飞书。

用法：
  python integration/iterate.py --once          # 跑一轮完整闭环并投递报告
  python integration/iterate.py --daemon        # 常驻：每 4h 一轮 + 每天 08:35 报告
  python integration/iterate.py --status        # 只看当前闭环状态（不执行）
  python integration/iterate.py --intake-only   # 只做飞书任务入账
  python integration/iterate.py --ask "问题"    # 经 EvoHarness Agent 一次性问答

一轮闭环 = 飞书任务入账 → 黄金回放评测 → loop_autorun(collect+triage)
         → 摘要报告 → 投递飞书（无凭证时本地落盘）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import os
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "eval"))

from integration.config import LOOP_LOCK, REPORTS, REPO as _REPO, venv_python  # noqa: E402
from integration.feishu_notify import send_text, notification_mode  # noqa: E402
from integration.loop_report import compose  # noqa: E402
from integration.audit import record as audit_record  # noqa: E402
import nudge  # noqa: E402
import task_intake  # noqa: E402
from integration.round_lease import RoundLease, process_marker

_lease_state = threading.local()

# DE-13：collab 评测产出的冻结门禁结论（release_gate）作为轮次显式结果。
COLLAB_EVAL_ARTIFACT = REPO / "integration" / "eval" / "runs" / "collab-eval.json"


def log(msg: str) -> None:
    print(f"[iterate {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _run(args: list[str], *, timeout: int = 900) -> dict:
    _guard_lease()
    started = time.monotonic()
    import tempfile
    from evoharness.governance.usage_sidecar import (USAGE_SIDECAR_ENV,
                                                    read_usage)
    descriptor, sidecar_name = tempfile.mkstemp(prefix="evo-usage-", suffix=".json")
    os.close(descriptor)
    sidecar = Path(sidecar_name)
    env = os.environ.copy()
    env[USAGE_SIDECAR_ENV] = str(sidecar)
    try:
        proc = subprocess.run(
            [venv_python(), *args], cwd=str(_REPO),
            capture_output=True, timeout=timeout, check=False, env=env,
        )
        usage = read_usage(sidecar) if sidecar.is_file() else None
    finally:
        sidecar.unlink(missing_ok=True)
    _guard_lease()
    return {
        "cmd": " ".join(args),
        "exitCode": proc.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "usage": usage,
        "stdoutTail": (proc.stdout or b"").decode("utf-8", "replace")[-400:],
        "stderrTail": (proc.stderr or b"").decode("utf-8", "replace")[-300:],
    }


def _acquire_lock() -> bool:
    if getattr(_lease_state, "lease", None) is not None:
        return False
    # Preserve a still-running legacy PID lock; never delete another worker's file.
    if LOOP_LOCK.is_file():
        try:
            pid = int(LOOP_LOCK.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            log("invalid legacy lock; inspect before continuing")
            return False
        if process_marker(pid) is not None:
            log(f"legacy iterate is running (pid {pid})")
            return False
    lease = RoundLease.acquire(LOOP_LOCK.with_suffix(".lease.sqlite3"))
    if lease is None:
        return False
    _lease_state.lease = lease
    return True


def _release_lock() -> None:
    lease = getattr(_lease_state, "lease", None)
    _lease_state.lease = None
    if lease is not None:
        lease.release()


def _guard_lease() -> None:
    lease = getattr(_lease_state, "lease", None)
    if lease is not None and not lease.current():
        raise RuntimeError("round_lease_lost")


def _health(lease: RoundLease, state: str, reason: str = "") -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"health-{lease.generation}-{lease.token}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"generation": lease.generation, "state": state,
                               "reason": reason, "at": time.time()}, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _run_exclusive(callback):
    if not _acquire_lock():
        return None
    lease = _lease_state.lease
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(lease.seconds / 3):
            try:
                if not lease.renew():
                    return
            except Exception:
                return
    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        _health(lease, "running")
        result = callback()
        _guard_lease()
        _health(lease, "completed")
        return result
    except BaseException as exc:
        _health(lease, "failed", type(exc).__name__)
        raise
    finally:
        stop.set()
        worker.join(timeout=2)
        _release_lock()


def _notify(text: str, *, title: str, mode: str) -> dict:
    _guard_lease()
    if mode == "disabled":
        result = {"ok": True, "mode": "disabled", "status": "disabled", "delivered": False}
    else:
        result = send_text(text, title=title, mode=mode)
    audit_record("notification-intent", actor="integration-controller", channel="control",
                 result=result.get("status", result.get("mode", "unknown")),
                 title=title, delivered=bool(result.get("delivered")),
                 outbox_id=result.get("outboxId"))
    return result


def _release_gate_step() -> dict:
    """把 collab 评测的冻结门禁结论显式带进轮次结果。

    缺产物/不可解析 → ok=None（不冒充通过）；有产物按 ``ok`` 与
    ``gatesSha256`` 原样呈现；Worker/验收侧可用同一 JSON 作为
    ``gate_report`` 证据（acceptance 会复核 passed + 门禁摘要）。
    """
    try:
        payload = json.loads(COLLAB_EVAL_ARTIFACT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"name": "releaseGate", "ok": None, "reason": "artifact_missing"}
    if not isinstance(payload, dict):
        return {"name": "releaseGate", "ok": None, "reason": "artifact_invalid"}
    return {"name": "releaseGate", "ok": payload.get("ok") is True,
            "overallRate": payload.get("overallRate"),
            "gatesSha256": str(payload.get("gatesSha256") or "")[:64]}


def _round_budget_reserve() -> "dict | None":
    """入口层预算网关（R03）：显式开启时给整轮闭环的付费步骤做原子预留。

    EVOHARNESS_LOOP_BUDGET=1 且 EVOHARNESS_DATABASE_URL 可达时生效；预留
    耗尽/未配置即整轮跳过（fail-closed，不产生未记账的付费副作用）。
    其余情况如实返回 disabled——遗留付费路径（飞书 LLM/embedding/judge/
    反思/抽取的逐次调用接线）在 AUDIT-FIXES/R03-20261114.md 剩余接线清单
    单列，不假装全接。
    """
    if os.environ.get("EVOHARNESS_LOOP_BUDGET", "").strip().lower() \
            not in {"1", "true", "on", "yes"}:
        return None
    db_url = os.environ.get("EVOHARNESS_DATABASE_URL", "").strip()
    if not db_url:
        return {"disabled": True, "error": "database_not_configured"}
    try:
        from evoharness.application import Application
        from evoharness.governance import budget, budget_gateway
        app = Application.open(db_url)
    except Exception as exc:  # noqa: BLE001 — 入口层如实降级，不阻塞既有闭环
        return {"disabled": True, "error": f"{type(exc).__name__}: {exc}"[:120]}
    gateway = budget_gateway.BudgetGateway(
        app.session_factory, default_category="integration.loop",
        default_tokens=int(os.environ.get("EVOHARNESS_LOOP_BUDGET_TOKENS", "2000")))
    try:
        record = gateway.reserve(
            org_id=os.environ.get("EVOHARNESS_LOOP_BUDGET_ORG", "").strip())
        return {"gateway": gateway, "record": record, "app": app}
    except budget.BudgetError as exc:
        app.close()
        return {"blocked": True, "error": f"{exc.code}:{exc.scope}"}
    except Exception as exc:  # noqa: BLE001
        app.close()
        return {"blocked": True, "error": f"{type(exc).__name__}: {exc}"[:120]}


def _round_budget_settle(handle: "dict | None", *, ok: bool = True,
                         used_tokens: int | None = None) -> None:
    """整轮结算：有子进程真实用量则按实际结算，否则按预留全额保守归集。

    子进程经 ``EVOHARNESS_USAGE_SIDECAR`` 上报 usage（DE-15 路径 7）；
    ``None`` = 没有任何上报（旧步骤/未接线），不是 0，usage_status=unknown。
    """
    if not handle or handle.get("disabled") or handle.get("blocked"):
        return
    try:
        if used_tokens is None:
            handle["gateway"].settle(handle["record"],
                                     used_tokens=handle["record"].reserved_tokens,
                                     ok=ok, usage_status="unknown",
                                     outcome="ok" if ok else "failed")
        else:
            handle["gateway"].settle(handle["record"],
                                     used_tokens=max(0, int(used_tokens)),
                                     ok=ok, usage_status="recorded",
                                     outcome="ok" if ok else "failed")
    finally:
        try:
            handle["app"].close()
        except Exception:  # noqa: BLE001
            pass


def _intake_journal_factory():
    """DE-05：显式开启时任务入账切产品库事务日志；默认 None（旧来源 journal）。"""
    if os.environ.get("EVOHARNESS_INTAKE_PRODUCT_JOURNAL", "").strip().lower()             not in {"1", "true", "on", "yes"}:
        return None
    db_url = os.environ.get("EVOHARNESS_DATABASE_URL", "").strip()
    if not db_url:
        return None
    try:
        from evoharness.application import Application
        from evoharness.work.intake_journal import ProductIntakeJournal
        app = Application.open(db_url)
    except Exception as exc:  # noqa: BLE001 — 入口层如实降级，不阻塞既有闭环
        log(f"product intake journal disabled: {type(exc).__name__}: {exc}")
        return None

    def factory(scope_key):
        return ProductIntakeJournal(app.session_factory, allow_write=True)

    factory.app = app
    return factory


def run_round(*, skip_notify: bool = False, notify_mode: str | None = None) -> dict:
    """All triggers share the operational lease and notification policy."""
    if getattr(_lease_state, "lease", None) is None:
        result = _run_exclusive(lambda: run_round(skip_notify=skip_notify, notify_mode=notify_mode))
        return result if result is not None else {"steps": [], "reason": "round_locked", "notify": {"mode": "disabled", "delivered": False}}
    _guard_lease()
    mode = "disabled" if skip_notify else notification_mode(notify_mode)
    steps: list[dict] = []

    _guard_lease()
    round_budget = _round_budget_reserve()
    if round_budget is not None and round_budget.get("blocked"):
        steps.append({"name": "budgetGateway", "blocked": True,
                      "error": round_budget.get("error")})
        return {"steps": steps, "reason": "budget_exhausted",
                "notify": {"mode": "disabled", "delivered": False}}
    if round_budget is not None and round_budget.get("disabled"):
        steps.append({"name": "budgetGateway", "disabled": True,
                      "error": round_budget.get("error")})

    try:
        intake_factory = _intake_journal_factory()
        try:
            if intake_factory is None:
                intake = task_intake.run()
            else:
                intake = task_intake.run(journal_factory=intake_factory)
        finally:
            if intake_factory is not None:
                intake_factory.app.engine.dispose()
        steps.append({"name": "feishuIntake", "added": intake["added"],
                      "items": intake["items"][:5]})

        # 黄金回放（离线、秒级）：飞书自动回复质量信号源
        replay = _run(["feishu-daily-agent/eval/replay.py", "--run"], timeout=300)
        steps.append({"name": "goldenReplay", **{k: replay[k] for k in ("exitCode", "seconds")},
                      "usage": replay.get("usage")})

        # 桥接层评测（eval-workflow.v1）：intake/nudge/ask/audit/red → collab-eval.json
        collab = _run(["integration/eval/collab_eval.py", "--run"], timeout=300)
        steps.append({"name": "collabEval", **{k: collab[k] for k in ("exitCode", "seconds")},
                      "usage": collab.get("usage")})
        steps.append(_release_gate_step())

        # KPI 记账 + 回归检测（趋势 B 级证据）
        trend = _run(["integration/eval/kpi_trend.py", "--record"], timeout=120)
        steps.append({"name": "kpiTrend", **{k: trend[k] for k in ("exitCode", "seconds")},
                      "usage": trend.get("usage")})

        # 闭环一轮（自检+collect+triage）
        autorun = _run(["eval/loop_autorun.py", "--once"], timeout=1800)
        steps.append({"name": "loopAutorun", **{k: autorun[k] for k in ("exitCode", "seconds")},
                      "usage": autorun.get("usage")})
    finally:
        reported = [step.get("usage") for step in steps
                    if isinstance(step, dict) and step.get("usage") is not None]
        if round_budget is not None and not round_budget.get("disabled")                 and not round_budget.get("blocked"):
            if reported:
                used = sum(int(item.get("tokens", 0)) for item in reported)
                calls = sum(int(item.get("calls", 0)) for item in reported)
                steps.append({"name": "budgetUsage", "tokens": used,
                              "calls": calls, "source": "subprocess_sidecar"})
            else:
                steps.append({"name": "budgetUsage",
                              "source": "conservative_reserved"})
        _round_budget_settle(
            round_budget,
            used_tokens=(sum(int(item.get("tokens", 0)) for item in reported)
                         if reported else None))

    # L9039 PM 主动推进：卡点主动亮出来（推飞书/本地落盘）
    nudge_result = {"ok": True, "mode": "none"}
    try:
        nudge_payload = nudge.collect()
        nudge_source_ok = bool((nudge_payload.get("sourceHealth") or {}).get("healthy", True))
        nudge_text = nudge.compose(nudge_payload)
        if nudge_text:
            nudge_result = _notify(nudge_text, title="pm-nudge", mode=mode)
            steps.append({"name": "pmNudge", "pushed": bool(nudge_result.get("delivered")), "mode": nudge_result.get("mode"), "status": nudge_result.get("status"), "sourceHealthOk": nudge_source_ok})
        else:
            steps.append({"name": "pmNudge", "pushed": False, "sourceHealthOk": nudge_source_ok})
    except Exception as exc:  # noqa: BLE001 — 催办失败不影响报告投递
        steps.append({"name": "pmNudge", "error": f"{type(exc).__name__}: {exc}"[:120]})

    _guard_lease()
    report_text = compose()
    REPORTS.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS / f"report-{time.strftime('%Y%m%d-%H%M%S')}.md"
    report_path.write_text(report_text, encoding="utf-8")

    notify = _notify(report_text, title=f"loop-{time.strftime('%Y%m%d')}", mode=mode)
    log(f"round done: replay={replay['exitCode']} autorun={autorun['exitCode']} "
        f"notify={notify.get('mode')}")
    return {"steps": steps, "report": str(report_path), "notify": notify,
            "reportText": report_text}


def _latest_health() -> dict:
    records, invalid = [], 0
    for path in REPORTS.glob("health-*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            row["generation"] = int(row.get("generation", 0))
            records.append(row)
        except (OSError, ValueError, TypeError, AttributeError):
            invalid += 1
    return max(records, key=lambda row: row["generation"],
               default={"state": "unreadable" if invalid else "not_run"})


def status() -> dict:
    sys.path.insert(0, str(REPO / "eval"))
    import loop as loop_mod
    state = loop_mod.load()
    items = state.get("items") or []
    open_items = [i for i in items if i.get("status") not in ("done", "blocked")]
    blocked_items = [i for i in items if i.get("status") == "blocked"]
    return {
        "backlogTotal": len(items),
        "backlogOpen": len(open_items),
        "blocked": len(blocked_items),
        "rounds": len(state.get("rounds") or []),
        "schedulerHealth": _latest_health(),
        "openHigh": sum(1 for i in open_items if i.get("severity") == "high"),
        "next": [i["id"] + " " + i["title"][:40] for i in open_items[:3]],
    }


def _push_nudge(*, skip_notify: bool = False, notify_mode: str | None = None) -> None:
    mode = "disabled" if skip_notify else notification_mode(notify_mode)
    text = nudge.compose(nudge.collect())
    if text:
        result = _notify(text, title="pm-nudge", mode=mode)
        log(f"daily nudge: {result.get('status', result.get('mode'))}")


def build_scheduler(interval_hours: float, daily_hour: int, daily_minute: int, *, skip_notify: bool = False, notify_mode: str | None = None):
    from apscheduler.schedulers.blocking import BlockingScheduler
    if interval_hours <= 0:
        raise ValueError("interval_must_be_positive")
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    kwargs = {"skip_notify": skip_notify, "notify_mode": notify_mode}
    def round_job():
        return _run_exclusive(lambda: run_round(**kwargs))
    def nudge_job():
        return _run_exclusive(lambda: _push_nudge(**kwargs))
    sched.add_job(round_job, "interval", hours=interval_hours, id="loop-round", max_instances=1, coalesce=True)
    sched.add_job(round_job, "cron", hour=daily_hour, minute=daily_minute, id="daily-report", max_instances=1, coalesce=True)
    sched.add_job(nudge_job, "cron", hour=10, minute=0, id="daily-nudge", max_instances=1, coalesce=True)
    return sched


def daemon(interval_hours: float, daily_hour: int, daily_minute: int, *, skip_notify: bool = False, notify_mode: str | None = None) -> int:
    if interval_hours <= 0:
        raise ValueError("interval_must_be_positive")
    try:
        sched = build_scheduler(interval_hours, daily_hour, daily_minute,
                                skip_notify=skip_notify, notify_mode=notify_mode)
    except ImportError:
        log("APScheduler unavailable; using sequential offline-compatible loop")
        try:
            while True:
                try:
                    _run_exclusive(lambda: run_round(skip_notify=skip_notify, notify_mode=notify_mode))
                except Exception as exc:
                    log(f"round failed: {type(exc).__name__}")
                time.sleep(interval_hours * 3600)
        except KeyboardInterrupt:
            return 0
    log(f"daemon started: interval={interval_hours}h notification={'disabled' if skip_notify else notify_mode or 'auto'}")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log("daemon stopped")
    finally:
        if sched.running:
            sched.shutdown(wait=True)
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="EvoHarness 飞书闭环自迭代")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=float, default=4.0)
    parser.add_argument("--daily-hour", type=int, default=8)
    parser.add_argument("--daily-minute", type=int, default=35)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--intake-only", action="store_true")
    parser.add_argument("--skip-notify", action="store_true")
    parser.add_argument("--notify-mode", choices=["auto", "disabled", "dryrun", "live"], default=None)
    parser.add_argument("--ask", nargs="+")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if args.ask:
        from integration.agent_ask import ask
        result = ask(" ".join(args.ask))
        if result.get("ok"):
            print(result["answer"])
            return 0
        print(f"问答失败：{result.get('error')}", file=sys.stderr)
        return 1
    if args.intake_only:
        intake_factory = _intake_journal_factory()
        try:
            result = (task_intake.run() if intake_factory is None
                      else task_intake.run(journal_factory=intake_factory))
        finally:
            if intake_factory is not None:
                intake_factory.app.engine.dispose()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.daemon:
        return daemon(args.interval, args.daily_hour, args.daily_minute,
                      skip_notify=args.skip_notify, notify_mode=args.notify_mode)
    if args.once:
        result = run_round(skip_notify=args.skip_notify, notify_mode=args.notify_mode)
        if result.get("reason") == "round_locked":
            return 4
        print(json.dumps({k: v for k, v in result.items() if k != "reportText"}, ensure_ascii=False, indent=2))
        failed = [step for step in result["steps"] if step.get("exitCode") not in (0, None)]
        return 2 if failed else 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
