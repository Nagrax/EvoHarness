#!/usr/bin/env python3
"""反思教训可召回性探针：写入的教训是否真能被真实模型的召回选择命中。

为什么先做这个（而不是直接测收益）：
收益 = 教训被召回 → 影响后续行为 → 减少同类失败。第一步断了，后面全不成立。
本探针只回答第一步，成本极低（~5K tokens），失败即说明整个反思链路无效。

设计：
- 造 8 条记忆：1 条由 reflection 生成的教训 + 7 条无关记忆（含干扰项：同为 feedback 类型）
- 3 个与教训相关的查询（同 kind 场景）+ 1 个无关查询（防过度召回）
- 真实模型做 side_query 选择，判分：教训是否被选中 / 无关查询是否误召

用法：python eval/reflection_recall_probe.py [--json] [--out PATH]
退出码：0 = 可召回；2 = 召回失败（反思链路在此断裂）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import agents.memory as memory  # noqa: E402
from agents.reflection import FailureEvent, derive_lessons, lesson_to_memory  # noqa: E402

MODEL = os.environ.get("EVOHARNESS_EVAL_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")


def _api_key() -> str:
    env_file = REPO / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("APIKEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("DEEPSEEK_API_KEY", "")


def make_side_query():
    import openai

    key = _api_key()
    ledger = {"calls": 0, "tokens": 0, "errors": []}

    async def side_query(system: str, user: str) -> str:
        client = openai.AsyncOpenAI(api_key=key or "local-no-key",
                                    base_url="http://127.0.0.1:8326/v1")
        try:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0.0, max_tokens=600)
            ledger["calls"] += 1
            usage = getattr(resp, "usage", None)
            if usage:
                ledger["tokens"] += (getattr(usage, "total_tokens", 0) or 0)
            return resp.choices[0].message.content or ""
        except Exception as error:  # noqa: BLE001
            ledger["errors"].append(f"{type(error).__name__}: {error}"[:160])
            return ""
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    return side_query, ledger


def build_fixtures(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    # 1) 用 reflection 真实生成一条教训（不用手写，保证与线上格式一致）
    events = [FailureEvent(f"t{i}", "no-final-marker", "gaia") for i in range(4)]
    lesson = derive_lessons(events)[0]
    payload = lesson_to_memory(lesson)
    (root / payload["filename"]).write_text(payload["content"], encoding="utf-8")

    # 2) 干扰记忆：4 条同类型 feedback（考验收敛性）+ 3 条其他类型（考查区分）
    decoys = [
        ("feedback_review.md", "review-style", "feedback",
         "代码评审固定先看错误处理", "评审先看异常路径。"),
        ("feedback_commit.md", "commit-style", "feedback",
         "提交信息用祈使句", "首行祈使句。"),
        ("feedback_test.md", "test-naming", "feedback",
         "测试文件用 test_ 前缀", "test_*.py。"),
        ("feedback_log.md", "log-level", "feedback",
         "日志默认 info 级别", "默认 info。"),
        ("proj_deploy.md", "deploy-policy", "project",
         "流水线自动发版", "绿灯自动发。"),
        ("user_tone.md", "reply-tone", "user",
         "中文回答先说结论", "中文，先结论。"),
        ("ref_dash.md", "monitor-url", "reference",
         "监控面板地址", "https://grafana.internal/d/e"),
    ]
    for filename, name, mtype, desc, body in decoys:
        (root / filename).write_text(
            f"---\nname: {name}\nsubject: {name}\ntype: {mtype}\ndescription: {desc}\n---\n\n{body}\n",
            encoding="utf-8")

    return {
        "lessonFile": payload["filename"],
        "lessonSubject": payload["subject"],
        "queries": [
            # 相关：同失败场景（收尾没给最终答案）
            {"id": "q_related_1", "expect": [payload["filename"]],
             "query": "任务收尾时经常没有明确给出最终答案，该怎么避免？"},
            {"id": "q_related_2", "expect": [payload["filename"]],
             "query": "长任务跑到最后容易漏掉结论，有什么教训可以参考？"},
            {"id": "q_related_3", "expect": [payload["filename"]],
             "query": "上次同类任务失败的原因记录在哪里？"},
            # 无关：不应误召教训（防过度召回）
            {"id": "q_unrelated", "expect": [], "forbid": [payload["filename"]],
             "query": "监控面板在哪个地址？"},
        ],
    }


async def run(fixtures: dict) -> dict:
    side_query, ledger = make_side_query()
    rows = []
    for item in fixtures["queries"]:
        try:
            recalled = await memory.select_relevant_memories(item["query"], side_query, set())
            picked = [Path(r.path).name for r in recalled]
        except Exception as error:  # noqa: BLE001
            picked = []
            ledger["errors"].append(f"select: {type(error).__name__}: {error}"[:160])
        expect = item.get("expect") or []
        forbid = item.get("forbid") or []
        rows.append({
            "id": item["id"], "picked": picked,
            "expect": expect, "forbid": forbid,
            "hit": all(name in picked for name in expect) if expect else True,
            "falseRecall": [name for name in forbid if name in picked],
        })
    return {"rows": rows, **ledger}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "memories"
        fixtures = build_fixtures(root)
        original = memory.get_memory_dir
        memory.get_memory_dir = lambda: root  # type: ignore[assignment]
        try:
            result = asyncio.run(run(fixtures))
        finally:
            memory.get_memory_dir = original  # type: ignore[assignment]

    related = [r for r in result["rows"] if r["expect"]]
    recall_rate = (sum(1 for r in related if r["hit"]) / len(related)) if related else 0.0
    false_recall = sum(len(r["falseRecall"]) for r in result["rows"])
    report = {
        "kind": "reflection-recall-probe",
        "model": MODEL,
        "lessonFile": fixtures["lessonFile"],
        "lessonSubject": fixtures["lessonSubject"],
        "recallRate": round(recall_rate, 3),
        "relatedHits": sum(1 for r in related if r["hit"]),
        "relatedTotal": len(related),
        "falseRecall": false_recall,
        "tokens": result["tokens"],
        "calls": result["calls"],
        "errors": result["errors"],
        "rows": result["rows"],
        "ok": recall_rate > 0 and false_recall == 0,
        "verdict": ("recallable" if recall_rate > 0 and false_recall == 0
                    else "not-recallable:reflection-chain-broken"),
    }
    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "reflection-recall-probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({k: report[k] for k in
                          ("ok", "verdict", "recallRate", "relatedHits", "relatedTotal",
                           "falseRecall", "tokens")}, ensure_ascii=False, indent=2))
        for row in report["rows"]:
            print(f"  {row['id']:14s} hit={row['hit']} picked={row['picked']}")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
