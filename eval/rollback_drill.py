#!/usr/bin/env python3
"""W19 ③ 回滚演练：坏 Skill 硬失败 → 自动回退，且留下可核对的证据。

验收要求（W19 第 3 条）："回滚恢复旧版本与状态，坏 Skill/RED 注入阻断有日志"。
本演练用**真实接口**（不是 mock 逻辑）验证三件事：
  1. 预发区技能在 status=watch + 硬失败 时被自动回退到实验区
  2. 回退写入 tier 审计日志（可核对 actor/trigger/evidence）
  3. 实验区技能不会被误回退（保护区间的边界）

全程在隔离 HOME 下运行，不触碰真实技能库。

用法：python eval/rollback_drill.py [--json]
退出码：0 演练通过；2 有断言失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def run_drill() -> dict:
    from agents import skill_tiers as tiers

    steps: list[dict] = []

    def check(name: str, ok: bool, detail: object = None) -> None:
        steps.append({"step": name, "ok": bool(ok), "detail": detail})

    bad = "drill-bad-skill"
    good = "drill-good-skill"

    # ① 预置：一个进预发区，一个留在实验区
    tiers.set_tier(bad, tiers.TIER_STAGING, trigger="drill_setup",
                   actor="system", producer="rollback_drill")
    tiers.set_tier(good, tiers.TIER_EXPERIMENTAL, trigger="drill_setup",
                   actor="system", producer="rollback_drill")
    check("setup-staging", tiers.get_tier(bad) == tiers.TIER_STAGING, tiers.get_tier(bad))
    check("setup-experimental", tiers.get_tier(good) == tiers.TIER_EXPERIMENTAL,
          tiers.get_tier(good))

    # ② 注入硬失败报告（模拟 RED 失败 + watch 状态 + 有 replay 样本）
    os.environ["EVOHARNESS_TIER_AUTO_ROLLBACK"] = "1"
    report = {
        "skills": [
            {"skill": bad, "status": "watch",
             "replay": {"count": 3},
             "eval": {"hard_failures": 2}},
            {"skill": good, "status": "watch",
             "replay": {"count": 3},
             "eval": {"hard_failures": 2}},
        ]
    }
    result = tiers.apply_eval_promotions(report)

    # ③ 断言：坏的被回退，好的是实验区（本来就该留在那）
    check("bad-rolled-back", tiers.get_tier(bad) == tiers.TIER_EXPERIMENTAL,
          tiers.get_tier(bad))
    check("bad-listed", bad in (result.get("rolled_back") or []), result.get("rolled_back"))
    check("good-untouched", tiers.get_tier(good) == tiers.TIER_EXPERIMENTAL,
          tiers.get_tier(good))
    check("no-rollback-errors", not (result.get("rollback_errors") or []),
          result.get("rollback_errors"))

    # ④ 审计日志：回退必须有可核对记录
    audit_path = tiers.tier_audit_path() if hasattr(tiers, "tier_audit_path") else None
    audit_rows = []
    if audit_path and Path(audit_path).is_file():
        for line in Path(audit_path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                audit_rows.append(json.loads(line))
            except ValueError:
                continue
    rollback_rows = [r for r in audit_rows
                     if r.get("trigger") == "auto_guard_hard_failure"
                     and r.get("skill") == bad]
    check("audit-logged", bool(rollback_rows),
          {"auditPath": str(audit_path), "matched": len(rollback_rows)})
    if rollback_rows:
        row = rollback_rows[-1]
        check("audit-has-evidence",
              bool(row.get("evidence")),
              row.get("evidence"))
        check("audit-actor-is-system", row.get("actor") == "system", row.get("actor"))

    # ⑤ 关闭开关时不得回退（防止默认自动回退误伤）
    tiers.set_tier(bad, tiers.TIER_STAGING, trigger="drill_reset",
                   actor="system", producer="rollback_drill")
    os.environ["EVOHARNESS_TIER_AUTO_ROLLBACK"] = "0"
    tiers.apply_eval_promotions({
        "skills": [{"skill": bad, "status": "watch",
                    "replay": {"count": 3}, "eval": {"hard_failures": 5}}]})
    check("disabled-means-no-rollback", tiers.get_tier(bad) == tiers.TIER_STAGING,
          tiers.get_tier(bad))

    return {"ok": all(s["ok"] for s in steps), "steps": steps}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    home = Path(tempfile.mkdtemp(prefix="evo-rollback-drill-"))
    original = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    original_cwd = Path.cwd()
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    # **cwd 也必须隔离**：get_evolution_dir() = cwd/.evoharness/skill-evolution，
    # 只隔离 HOME 会让演练把审计日志写进真实仓库（曾写入 1547 行）。
    os.chdir(home)
    try:
        result = run_drill()
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.environ.pop("EVOHARNESS_TIER_AUTO_ROLLBACK", None)
        os.chdir(original_cwd)
        import shutil
        shutil.rmtree(home, ignore_errors=True)

    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "rollback-drill.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for step in result["steps"]:
            print(f"{'PASS' if step['ok'] else 'FAIL'}  {step['step']}")
        print(f"-> {out}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
