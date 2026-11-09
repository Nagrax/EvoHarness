"""影子统计回归：影子模式的候选必须计入 precision 分母。"""
from __future__ import annotations

import json

from app.core.clock import utcnow
from app.core.database import session_scope
from app.models import Experience, ExperienceStatus
from scripts import shadow_eval


def test_shadow_mode_all_gates_pass_counts_as_auto_eligible(auto_mode, source, tmp_path):
    intent = "intent-shadow-1"
    with session_scope() as session:
        old = Experience(
            tenant="default",
            owner="ou_owner",
            intent_id=intent,
            version=1,
            question_pattern="请问年假怎么申请？",
            canonical_answer="旧版答案。",
            status=ExperienceStatus.SUPERSEDED,
            visibility="private",
            category="faq",
            created_by="test",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        current = Experience(
            tenant="default",
            owner="ou_owner",
            intent_id=intent,
            version=2,
            question_pattern="请问年假怎么申请？",
            canonical_answer="年假需提前3天在OA系统提交申请。",
            status=ExperienceStatus.APPROVED_AUTO,
            visibility="public",
            sensitive_level="internal",
            audience_json='{"chat_types": ["p2p"]}',
            category="faq",
            created_by="test",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add_all([old, current])
        session.flush()
        old_id = old.id
        current_id = current.id
    assert old_id != current_id

    holdout = tmp_path / "holdout.json"
    holdout.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "H001",
                        "question": "请问年假怎么申请？",
                        "expect": "auto_reply",
                        "expect_experience_id": old_id,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = shadow_eval.run(holdout, None)

    assert result["stats"]["autoEligible"] == 1
    assert result["stats"]["precisionCorrect"] == 1
    assert result["stats"]["severeErrors"] == 0
    assert result["rows"][0]["experienceId"] == current_id
    assert result["stats"]["verdict"] == "insufficient_sample_keep_single_approval"
