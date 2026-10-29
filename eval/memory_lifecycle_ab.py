#!/usr/bin/env python3
"""记忆生命周期 A/B 对照：开 / 关两组，同一记忆目录、同一模型、同一查询集。

为什么这样设计（而不是跑 GAIA/LoCoMo）：
- lifecycle 影响的是**跨会话召回选择**，GAIA 单题独立不触发记忆召回；
  LoCoMo 数据集不在仓库，无法重跑。
- 端到端答题噪声大（模型可能不靠记忆也答对）。直接测**召回选择**才是
  lifecycle 精确作用的对象，且可完全复现。

判分口径：
- 每个 query 有期望记忆（ground truth）与被排除记忆（陈旧版/被覆盖版）
- 命中期望 = recall hit；选中被排除 = contamination（污染）
- 记 tokens，供 ROI 计算

用法：
  python eval/memory_lifecycle_ab.py [--json] [--out PATH]
退出码：0 完成；1 有异常。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import agents.memory as memory  # noqa: E402

ENDPOINT = "http://127.0.0.1:8326/v1/chat/completions"
MODEL = os.environ.get("EVOHARNESS_EVAL_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")

# 三臂：off（旧行为）/ soft（标注 stale+superseded，默认）/ filter（硬过滤被取代候选）
ARMS = ("off", "soft", "filter")


# ── 记忆库：12 条，覆盖冲突/重复/陈旧/干净四类 ────────────────

def build_memory_fixtures(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    specs = [
        # (文件名, name, subject, type, description, 正文, 年龄天数)
        ("proj_deploy_old.md", "deploy-old", "deploy-policy", "project",
         "部署到生产必须手工发版", "生产发版需要人工审批。", 60),
        ("proj_deploy_new.md", "deploy-new", "deploy-policy", "project",
         "部署到生产改为流水线自动发版", "流水线绿灯自动发版，人工审批已取消。", 2),
        ("proj_commit_a.md", "commit-a", "git-commit-a", "project",
         "git 提交信息首行五十字符内用祈使句", "首行 <=50，祈使句，正文写 why。", 3),
        ("proj_commit_b.md", "commit-b", "git-commit-b", "project",
         "git 提交信息首行五十字符内用祈使句加序号", "首行 <=50，祈使句加序号，正文写 why。", 3),
        ("user_tone.md", "tone", "reply-tone", "user",
         "回复固定用中文且直接给结论", "中文回答，先说结论再解释。", 1),
        ("ref_dashboard.md", "dashboard", "monitor-url", "reference",
         "监控面板地址", "面板：https://grafana.internal/d/evo", 5),
        ("proj_python_sort.md", "py-sort", "python-sort", "project",
         "python 排序统一用 sorted 不用 list.sort", "用 sorted()，不改原列表。", 7),
        ("proj_git_tag.md", "tag", "git-tag", "project",
         "打 tag 用语义化版本带 v 前缀", "vMAJOR.MINOR.PATCH。", 4),
        ("proj_quota_old.md", "quota-old", "token-quota", "project",
         "每日 token 配额十万", "每天上限 100000 tokens。", 90),
        ("proj_quota_new.md", "quota-new", "token-quota", "project",
         "每日 token 配额三百万", "每天上限 3000000 tokens。", 1),
        ("fb_naming.md", "naming", "naming-rule", "feedback",
         "测试文件统一 test_ 前缀", "测试文件必须 test_*.py。", 20),
        ("stale_hint.md", "old-hint", "old-build-hint", "project",
         "构建用 make build 脚本", "make build。", 200),
    ]
    written = []
    for filename, name, subject, mtype, desc, body, age_days in specs:
        path = root / filename
        path.write_text(
            f"---\nname: {name}\nsubject: {subject}\ntype: {mtype}\ndescription: {desc}\n---\n\n{body}\n",
            encoding="utf-8")
        stamp = now - age_days * 86400
        os.utime(path, (stamp, stamp))
        written.append({"file": filename, "name": name, "subject": subject,
                        "ageDays": age_days})

    # ground truth：每个 query 期望选中的记忆 + 必须排除的记忆
    # v2 强化：加入跨记忆组合推理（需同时命中两条）与"旧版本必须排除"的对照，
    # 提高区分度——v1 的 6 个 query 两组召回率都是天花板 1.0，无区分度。
    queries = [
        {"id": "q_deploy", "query": "生产环境发版流程是什么，还需要人工审批吗？",
         "expect": ["proj_deploy_new.md"], "forbid": ["proj_deploy_old.md"]},
        {"id": "q_quota", "query": "每天最多能花多少 token？",
         "expect": ["proj_quota_new.md"], "forbid": ["proj_quota_old.md"]},
        {"id": "q_commit", "query": "写 git 提交信息有什么格式要求？",
         "expect": ["proj_commit_a.md", "proj_commit_b.md"], "forbid": []},
        {"id": "q_tone", "query": "回答我问题时用什么语言和语气？",
         "expect": ["user_tone.md"], "forbid": []},
        {"id": "q_dash", "query": "监控面板在哪个地址？",
         "expect": ["ref_dashboard.md"], "forbid": []},
        {"id": "q_build", "query": "这个项目怎么构建？",
         "expect": ["stale_hint.md"], "forbid": []},
        # ── 跨记忆组合推理：答案需要同时用到两条不同记忆 ──
        {"id": "q_combo_deploy_quota",
         "query": "按当前配额，流水线自动发版一次最多能烧多少 token？",
         "expect": ["proj_deploy_new.md", "proj_quota_new.md"],
         "forbid": ["proj_deploy_old.md", "proj_quota_old.md"]},
        {"id": "q_combo_tag_commit",
         "query": "打 tag 和写提交信息分别有什么格式约束？",
         "expect": ["proj_git_tag.md", "proj_commit_a.md"],
         "forbid": []},
        # ── 历史沿革询问：明确要求"变化过程"，此时旧版本是**合法**信息 ──
        {"id": "q_history_quota",
         "query": "token 配额从多少改到多少，什么时候改的？",
         "expect": ["proj_quota_new.md", "proj_quota_old.md"],
         "forbid": []},
        {"id": "q_combo_naming_sort",
         "query": "按命名规则，排序相关的测试文件该叫什么？",
         "expect": ["fb_naming.md", "proj_python_sort.md"],
         "forbid": []},
    ]
    return {"memories": written, "queries": queries}


# ── 真实模型 side_query ────────────────────────────────────


def _api_key() -> str:
    env_file = REPO / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("APIKEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("DEEPSEEK_API_KEY", "")


def make_side_query():
    """返回 (side_query 协程函数, 记账器)。用真实模型，不用桩。"""
    import urllib.error
    import urllib.request

    key = _api_key()
    ledger = {"calls": 0, "promptTokens": 0, "completionTokens": 0, "errors": []}

    async def side_query(system: str, user: str) -> str:
        import openai  # type: ignore

        client = openai.AsyncOpenAI(api_key=key or "local-no-key", base_url="http://127.0.0.1:8326/v1")
        try:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0.0,
                max_tokens=800,
            )
            ledger["calls"] += 1
            usage = getattr(resp, "usage", None)
            if usage:
                ledger["promptTokens"] += getattr(usage, "prompt_tokens", 0) or 0
                ledger["completionTokens"] += getattr(usage, "completion_tokens", 0) or 0
            return resp.choices[0].message.content or ""
        except Exception as error:  # noqa: BLE001
            ledger["errors"].append(f"{type(error).__name__}: {error}"[:200])
            return ""
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    return side_query, ledger


# ── 单组执行 ───────────────────────────────────────────────


async def run_group(memory_root: Path, queries: list[dict], lifecycle: str,
                    repeats: int = 1) -> dict:
    os.environ["EVOHARNESS_MEMORY_LIFECYCLE"] = lifecycle
    side_query, ledger = make_side_query()
    rows = []
    for item in queries:
        for rep in range(repeats):
            surfaced: set[str] = set()
            try:
                recalled = await memory.select_relevant_memories(
                    item["query"], side_query, surfaced)
                picked = [Path(r.path).name for r in recalled]
            except Exception as error:  # noqa: BLE001
                picked = []
                ledger["errors"].append(f"select failed: {type(error).__name__}: {error}"[:200])
            hit = [name for name in item["expect"] if name in picked]
            contaminated = [name for name in item["forbid"] if name in picked]
            rows.append({
                "id": item["id"], "repeat": rep, "picked": picked,
                "expect": item["expect"], "forbid": item["forbid"],
                "recallHit": len(hit), "recallWanted": len(item["expect"]),
                "contaminated": contaminated,
                "hitRate": round(len(hit) / len(item["expect"]), 3) if item["expect"] else 0.0,
            })
    return {
        "lifecycle": lifecycle,
        "rows": rows,
        "tokens": ledger["promptTokens"] + ledger["completionTokens"],
        "promptTokens": ledger["promptTokens"],
        "completionTokens": ledger["completionTokens"],
        "calls": ledger["calls"],
        "errors": ledger["errors"],
    }


def summarize(group: dict) -> dict:
    rows = group["rows"]
    hits = sum(r["recallHit"] for r in rows)
    wanted = sum(r["recallWanted"] for r in rows)
    contaminated = sum(len(r["contaminated"]) for r in rows)
    # 按 query 聚合：某 query 在任一 repeat 出现污染即记为"该 query 曾污染"
    by_query: dict[str, dict] = {}
    for row in rows:
        slot = by_query.setdefault(row["id"], {"runs": 0, "contaminatedRuns": 0,
                                               "hits": 0, "wanted": 0})
        slot["runs"] += 1
        if row["contaminated"]:
            slot["contaminatedRuns"] += 1
        slot["hits"] += row["recallHit"]
        slot["wanted"] += row["recallWanted"]
    return {
        "lifecycle": group["lifecycle"],
        "runs": len(rows),
        "recallRate": round(hits / wanted, 3) if wanted else 0.0,
        "hits": hits, "wanted": wanted,
        "contamination": contaminated,
        "contaminatedQueries": sorted(q for q, v in by_query.items() if v["contaminatedRuns"]),
        "contaminatedQueryRuns": sum(v["contaminatedRuns"] for v in by_query.values()),
        "cleanQueries": sum(1 for r in rows if not r["contaminated"]),
        "tokens": group["tokens"],
        "calls": group["calls"],
        "errors": group["errors"],
        "byQuery": by_query,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--repeats", type=int, default=1,
                        help="每组重复次数（协议要求 n>=30 才可宣称；这里先看稳定性）")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--arms", default=",".join(ARMS))
    args = parser.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    model = args.model

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "memories"
        fixtures = build_memory_fixtures(root)
        # 把记忆目录指向临时区（不碰真实 ~/.evoharness）
        original = memory.get_memory_dir
        memory.get_memory_dir = lambda: root  # type: ignore[assignment]
        try:
            groups = {}
            for arm in arms:
                groups[arm] = asyncio.run(
                    run_group(root, fixtures["queries"], arm, args.repeats))
        finally:
            memory.get_memory_dir = original  # type: ignore[assignment]
            os.environ.pop("EVOHARNESS_MEMORY_LIFECYCLE", None)

    summaries = {arm: summarize(group) for arm, group in groups.items()}
    baseline = summaries.get("off") or next(iter(summaries.values()))
    deltas = {}
    for arm, summary in summaries.items():
        if arm == baseline["lifecycle"]:
            continue
        deltas[arm] = {
            "recallRatePts": round((summary["recallRate"] - baseline["recallRate"]) * 100, 1),
            "contaminationDelta": summary["contamination"] - baseline["contamination"],
            "tokensDeltaPct": round(
                (summary["tokens"] - baseline["tokens"]) / baseline["tokens"] * 100, 1)
            if baseline["tokens"] else None,
        }
    total_obs = sum(s["runs"] for s in summaries.values())
    report = {
        "kind": "memory-lifecycle-ab",
        "model": MODEL,
        "repeats": args.repeats,
        "arms": arms,
        "fixtures": {"memories": len(fixtures["memories"]),
                     "queries": len(fixtures["queries"])},
        "armsDetail": summaries,
        "baseline": baseline["lifecycle"],
        "deltas": deltas,
        "detail": {arm: group["rows"] for arm, group in groups.items()},
        "claimability": {
            "totalObservations": total_obs,
            "threshold": 30,
            "perArmObservations": {arm: s["runs"] for arm, s in summaries.items()},
            "verdict": "not-claimable:undersampled" if total_obs < 30 else "needs-noise-check",
            "note": ("观测数达标只是必要条件；还需同配置重复间一致（配置噪声 < 差异）"
                     "才能宣称。污染事件绝对数很小，必须看跨批次是否复现。"),
        },
    }
    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "memory-lifecycle-ab.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"model": MODEL, "arms": report["armsDetail"],
                          "deltas": report["deltas"],
                          "claimability": report["claimability"],
                          "out": str(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
