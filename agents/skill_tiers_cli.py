#!/usr/bin/env python3
"""部署分区 CLI —— 让"晋级/回退"能在运维侧直接用，不必 curl。

为什么补这个（2026-10-29 全栈评测发现）：
`GET /api/skills/tiers` 与 `POST /api/skills/tiers/action` 是**部署分区管理**
（experimental / staging / core），但它们既没有前端入口（前端 `stageBand` 展示的是
**评测阶段** incubating/watch/healthy，是另一个概念），`skill_tiers.py` 也没有 CLI
→ 运维人员想晋级/回退一个技能，**只能手写 curl**。

本 CLI 直接复用库里的 `get_tier_map` / `manual_promote` / `rollback`，
不复制业务逻辑（避免 CLI 与 HTTP 两条路径行为漂移）。

用法：
  python -m agents.skill_tiers_cli list
  python -m agents.skill_tiers_cli show <skill>
  python -m agents.skill_tiers_cli promote <skill> --to staging
  python -m agents.skill_tiers_cli rollback <skill> --reason "hard failure"
  python -m agents.skill_tiers_cli doctor          # 分区元数据是否健康
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _tiers():
    # 允许以脚本方式直接运行时也能 import 到 agents 包
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agents import skill_tiers

    return skill_tiers


def cmd_list(args: argparse.Namespace) -> int:
    st = _tiers()
    mapping = st.get_tier_map()
    if args.json:
        print(json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    # format_tiers() 无参（内部自己 get_tier_map），不要传 mapping
    if hasattr(st, "format_tiers"):
        print(st.format_tiers())
    else:
        print(json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    st = _tiers()
    tier = st.get_tier(args.skill)
    payload = {
        "skill": args.skill,
        "tier": tier,
        "degraded": bool(st.tier_state_degraded()) if hasattr(st, "tier_state_degraded") else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """晋级技能分区。

    两条语义严格区分：
    - `--via-gate`：走库里的 `manual_promote`（自动门禁：healthy + champion 双断言）
    - 默认（`--to X`）：管理员直设，走 `set_tier`，**明确绕过自动门禁**
      —— 审计里会记 `trigger=cli_manual_override`，事后可查。
    """
    st = _tiers()

    if getattr(args, "via_gate", False):
        result = st.manual_promote(args.skill)
    else:
        result = st.set_tier(
            args.skill,
            args.to,
            trigger="cli_manual_override",
            actor="cli",
            producer="skill_tiers_cli",
            evidence={"reason": args.reason or "manual promote via CLI"},
        )
    print(json.dumps(result if isinstance(result, dict) else {"ok": True, "result": result},
                     ensure_ascii=False, indent=2))
    return 0 if (result or {}).get("ok", True) else 1


def cmd_rollback(args: argparse.Namespace) -> int:
    st = _tiers()
    result = st.rollback(args.skill, reason=args.reason or "cli manual rollback")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (result or {}).get("ok", True) else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    st = _tiers()
    payload = {
        "degraded": bool(st.tier_state_degraded()) if hasattr(st, "tier_state_degraded") else None,
        "tiersPath": str(st.deployment_tiers_path()),
        "auditPath": str(st.tier_audit_path()),
        "faultPath": str(st.tier_fault_path()),
        "tiersExist": Path(st.deployment_tiers_path()).is_file(),
        "auditExists": Path(st.tier_audit_path()).is_file(),
        "skillCount": len(st.get_tier_map()),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="部署分区（experimental/staging/core）管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出全部技能的分区")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="查看单个技能的分区")
    p_show.add_argument("skill")
    p_show.set_defaults(func=cmd_show)

    p_promote = sub.add_parser("promote", help="晋级技能到指定分区")
    p_promote.add_argument("skill")
    p_promote.add_argument("--to", default="staging",
                           choices=["experimental", "staging", "core"])
    p_promote.add_argument("--reason", default="")
    p_promote.add_argument("--via-gate", action="store_true",
                           help="走自动门禁（healthy + champion）；默认是管理员直设")

    p_promote.set_defaults(func=cmd_promote)

    p_rollback = sub.add_parser("rollback", help="回退技能到实验区")
    p_rollback.add_argument("skill")
    p_rollback.add_argument("--reason", default="")
    p_rollback.set_defaults(func=cmd_rollback)

    p_doctor = sub.add_parser("doctor", help="检查分区元数据/审计文件状态")
    p_doctor.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())