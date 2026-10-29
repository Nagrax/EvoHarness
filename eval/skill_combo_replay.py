#!/usr/bin/env python3
"""W11 组合重放评测器：发现互斥指令组合，阻断回归 Skill。

对应验收：
- "组合重放能发现互斥指令且回归 Skill 被阻断，硬失败不能被均分覆盖"
- "合成重复/矛盾案例可追溯到 add/merge/revise/discard 决策"

设计：
1. 从历史"同一次提问同时检索到多个 skill"的样本构造组合（无历史时用合成组合）；
2. 对每个组合跑 compute_skill_conflicts 静态检测（互斥输出 flag / when_to_use 撞车）；
3. 硬失败（互斥输出要求）的 skill **阻断**：不得进入检索注入，也不能晋级；
4. 重复 Skill（同 identity/相似度超阈值）给出 add/merge/revise/discard 追溯建议。

这是评测器不是修复器：findings 落盘供闭环消费。
用法：python eval/skill_combo_replay.py [--json] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _mk_skill(name: str, desc: str, when: str, body: str) -> dict:
    return {"name": name, "description": desc, "when_to_use": when, "instructions": body}


def synthetic_conflict_pair() -> tuple[dict, dict]:
    """合成互斥对：同一触发场景，输出要求互斥（json vs table）。"""
    a = _mk_skill(
        "api-response-json",
        "API 响应断言必须输出 JSON",
        "when reviewing api endpoints output format testing",
        "输出必须为纯 JSON，禁止 markdown 表格。",
    )
    b = _mk_skill(
        "api-response-table",
        "API 响应断言必须输出表格",
        "when reviewing api endpoints output format testing",
        "输出必须为 markdown 表格，禁止 JSON。",
    )
    return a, b


def synthetic_duplicate_pair() -> tuple[dict, dict]:
    """合成重复对：近同能力、近同触发（重复率审计）。"""
    a = _mk_skill(
        "commit-message",
        "写 git commit message 的约定",
        "when writing git commit messages",
        "首行 <=50 字符，祈使句，正文解释 why。",
    )
    b = _mk_skill(
        "commit-msg-convention",
        "git commit 信息的书写约定",
        "when writing git commit message conventions",
        "首行 50 字符内，用祈使句，正文说明原因。",
    )
    return a, b


def load_skills_for_replay(skills: list[dict]):
    """把合成 skill 注入 compute_skill_conflicts 的输入（不写真实 .evoharness）。"""
    from agents.skills import compute_skill_conflicts

    class _S:
        def __init__(self, d: dict) -> None:
            self.name = d["name"]
            self.description = d["description"]
            self.when_to_use = d["when_to_use"]
            self.instructions = d["instructions"]
            # compute_skill_conflicts/_output_flags 还会读这些字段
            self.prompt_template = d["instructions"]
            self.body = d["instructions"]

    stubs = [_S(s) for s in skills]
    return compute_skill_conflicts(stubs)


def replay(skills: list[dict]) -> dict:
    """对一组 skill 跑组合重放：静态互斥检测 + 重复率审计 + 阻断判定。"""
    from agents.skills import compute_skill_conflicts

    conflicts = load_skills_for_replay(skills)
    names = [s["name"] for s in skills]

    # 互斥对（硬失败）：冲突表里的组合必须阻断，不能因为"两个都高分"就都注入
    hard_failures = []
    for i in range(len(skills)):
        for j in range(i + 1, len(skills)):
            a, b = names[i], names[j]
            if b in (conflicts.get(a) or []) or a in (conflicts.get(b) or []):
                hard_failures.append({
                    "pair": [a, b],
                    "severity": "hard-fail",
                    "action": "block-both-from-injection",
                    "why": "静态互斥/触发撞车：同时注入会让模型收到互斥指令",
                })

    # 重复审计（trace 到四路决策）
    duplicates = audit_duplicates(skills)

    return {
        "ok": True,
        "skillsReplayed": names,
        "conflictMap": conflicts,
        "hardFailures": hard_failures,
        "blockedFromInjection": sorted({n for f in hard_failures for n in f["pair"]}),
        "duplicates": duplicates,
        "claims": [
            "硬失败组合必须阻断（不得均分覆盖：两个互斥 skill 不能同时注入）",
            "重复 Skill 的处置必须可追溯到 add/merge/revise/discard 之一",
        ],
    }


def audit_duplicates(skills: list[dict]) -> list[dict]:
    """重复率审计：相似度高的对给出可追溯的四路决策建议。

    门槛 0.4（不是 0.55）：中英混排、中文描述按空格分词后 token 少，
    Jaccard 天然偏低——实测合成重复对（同义改写）只有 0.5。
    最终 merge/discard 仍由 LLM 决策链四选一，这里只做"值得看"的召回。
    """
    from agents.online_skill_evolution import _normalize_identity

    out: list[dict] = []
    for i in range(len(skills)):
        for j in range(i + 1, len(skills)):
            a, b = skills[i], skills[j]
            id_a = _normalize_identity(a["name"])
            id_b = _normalize_identity(b["name"])
            tokens_a = set((a["when_to_use"] + " " + a["description"]).lower().split())
            tokens_b = set((b["when_to_use"] + " " + b["description"]).lower().split())
            union = tokens_a | tokens_b
            jaccard = (len(tokens_a & tokens_b) / len(union)) if union else 0.0
            if id_a == id_b or jaccard >= 0.4:
                out.append({
                    "pair": [a["name"], b["name"]],
                    "identityEqual": id_a == id_b,
                    "jaccard": round(jaccard, 2),
                    "suggestedAction": "merge" if jaccard < 0.85 else "discard-one",
                    "trace": "add-duplicate-detected → merge (same capability) / discard (near-identical)",
                })
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    # 场景 1：互斥对必须被检测并阻断
    a, b = synthetic_conflict_pair()
    conflict_replay = replay([a, b])

    # 场景 2：重复对必须给出可追溯决策
    d1, d2 = synthetic_duplicate_pair()
    duplicate_replay = replay([d1, d2])

    # 场景 3：无冲突的独立 skill 不得误伤
    clean = replay([
        _mk_skill("python-sort", "排序工具用法", "when sorting python lists", "用 sorted()。"),
        _mk_skill("git-tag", "打 tag 约定", "when creating git tags", "语义化版本。"),
    ])

    report = {
        "kind": "skill-combo-replay",
        "conflictPair": conflict_replay,
        "duplicatePair": duplicate_replay,
        "cleanPair": {"hardFailures": clean["hardFailures"], "duplicates": clean["duplicates"]},
        "assertions": {
            "conflictDetected": bool(conflict_replay["hardFailures"]),
            "conflictBlocked": bool(conflict_replay["blockedFromInjection"]),
            "duplicateTraced": bool(duplicate_replay["duplicates"]),
            "cleanNotFlagged": not clean["hardFailures"] and not clean["duplicates"],
        },
    }
    report["ok"] = all(report["assertions"].values())
    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "skill-combo-replay.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report["assertions"], ensure_ascii=False))
        print(f"-> {out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
