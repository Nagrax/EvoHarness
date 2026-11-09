"""F09 影子统计：对冻结留出集跑 检索+策略 的影子判定并输出可复核统计。

只读生产库；不发送任何消息（policy.evaluate shadow_override=True）。
统计口径（docs/FEISHU-DELIVERY-PLAYBOOK.md §验收矩阵）：
- precision 分母 = 系统会实际发出的候选（shadow 判定 auto_eligible 的 case）；
- coverage = 全部合格问题上的命中；
- 严重错误 = 高置信命中错误经验 / 敏感或注入被放行；
- 单侧 95% Wilson 下界；样本不足输出"维持 single_approval"。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.database import session_scope  # noqa: E402
from app.domain.policy import evaluate as policy_evaluate  # noqa: E402
from app.memory.experience import find_best_match  # noqa: E402
from app.models import Experience, Message, Source  # noqa: E402


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    if total == 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, (centre - margin) / denom)


def load_holdout(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else data
    assert isinstance(cases, list) and cases, "留出集为空或格式错误"
    for case in cases:
        for key in ("id", "question", "expect"):
            assert key in case, f"case 缺字段 {key}: {case}"
        assert case["expect"] in {"auto_reply", "draft_review", "no_reply"}, case["expect"]
    return cases


def build_probe_message(session, chat_id: str, text: str, source: Source) -> Message:
    """构造不入库的影子探针 Message 行（只用于 policy.evaluate 的只读字段）。"""
    from app.core.clock import epoch_ms, utcnow

    now = utcnow()
    created_ms = epoch_ms(now)
    return Message(
        tenant=source.tenant,
        message_id=f"shadow-probe-{created_ms}",
        source_id=source.id,
        chat_id=chat_id,
        thread_id="",
        sender_id=source.owner,
        sender_name="",
        msg_type="text",
        content_text=text,
        content_hash="",
        create_time=created_ms,
        event_time_ms=created_ms,
        is_from_bot=False,
        is_mention=False,
        category="question",
    )


def is_auto_eligible(row: dict) -> bool:
    """影子模式下所有硬门通过时，决策会被降级为 draft_only 并附 shadow_mode。"""
    if row.get("decision") == "auto_eligible":
        return True
    return bool(row.get("shadow")) and "shadow_mode" in (row.get("reasons") or [])


def run(holdout_path: Path, out_path: Path | None = None) -> dict:
    settings = get_settings()
    cases = load_holdout(holdout_path)
    rows = []
    with session_scope() as session:
        source = (
            session.query(Source)
            .filter(Source.tenant == settings.tenant_id, Source.revoked_at.is_(None), Source.enabled.is_(True))
            .first()
        )
        if source is None:
            raise SystemExit("无可用启用来源：影子统计需要至少一个未撤权来源")
        for case in cases:
            text = case["question"]
            probe = build_probe_message(session, source.chat_id, text, source)
            match = find_best_match(session, text, tenant=settings.tenant_id, source=source)
            decision = policy_evaluate(
                session,
                message=probe,
                source=source,
                experience=match.experience,
                similarity=match.score,
                match_reasons=match.reasons,
                shadow_override=True,
            )
            expected = None
            if case.get("expect_experience_id"):
                expected = session.get(Experience, case["expect_experience_id"])
            rows.append({
                "id": case["id"],
                "expect": case["expect"],
                "decision": decision.decision,
                "shadow": decision.shadow,
                "similarity": round(decision.similarity, 4),
                "experienceId": decision.experience.id if decision.experience else None,
                "intentId": decision.experience.intent_id if decision.experience else None,
                "expectExperience": case.get("expect_experience_id"),
                # 留出集冻结的是经验版本；正确性按 intent 判断，允许同一经验迭代到新版本。
                "expectIntent": expected.intent_id if expected else None,
                "reasons": decision.reasons,
            })

    # 统计
    eligible = [r for r in rows if is_auto_eligible(r)]
    # precision 分母：系统会实际发出的候选；正确 = expect=auto_reply 且经验命中期望（或未指定期望但确有经验）
    correct = [
        r for r in eligible
        if r["expect"] == "auto_reply"
        and (r["expectIntent"] is None or r["intentId"] == r["expectIntent"])
    ]
    severe = [
        r for r in rows
        if r["expect"] == "no_reply" and is_auto_eligible(r)
    ] + [
        r for r in rows
        if r["expect"] == "auto_reply" and is_auto_eligible(r)
        and r["expectIntent"] is not None and r["intentId"] != r["expectIntent"]
    ]
    qualified = [r for r in rows if r["expect"] == "auto_reply"]
    covered = [
        r for r in qualified
        if r["decision"] in ("auto_eligible",) or (r["experienceId"] is not None)
    ]
    stats = {
        "cases": len(rows),
        "autoEligible": len(eligible),
        "precisionSample": len(eligible),
        "precisionCorrect": len(correct),
        "precision": round(len(correct) / len(eligible), 4) if eligible else None,
        "precisionWilsonLB95": round(wilson_lower_bound(len(correct), len(eligible)), 4) if eligible else None,
        "coverageQualified": len(qualified),
        "coverage": round(len(covered) / len(qualified), 4) if qualified else None,
        "severeErrors": len(severe),
        "severeDetail": [{"id": r["id"], "reasons": r["reasons"]} for r in severe],
        "thresholds": {"precisionLB95": 0.95, "severeErrors": 0, "minAdjudicated": 60},
        "verdict": None,
    }
    if stats["precisionSample"] < stats["thresholds"]["minAdjudicated"]:
        stats["verdict"] = "insufficient_sample_keep_single_approval"
    elif stats["precisionWilsonLB95"] >= 0.95 and stats["severeErrors"] == 0:
        stats["verdict"] = "thresholds_met_owner_release_required"
    else:
        stats["verdict"] = "thresholds_not_met_keep_single_approval"

    result = {"holdout": str(holdout_path), "stats": stats, "rows": rows}
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if out_path:
        out_path.write_text(text, encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="F09 影子统计")
    parser.add_argument("--holdout", default=str(ROOT / "docs" / "feishu-delivery" / "probes" / "F09-holdout-v1.json"))
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    out = Path(args.out) if args.out else None
    result = run(Path(args.holdout), out)
    # 打印时脱敏问题文本，只输出统计与决策
    safe = json.loads(json.dumps(result, ensure_ascii=False))
    for row in safe["rows"]:
        row.pop("question", None)
    print(json.dumps(safe["stats"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
