"""改进闭环健康检查：不能只看"历史产出"，还要验证**底层脚本能跑**。

原版缺陷（本轮自查发现）：只看 `BACKLOG.rounds[].bySource` 的累计产出，
于是"信号源干净"与"信号源坏了"外观完全相同——**这恰恰是它要防的问题**。

新增第二判据：每个源对应的底层脚本必须存在且能正常运行（exit 0）。
两个判据合起来才可信：
- 脚本跑不通 → **坏了**
- 脚本能跑但无产出 → **干净**（可信）
"""
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
BACKLOG = REPO / "eval" / "loop" / "BACKLOG.json"

# 源 → 底层脚本（与 loop.py 里各 _collect_* 实际调用的命令一致）
SCRIPT_MAP = {
    "backend": ["eval/backend_contract.py"],
    "module": ["eval/module_scan.py"],
    "memory": ["eval/memory_eval.py"],
    "calibration": ["eval/holdout_calibrate.py"],
    "skillCombo": ["eval/skill_combo_replay.py"],
    "judgeProbe": ["eval/judge_bias_probe.py"],
    # 飞书信号：包装脚本读取黄金回放产物，不执行真实飞书写操作
    "feishu": ["eval/feishu_signal_check.py"],
    # failureStability 例外：loop 不调用它的脚本，只读产物（见下方新鲜度检查）
}

# 这些源由 loop 内部逻辑产生，没有独立脚本（前端需要浏览器、其余读产物）
NO_SCRIPT = {"frontend", "harness2", "variance", "lifecycleNoiseGate", "benchmarkFailures"}


def check_script(paths: list[str]) -> tuple[bool, str]:
    if "--" in paths:
        split = paths.index("--")
        scripts, args = paths[:split], paths[split + 1:]
    else:
        scripts, args = paths, []
    for rel in scripts:
        if not (REPO / rel).is_file():
            return False, f"缺少脚本 {rel}"
    try:
        proc = subprocess.run([sys.executable, *scripts, *args], cwd=str(REPO),
                              capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "脚本超时"
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[-160:]
        return False, f"exit={proc.returncode} {err}"
    return True, "ok"


def _usage() -> str:
    return ("用法: python eval/loop_source_health.py\n"
            "  检查闭环各信号源：脚本可跑 + 产物新鲜度\n"
            "  注意：会实际执行各源脚本，耗时约 1-2 分钟")


def main() -> int:
    if any(a in {"-h", "--help"} for a in sys.argv[1:]):
        print(_usage())
        return 0
    data = json.loads(BACKLOG.read_text(encoding="utf-8"))
    rounds = data.get("rounds") or []
    produced = Counter()
    for record in rounds:
        for source, count in (record.get("bySource") or {}).items():
            if int(count or 0) > 0:
                produced[source] += int(count)

    results = []
    for source, scripts in SCRIPT_MAP.items():
        runnable, detail = check_script(scripts)
        results.append({
            "source": source,
            "findings": produced.get(source, 0),
            "scriptRunnable": runnable,
            "detail": detail,
            "state": ("broken" if not runnable
                      else ("working" if produced.get(source, 0) else "clean")),
        })

    # 产物依赖型源：检查最新产物的新鲜度（超过 7 天视为陈旧）
    import time as _time

    artifact_checks = []
    for pattern, source, probe_cmd in (
        ("failure-stability-*.json", "failureStability",
         ["eval/failure_stability.py", "--", "--probe"]),
    ):
        producer_runnable, producer_detail = check_script(probe_cmd)
        files = sorted((REPO / "eval" / "runs").glob(pattern),
                       key=lambda f: f.stat().st_mtime)
        if not files:
            artifact_checks.append({
                "source": source, "fresh": False, "producerRunnable": producer_runnable,
                "producerDetail": producer_detail,
                "detail": f"无产物 {pattern}", "ageDays": None,
            })
            continue
        age_days = round((_time.time() - files[-1].stat().st_mtime) / 86400, 1)
        artifact_valid = True
        artifact_detail = ""
        try:
            artifact = json.loads(files[-1].read_text(encoding="utf-8"))
            infra_total = int(artifact.get("infraBlocked") or 0) + sum(
                int(task.get("infraFailures") or 0)
                for task in (artifact.get("perTask") or [])
            )
            if artifact.get("ok") is False or infra_total > 0:
                artifact_valid = False
                artifact_detail = (
                    f"infraFailures={infra_total}"
                    "（provider/配额失败不能冒充模型能力失败）"
                )
        except (ValueError, OSError) as exc:
            artifact_valid = False
            artifact_detail = f"产物不可解析: {exc}"
        artifact_checks.append({
            "source": source, "fresh": age_days <= 7 and artifact_valid,
            "artifactValid": artifact_valid, "artifactDetail": artifact_detail,
            "producerRunnable": producer_runnable, "producerDetail": producer_detail,
            "artifact": files[-1].name, "ageDays": age_days,
            "detail": f"{files[-1].name} 已 {age_days} 天" + (f"；{artifact_detail}" if artifact_detail else ""),
        })

    broken = [r["source"] for r in results if r["state"] == "broken"]
    broken += [
        c["source"] for c in artifact_checks
        if not c["fresh"] or not c.get("producerRunnable", True)
    ]
    untested = sorted(set(NO_SCRIPT) - {r["source"] for r in results})

    report = {
        "ok": not broken,
        "rounds": len(rounds),
        "sourcesChecked": len(results),
        "broken": broken,
        "noIndependentScript": untested,
        "perSource": results,
        "artifactChecks": artifact_checks,
        "verdict": ("all-sources-runnable" if not broken else "has-broken-sources"),
    }
    out = REPO / "eval" / "runs" / "loop-source-health.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"{'source':22s}{'findings':>9s}{'script':>8s}  state")
    for r in results:
        print(f"{r['source']:22s}{r['findings']:>9d}{'ok' if r['scriptRunnable'] else 'FAIL':>8s}"
              f"  {r['state']}")
    print(f"\nno independent script: {untested}")
    print(json.dumps({"ok": report["ok"], "verdict": report["verdict"]}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())