"""人工门的可执行工作台（EVO-H1 人工耗时基线 / EVO-H2 独立语义真值审阅 / EVO-H3 自然周期核对）。

设计原则（为什么这样切）：
- 这三项的共同阻塞是"需要真人"，不是"需要代码"；代理不能伪造人工结论。
  所以工具只做三件事：**准备最小可行的输入**、**强制记录可审计的元数据**（谁/何时/基于哪个
  revision 与哈希）、**机械核验声明与产物一致**。结论本身永远由人给出。
- 任何缺元数据的记录一律拒绝（fail-closed），避免"看起来完成了"的假验收。
- 输出写 eval/runs/human-gates/（.gitignore 已覆盖 eval/runs/），不进仓库。

子命令：
  h1-plan    生成 H1 人工耗时基线的计时任务单（含样本、字段、口径）
  h1-check   核验一份计时记录是否完整、是否可并入对比
  h2-plan    从归因产物生成 H2 盲审任务单（去掉模型结论，避免锚定）
  h2-check   核验一份审阅记录并计算与模型归因的一致率
  h3-check   只读核对服务器/本地 org.db 的自然周期日报天数（H3 时间门进度）

用法示例：
  python eval/human_gate.py h1-plan --out eval/runs/human-gates
  python eval/human_gate.py h2-plan --attribution eval/runs/attribution-20261121 --out eval/runs/human-gates --sample 20
  python eval/human_gate.py h2-check eval/runs/human-gates/h2-review.json
  python eval/human_gate.py h3-check --db /var/lib/evoharness/org.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "eval" / "runs" / "human-gates"

# 归因五类固定标签（与 agents/skill_search/feedback.py 的 schema 对齐）
ATTRIBUTION_CATEGORIES = ("SKILL_ISSUE", "CASE_ISSUE", "HARNESS_ISSUE", "ENVIRONMENT_ISSUE", "UNCERTAIN")

# H1 计时口径：只统计"从看到题面到产出可交付答案"的净工作时间，不含环境搭建/等待模型
H1_TIMING_FIELDS = (
    "case_id",
    "started_at",
    "ended_at",
    "net_seconds",
    "operator",
    "tooling",
    "notes",
)


def read_json(path: Path, what: str) -> dict:
    """读 JSON 时容忍 BOM。

    为什么：Windows PowerShell 的 Out-File/重定向默认写 UTF-8 BOM，
    人工填写的记录很可能带 BOM；这里统一按 utf-8-sig 读取并给出可读报错，
    而不是抛原始 JSONDecodeError 让使用者以为工具坏了。
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SystemExit(f"REJECTED: 无法读取{what} {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"REJECTED: {what} {path} 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"REJECTED: {what} {path} 顶层必须是对象")
    return data


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(f"REJECTED: {msg}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_attribution_cases(attr_dir: Path) -> list[dict]:
    cases = []
    for p in sorted(attr_dir.glob("cases/*/*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if d.get("kind") != "evo07-attribution-case":
            continue
        cases.append(
            {
                "path": p,
                "case_id": d.get("case_id"),
                "arm": d.get("arm"),
                "role": d.get("role"),
                "grade": d.get("grade", {}),
                "proposal": d.get("proposal", {}),
                "agreement": d.get("agreement", {}),
                "case_sha256": sha256_of(p),
            }
        )
    return cases


# ---------------------------------------------------------------- H1

def h1_plan(out_dir: Path, protocol: Path | None) -> dict:
    """生成人工耗时基线任务单。

    样本取自冻结题单的开发/验证集（有人在用真实询价样本），不取留出集，
    避免计时过程反向污染留出集。若提供了 protocol，记录其哈希以便追溯。
    """
    protocol_sha = sha256_of(protocol) if protocol and protocol.exists() else None
    plan = {
        "kind": "h1-timing-plan",
        "generated_at": _now(),
        "repo_revision": _git_rev(),
        "protocol_path": str(protocol) if protocol else None,
        "protocol_sha256": protocol_sha,
        "purpose": "采集真实人工处理询价样本的耗时基线，用于与三臂模型结果对比（DE-24）",
        "timing_definition": {
            "started_when": "开始阅读题面/原始询价材料",
            "ended_when": "产出可交付结论（含 JSON 字段齐全）并自查完毕",
            "net_seconds": "净工作时间，扣除环境搭建、等待、打断",
            "exclude": ["环境安装", "等待模型/网络", "与本题无关的会议与打断"],
        },
        "required_fields": list(H1_TIMING_FIELDS),
        "suggested_sample_size": 20,
        "min_sample_size": 10,
        "instructions": [
            "每条样本独立计时，不要事后回忆估算",
            "同一 operator 至少 10 条，且覆盖至少 2 个不同 difficulty 档",
            "把结果写成 h1-timing.json：{\"records\": [ ... ]}，字段见 required_fields",
            "完成后运行 h1-check 核验，再并入对比",
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "h1-plan.json"
    target.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    plan["written_to"] = str(target)
    return plan


def h1_check(record_path: Path) -> dict:
    """核验计时记录：字段完整、时间自洽、净时间合理、样本量达标。"""
    _require(record_path.exists(), f"记录文件不存在: {record_path}")
    data = read_json(record_path, "计时记录")
    records = data.get("records")
    _require(isinstance(records, list) and records, "records 必须是非空数组")

    problems: list[str] = []
    timing_meta_keys = {"repo_revision", "protocol_sha256", "operator_ids"}

    for i, r in enumerate(records):
        tag = f"records[{i}]"
        if not isinstance(r, dict):
            problems.append(f"{tag}: 不是对象")
            continue
        for f in H1_TIMING_FIELDS:
            _require(f in r, f"{tag} 缺少字段 {f}（元数据不完整一律拒绝）")
        net = r.get("net_seconds")
        if not isinstance(net, (int, float)) or isinstance(net, bool) or net <= 0:
            problems.append(f"{tag}: net_seconds 必须是正数，实际 {net!r}")
            continue
        if net > 8 * 3600:
            problems.append(f"{tag}: net_seconds={net} 超过 8 小时，疑似不是单题净耗时")
        try:
            s = datetime.fromisoformat(str(r["started_at"]))
            e = datetime.fromisoformat(str(r["ended_at"]))
        except ValueError as exc:
            problems.append(f"{tag}: 时间戳不可解析 ({exc})")
            continue
        wall = (e - s).total_seconds()
        if wall < net:
            problems.append(f"{tag}: 净耗时 {net}s 大于墙钟跨度 {wall}s，时间自洽性不成立")
        for f in ("case_id", "operator"):
            if not str(r.get(f) or "").strip():
                problems.append(f"{tag}: {f} 不能为空")

    _require(not problems, "; ".join(problems[:8]))
    _require("records_meta" in data or any(k in data for k in timing_meta_keys),
             "缺少记录级元数据（repo_revision / protocol_sha256 / operator_ids 至少一项）")

    nets = [float(r["net_seconds"]) for r in records]
    nets_sorted = sorted(nets)
    n = len(nets_sorted)
    median = (nets_sorted[n // 2] if n % 2 else (nets_sorted[n // 2 - 1] + nets_sorted[n // 2]) / 2)
    operators = sorted({str(r["operator"]) for r in records})
    return {
        "kind": "h1-timing-check",
        "checked_at": _now(),
        "record_path": str(record_path),
        "record_sha256": sha256_of(record_path),
        "sample_size": n,
        "meets_min_sample": n >= 10,
        "operators": operators,
        "distinct_operators": len(operators),
        "mean_seconds": round(sum(nets) / n, 2),
        "median_seconds": round(median, 2),
        "min_seconds": min(nets),
        "max_seconds": max(nets),
        "note": "此为人工耗时基线统计；是否可用于效果对比仍需与三臂结果按同一题单对齐后再判定。",
    }


# ---------------------------------------------------------------- H2

def h2_plan(attr_dir: Path, out_dir: Path, sample: int, seed: int) -> dict:
    """生成盲审任务单：只给题面 + 冻结 grade 错误项 + 执行轨迹，隐藏模型归因与置信度。

    为什么要盲：模型结论（SKILL_ISSUE 倾向）会锚定审阅人，使"独立真值"失去独立性。
    抽样用确定性顺序（按 case_id 排序后等距取），保证可复现。
    """
    cases = load_attribution_cases(attr_dir)
    _require(len(cases) > 0, f"在 {attr_dir} 下没找到 evo07-attribution-case 产物")
    cases.sort(key=lambda c: (str(c["case_id"]), str(c["arm"])))

    if sample > 0 and sample < len(cases):
        step = len(cases) / sample
        picked = [cases[int(i * step)] for i in range(sample)]
    else:
        picked = cases

    items = []
    for c in picked:
        prop = c.get("proposal") or {}
        items.append(
            {
                "case_id": c["case_id"],
                "arm": c["arm"],
                "role": c["role"],
                "frozen_grade": c["grade"],
                # 盲审材料：只暴露"哪一步失败"的事实，不暴露模型判断
                "failure_evidence": {
                    "errors": c["grade"].get("errors", []),
                    "valid_json": c["grade"].get("passed"),
                },
                "trace_ref": prop.get("trace_ref") or f"trace://{c['arm']}/{c['case_id']}",
                "case_sha256": c["case_sha256"],
                "allowed_verdicts": list(ATTRIBUTION_CATEGORIES),
            }
        )

    plan = {
        "kind": "h2-review-plan",
        "generated_at": _now(),
        "repo_revision": _git_rev(),
        "attribution_dir": str(attr_dir),
        "population": len(cases),
        "sampled": len(items),
        "sampling": "deterministic_even_spacing_over_sorted_case_id" if len(items) < len(cases) else "full",
        "blind_rules": [
            "本任务单不含模型归因类别、置信度与建议动作，审阅人不得查看 cases/*.json 的 proposal 字段",
            "按冻结 grade 的 errors 与执行轨迹独立判断失败根因",
            "判断不确定时选 UNCERTAIN，不要强行归类",
        ],
        "required_fields_per_item": ["case_id", "verdict", "reviewer", "reviewed_at", "rationale"],
        "output_format": "{\"reviews\": [{\"case_id\": ..., \"verdict\": ..., \"reviewer\": ..., \"reviewed_at\": ..., \"rationale\": ...}]}",
        "items": items,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "h2-plan.json"
    target.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # 同时给审阅人一张纯文本清单，便于逐条填写
    checklist = out_dir / "h2-checklist.md"
    lines = ["# H2 独立语义真值审阅清单", "",
             f"- 生成：{plan['generated_at']}",
             f"- 总体：{plan['population']} 份，抽样 {plan['sampled']} 份（{plan['sampling']}）",
             "- 盲审：不得查看 proposal 字段；不确定选 UNCERTAIN", "",
             "| # | case_id | arm | 冻结错误项 | 你的判定 | 理由 |", "|---|---|---|---|---|---|"]
    for i, it in enumerate(items, 1):
        errs = ",".join(it["failure_evidence"]["errors"]) or "(无)"
        lines.append(f"| {i} | {it['case_id']} | {it['arm']} | {errs} |  |  |")
    checklist.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {"plan": target, "checklist": checklist, "sampled": len(items), "population": len(cases)}


def h2_check(review_path: Path, attr_dir: Path | None) -> dict:
    """核验审阅记录，并在给了归因产物目录时计算与模型结论的一致率。"""
    _require(review_path.exists(), f"审阅记录不存在: {review_path}")
    data = read_json(review_path, "审阅记录")
    reviews = data.get("reviews")
    _require(isinstance(reviews, list) and reviews, "reviews 必须是非空数组")

    problems = []
    seen = set()
    for i, r in enumerate(reviews):
        tag = f"reviews[{i}]"
        for f in ("case_id", "verdict", "reviewer", "reviewed_at", "rationale"):
            _require(f in r, f"{tag} 缺少字段 {f}（人工结论必须可审计）")
        v = r["verdict"]
        if v not in ATTRIBUTION_CATEGORIES:
            problems.append(f"{tag}: verdict={v!r} 不在固定五类 {ATTRIBUTION_CATEGORIES}")
        if r["case_id"] in seen:
            problems.append(f"{tag}: case_id={r['case_id']} 重复")
        seen.add(r["case_id"])
        if not str(r["rationale"] or "").strip():
            problems.append(f"{tag}: rationale 不能为空（无理由的判定不可用）")
        try:
            datetime.fromisoformat(str(r["reviewed_at"]))
        except ValueError:
            problems.append(f"{tag}: reviewed_at 不可解析")
    _require(not problems, "; ".join(problems[:8]))

    result = {
        "kind": "h2-review-check",
        "checked_at": _now(),
        "review_path": str(review_path),
        "review_sha256": sha256_of(review_path),
        "reviewed": len(reviews),
        "reviewers": sorted({str(r["reviewer"]) for r in reviews}),
        "verdicts": {c: sum(1 for r in reviews if r["verdict"] == c) for c in ATTRIBUTION_CATEGORIES},
    }

    if attr_dir is not None:
        cases = {c["case_id"]: c for c in load_attribution_cases(attr_dir)}
        agree = disagree = unknown = 0
        detail = []
        for r in reviews:
            c = cases.get(r["case_id"])
            if c is None:
                unknown += 1
                continue
            model_cat = (c.get("proposal") or {}).get("category")
            human = r["verdict"]
            same = model_cat == human
            agree += 1 if same else 0
            disagree += 0 if same else 1
            detail.append(
                {"case_id": r["case_id"], "human": human, "model": model_cat, "agree": same}
            )
        judged = agree + disagree
        result["agreement"] = {
            "judged": judged,
            "agree": agree,
            "disagree": disagree,
            "unknown_case": unknown,
            "agreement_rate": round(agree / judged, 4) if judged else None,
            "detail_head": detail[:20],
        }
        result["note"] = (
            "一致率只描述人机判断重合度，不等于真值；"
            "不一致项需逐条复核，不得直接把模型结论当真值。"
        )
    return result


# ---------------------------------------------------------------- H3

def h3_check(db_path: Path) -> dict:
    """只读核对自然周期日报累计（H3 时间门）。不改数据库、不发送任何消息。"""
    _require(db_path.exists(), f"数据库不存在: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select logical_date, status, delivered_at from daily_reports order by logical_date"
        ).fetchall()
        try:
            mode = conn.execute(
                "select value from system_state where key='deployment_mode'"
            ).fetchone()
        except sqlite3.Error:
            mode = None
    finally:
        conn.close()

    natural, backfill = [], []
    for logical_date, status, delivered_at in rows:
        if status != "sent" or not delivered_at:
            continue
        # delivered_at 存 UTC；东八区 08:30 == 当日 UTC 00:30
        if str(delivered_at)[:10] == logical_date and str(delivered_at)[11:16] == "00:30":
            natural.append(logical_date)
        else:
            backfill.append(logical_date)

    consecutive = 0
    target = 7
    if natural:
        ds = sorted(date.fromisoformat(d) for d in natural)
        runs, cur = [], [ds[0]]
        for a, b in zip(ds, ds[1:]):
            if (b - a).days == 1:
                cur.append(b)
            else:
                runs.append(cur)
                cur = [b]
        runs.append(cur)
        consecutive = len(runs[-1])
        latest_run = f"{runs[-1][0]} .. {runs[-1][-1]}"
    else:
        latest_run = None

    return {
        "kind": "h3-natural-period-check",
        "checked_at": _now(),
        "db_path": str(db_path),
        "deployment_mode": mode[0] if mode else None,
        "natural_days_total": len(natural),
        "natural_days": natural,
        "backfill_days": len(backfill),
        "latest_consecutive_run": latest_run,
        "consecutive_days": consecutive,
        "target_days": target,
        "remaining_days": max(0, target - consecutive),
        "verdict": "complete" if consecutive >= target else "in_progress",
        "note": "仅统计自然调度 08:30 当日产出（UTC 00:30）且 status=sent 的记录；补发/修订不计入连续段。",
    }


def _git_rev() -> str:
    try:
        import subprocess

        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="人工门工作台（H1 耗时 / H2 盲审 / H3 自然周期）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("h1-plan", help="生成人工耗时基线任务单")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--protocol", type=Path, default=None)

    p = sub.add_parser("h1-check", help="核验计时记录")
    p.add_argument("record", type=Path)

    p = sub.add_parser("h2-plan", help="生成盲审任务单")
    p.add_argument("--attribution", type=Path, required=True)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--sample", type=int, default=20)
    p.add_argument("--seed", type=int, default=0)

    p = sub.add_parser("h2-check", help="核验审阅记录并算人机一致率")
    p.add_argument("review", type=Path)
    p.add_argument("--attribution", type=Path, default=None)

    p = sub.add_parser("h3-check", help="核对自然周期日报累计")
    p.add_argument("--db", type=Path, required=True)

    args = ap.parse_args(argv)

    if args.cmd == "h1-plan":
        out = h1_plan(args.out, args.protocol)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.cmd == "h1-check":
        print(json.dumps(h1_check(args.record), ensure_ascii=False, indent=2))
    elif args.cmd == "h2-plan":
        out = h2_plan(args.attribution, args.out, args.sample, args.seed)
        print(json.dumps({k: str(v) for k, v in out.items()}, ensure_ascii=False, indent=2))
    elif args.cmd == "h2-check":
        print(json.dumps(h2_check(args.review, args.attribution), ensure_ascii=False, indent=2))
    elif args.cmd == "h3-check":
        print(json.dumps(h3_check(args.db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
